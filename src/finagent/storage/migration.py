"""Offline SQLite snapshots and verified one-way MySQL imports.

This module is the only new business-storage path allowed to read SQLite.
"""
from contextlib import ExitStack, closing
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import time
from uuid import uuid4

from sqlalchemy import select, func

from ..errors import AgentError
from ..harness.state import atomic_json, RuntimeStore
from .database import Session, REVISION
from .schema import TABLES
from .scope import resolve_scope

AUTH = {n:n for n in ('users', 'auth_sessions', 'invites', 'auth_audit', 'auth_limits', 'auth_meta')}
WEB = {'sessions':'web_sessions', 'runs':'web_runs', 'events':'web_events', 'cursors':'web_cursors'}
RESEARCH = {n:'research_' + n for n in ('runs', 'tasks', 'reservations', 'grants', 'events', 'stage_states', 'execution_selections')}


def read_sqlite(path):
    if path.is_symlink():
        raise ValueError('SQLite source must not be a symlink')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as c:
        c.row_factory = sqlite3.Row
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed: ' + str(path))
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        result = {}
        for name in tables:
            if name.startswith('sqlite_'):
                continue
            escaped = name.replace('"', '""')
            prefix = 'rowid AS _old_rowid, ' if name in ('tasks', 'stage_states') else ''
            result[name] = [dict(r) for r in c.execute(f'SELECT {prefix}* FROM "{escaped}"')]
        if 'sqlite_sequence' in tables:
            result['_sequences'] = {r[0]:r[1] for r in c.execute('SELECT name,seq FROM sqlite_sequence')}
        return result


def discover(root):
    root = Path(root).resolve()
    if (root / 'users').is_symlink():
        raise ValueError('User directory cannot be a symlink')
    roots = [root] + sorted((root / 'users').glob('usr_*'))
    if any(p.is_symlink() or not p.resolve().is_relative_to(root) for p in roots):
        raise ValueError('Workspace directory escapes data root')
    sources = []
    if (root / 'auth/auth.sqlite3').exists():
        sources.append(dict(kind='auth', path=root / 'auth/auth.sqlite3', root=root, user=None))
    for workspace in roots:
        for kind, relative in [('web', 'web/web.sqlite3'), ('research', 'research.sqlite3')]:
            p = workspace / relative
            if p.exists():
                sources.append(dict(kind=kind, path=p, root=workspace, user=None))
        for p in sorted((workspace / 'v3/memory/users').glob('*/embeddings.sqlite3')):
            sources.append(dict(kind='vectors', path=p, root=workspace, user=p.parent.name))
    for source in sources:
        relative = source['path'].relative_to(root)
        if any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
            raise ValueError('Source path contains symlink')
    return sources


def convert(kind, source_table, row):
    value = dict(row)
    old_rowid = value.pop('_old_rowid', None)
    if kind == 'auth':
        if source_table in ('auth_meta', 'auth_limits'):
            value['meta_key' if source_table == 'auth_meta' else 'limit_key'] = value.pop('key')
    elif kind == 'web':
        if source_table == 'runs':
            value['idempotency_key'] = value.pop('key')
        if source_table == 'cursors':
            value['runtime_seq'] = value.pop('seq')
    elif kind == 'research':
        if 'run' in value:
            value['run_id'] = value.pop('run')
        if source_table in ('tasks', 'stage_states'):
            value['task_id'] = value.pop('id')
            value['seq'] = old_rowid
        elif 'task' in value:
            value['task_id'] = value.pop('task')
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_values(name, value):
    table = TABLES[name]
    if set(value) - set(table.c.keys()):
        raise ValueError('Unmapped columns in ' + name + ': ' + ','.join(sorted(set(value) - set(table.c.keys()))))
    for col in table.c:
        if col.name in value and isinstance(value[col.name], str):
            limit = getattr(col.type, 'length', None)
            if limit and len(value[col.name]) > limit:
                raise ValueError('Value too long for ' + name + '.' + col.name)
    for field in ('scope', 'config', 'command', 'body', 'usage', 'vector'):
        if value.get(field) is not None:
            json.loads(value[field])
    if name == 'web_runs' and value.get('result') is not None:
        json.loads(value['result'])


def prepare(root):
    sources = discover(root)
    if not sources:
        raise ValueError('No SQLite business databases found')
    prepared, summaries = [], []
    digest_parts = []
    for source in sources:
        content = read_sqlite(source['path'])
        mapping = AUTH if source['kind'] == 'auth' else WEB if source['kind'] == 'web' else RESEARCH if source['kind'] == 'research' else {'vectors':'memory_vectors'}
        converted = {}
        for old, rows in content.items():
            if old.startswith('_'):
                continue
            if old not in mapping:
                if old == 'schema_migrations' or not rows:
                    continue
                raise ValueError('Unmapped nonempty source table: ' + old)
            name = mapping[old]
            if source['kind'] == 'vectors':
                from ..harness.memory_search import normalize
                values = []
                for row in rows:
                    vector = json.loads(row['vector'])
                    normalize(vector)
                    values.append(dict(memory_user_id=source['user'], memory_id=row['memory_id'],
                        fingerprint_hash=sha256(row['fingerprint'].encode()).hexdigest(), fingerprint=row['fingerprint'],
                        text_hash=row['text_hash'], dimensions=len(vector), vector=row['vector']))
            else:
                values = [convert(source['kind'], old, r) for r in rows]
            for value in values:
                validate_values(name, value)
            if name in ('web_runs', 'research_runs') and any(v['status'] in ('running', 'queued', 'cancelling') for v in values):
                raise ValueError('Active work must finish or be explicitly interrupted before import: ' + str(source['path']))
            converted[name] = values
        part = dict(path=str(source['path']), tables=content)
        digest_parts.append(part)
        prepared.append({**source, 'values':converted, 'sequences':content.get('_sequences', {}), 'source_metadata':content.get('schema_migrations', [])})
        summaries.append(dict(path=str(source['path']), kind=source['kind'], tables={k:len(v) for k,v in converted.items()}))
    return prepared, dict(status='dry_run', sources=summaries, source_digest=sha256(canonical(digest_parts).encode()).hexdigest())


def copy_tree(source, target):
    if source.is_symlink():
        raise ValueError('Backup source must not be a symlink: ' + str(source))
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            if child.name.endswith(('-wal', '-shm', '.lock')) or child.name.startswith('.storage_namespace'):
                continue
            copy_tree(child, target / child.name)
    elif source.suffix == '.sqlite3':
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as before, closing(sqlite3.connect(target)) as after:
            before.backup(after)
            if after.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Backup integrity check failed')
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def import_sqlite(settings, database, *, apply=False, backup_dir=None):
    root = settings.data_dir.resolve()
    prepared, report = prepare(root)
    if not apply:
        return report
    batch_id = report['source_digest'][:32]
    backup = Path(backup_dir) if backup_dir else root / 'migration' / ('mysql_' + batch_id)
    with ExitStack() as stack:
        # Match current Web service lease and guard CLI session stores.
        for store_root in [root / 'web_server'] + [p / 'v3' for p in {s['root'] for s in prepared} if (p / 'v3').exists()]:
            runtime = RuntimeStore(store_root, settings=settings.runtime)
            stack.enter_context(runtime.lease('finsight_web_server'))
            if (store_root / 'sessions').exists():
                for session in (store_root / 'sessions').iterdir():
                    if session.is_dir():
                        stack.enter_context(runtime.lease('host_' + sha256(session.name.encode()).hexdigest()[:24]))
        prepared, report = prepare(root)  # Re-read only after leases are held.
        if report['source_digest'][:32] != batch_id:
            raise ValueError('Source changed while acquiring maintenance leases; run preflight again')
        with database.transaction(unchecked=True) as s:
            existing = s.one('storage_import_batches', source_digest=report['source_digest'])
            if existing and existing['status'] == 'verified':
                return {**report, 'status':'already_migrated', 'batch_id':existing['id']}
            if existing is None:
                if s.rows('storage_import_batches') or any(s.connection.scalar(select(func.count()).select_from(t)) for n,t in TABLES.items() if n != 'storage_import_batches'):
                    raise ValueError('Target database is not empty; refusing to merge unrelated data')
                s.add('storage_import_batches', dict(id=batch_id, source_digest=report['source_digest'], status='importing',
                    manifest=canonical(report), created_at=time.time()))
        backup.mkdir(parents=True, exist_ok=True)
        backup.chmod(0o700)
        if not (backup / 'snapshot.json').exists():
            for source in prepared:
                copy_tree(source['path'], backup / 'sqlite' / source['path'].relative_to(root))
            for workspace in {s['root'] for s in prepared}:
                target = backup / 'files' / workspace.relative_to(root)
                for name in ('v3', 'research', 'results', 'evidence', 'library', 'runs', 'sessions', 'market_library', 'policies', 'signals'):
                    if (workspace / name).exists():
                        copy_tree(workspace / name, target / name)
                runtime_root = workspace / 'v3'
                if runtime_root.exists():
                    runtime = RuntimeStore(runtime_root, settings=settings.runtime)
                    snap = runtime.snapshot()
                    if any(r['kind'] == 'work' and r['body'].get('status') == 'running' for r in snap['records']):
                        raise ValueError('Running Redis work must stop before import')
                    atomic_json(target / 'runtime_redis.json', snap)
            atomic_json(backup / 'snapshot.json', {**report, 'batch_id':batch_id})
        # Identity/auth rows precede user namespaces and their foreign keys.
        scopes = {}
        for source in sorted(prepared, key=lambda x: 0 if x['kind'] == 'auth' else 1):
            if source['kind'] != 'auth':
                scopes.setdefault(str(source['root']), resolve_scope(database, source['root'], importing=True))
            scope = scopes.get(str(source['root'])) if source['kind'] != 'auth' else None
            for name, values in source['values'].items():
                for offset in range(0, len(values), 250):
                    with database.transaction(scope, lock=True, unchecked=True) as s:
                        for value in values[offset:offset+250]:
                            pk = {c.name:value[c.name] for c in TABLES[name].primary_key if c.name != 'namespace_id'}
                            prior = s.one(name, **pk)
                            if prior is not None:
                                if any(prior.get(k) != v for k,v in value.items()):
                                    raise ValueError('Conflicting target row in ' + name)
                            else:
                                s.add(name, value)
                # Compare all mapped fields, not only row counts.
                with database.transaction(scope, unchecked=True) as s:
                    actual = s.rows(name, **({'memory_user_id':source['user']} if source['kind'] == 'vectors' else {}))
                    expected = sorted(canonical(v) for v in values)
                    selected = sorted(canonical({k:r[k] for k in values[0]}) for r in actual) if values else []
                    if expected != selected or len(actual) != len(values):
                        raise ValueError('Imported rows do not match source: ' + name)
                    if name.startswith(('web_', 'research_')) and 'seq' in TABLES[name].c:
                        old_table = next((k for k,v in (WEB if source['kind'] == 'web' else RESEARCH).items() if v == name), '')
                        high = max([v['seq'] for v in values] + [source['sequences'].get(old_table, 0)])
                        s.upsert('namespace_sequences', dict(sequence_name=name, last_value=high), ['last_value'])
        # Verify every research artifact before making data visible.
        for source in prepared:
            for row in source['values'].get('research_tasks', []):
                if row.get('artifact'):
                    path = (source['root'] / row['artifact']).resolve()
                    allowed = (source['root'] / 'research' / row['run_id']).resolve()
                    if not path.is_relative_to(allowed) or not path.is_file() or sha256(path.read_bytes()).hexdigest() != row['hash']:
                        raise ValueError('Research artifact missing or changed: ' + row['run_id'])
            for row in source['values'].get('research_runs', []):
                if row.get('report'):
                    path = Path(row['report']).resolve()
                    if not path.is_relative_to((source['root'] / 'research' / row['id']).resolve()) or not path.is_file():
                        raise ValueError('Research report missing or outside workspace')
        # Local metadata is recoverable; database readiness is the final commit.
        auth_root = root / 'auth'
        if any(s['kind'] == 'auth' for s in prepared):
            with database.transaction(unchecked=True) as s:
                s.upsert('auth_meta', dict(meta_key='deployment_root', value=sha256(str(auth_root).encode()).hexdigest()), [])
            atomic_json(auth_root / '.mysql_auth.json', {'schema_version':1, 'batch_id':batch_id})
        with database.transaction(unchecked=True) as s:
            for scope in scopes.values():
                s.change('storage_namespaces', {'status':'ready'}, id=scope.id)
            s.change('storage_import_batches', {'status':'verified', 'manifest':canonical({**report, 'backup':str(backup)})}, id=batch_id)
        database.checked = False
        database.check()
        final = {**report, 'status':'complete', 'batch_id':batch_id, 'backup':str(backup)}
        atomic_json(backup / 'verified.json', final)
        return final
