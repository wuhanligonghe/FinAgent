"""Offline one-way import of legacy SQLite runtimes; never a live fallback."""
from contextlib import ExitStack, closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from ..errors import AgentError
from .state import RuntimeStore, atomic_json, encode


def legacy_snapshot(path):
    """Read a consistent old runtime, preserving event cursors and acknowledgments."""
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sessions = {row['id']: json.loads(row['body']) for row in db.execute('SELECT id,body FROM sessions')} if 'sessions' in tables else {}
        events = [{'seq': row['seq'], 'body': json.loads(row['body']), 'pending': row['delivered'] == 0}
                  for row in db.execute('SELECT seq,body,delivered FROM events ORDER BY seq')] if 'events' in tables else []
        records = []
        for kind in ('tasks', 'jobs', 'work'):
            if kind in tables:
                records.extend({'kind': kind, 'session': row['session'], 'id': row['id'], 'body': json.loads(row['body'])}
                               for row in db.execute(f'SELECT id,session,body FROM {kind} ORDER BY rowid'))
    return {'sessions': sessions, 'events': events, 'records': records}


def migrate_runtimes(settings, *, apply=False):
    base = settings.data_dir.resolve()
    roots = [base / 'web_server', base / 'v3', *sorted((base / 'users').glob('usr_*/v3'))]
    roots = [root for root in roots if (root / 'runtime.sqlite3').exists()]
    report = {'status': 'dry_run', 'stores': []}
    for root in roots:
        snapshot = legacy_snapshot(root / 'runtime.sqlite3')
        report['stores'].append({'root': str(root), 'sessions': len(snapshot['sessions']),
                                 'events': len(snapshot['events']), 'records': len(snapshot['records'])})
    if not apply:
        return report
    directory = base / 'migration' / ('runtime_redis_' + uuid4().hex)
    directory.mkdir(parents=True, mode=0o700)
    with ExitStack() as stack:
        # Works for both historical Web lease locations; running services refuse migration.
        for root in (base / 'web_server', base / 'v3'):
            store = RuntimeStore(root, settings=settings.runtime, allow_legacy=True)
            stack.enter_context(store.lease('finsight_web_server'))
        for item, root in zip(report['stores'], roots):
            store = RuntimeStore(root, settings=settings.runtime, allow_legacy=True)
            if store.client.hget(store.prefix + 'meta', 'legacy_import'):
                item['status'] = 'already_migrated'
                continue
            snapshot = legacy_snapshot(root / 'runtime.sqlite3')
            for session_id in snapshot['sessions']:
                name = 'host_' + sha256(session_id.encode()).hexdigest()[:24]
                stack.enter_context(store.lease(name))
            for entry in snapshot['records']:
                if entry['kind'] == 'work' and entry['body'].get('status') == 'running':
                    raise AgentError('RUNTIME_MIGRATION_BUSY', '旧运行存储仍有在途工作，请先完成或取消')
            web = root.parent / 'web' / 'web.sqlite3'
            if web.exists():
                with closing(sqlite3.connect(web.as_uri() + '?mode=ro', uri=True)) as db:
                    if db.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running','cancelling')").fetchone()[0]:
                        raise AgentError('RUNTIME_MIGRATION_BUSY', '旧 Web 队列未结束，请先完成或取消')
            name = sha256(str(root).encode()).hexdigest()[:16]
            backup = directory / (name + '.sqlite3')
            with closing(sqlite3.connect((root / 'runtime.sqlite3').as_uri() + '?mode=ro', uri=True)) as source, \
                 closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            # Import exactly the consistent backup, including committed WAL pages.
            snapshot = legacy_snapshot(backup)
            snapshot_file = directory / (name + '.json')
            atomic_json(snapshot_file, snapshot)
            digest = sha256(backup.read_bytes()).hexdigest()
            store.restore(snapshot, source='sqlite:' + digest)
            if store.snapshot() != snapshot:
                # Record order can differ between independent task tables; compare normalized entries.
                actual = store.snapshot()
                normalize = lambda value: sorted(encode(entry) for entry in value)
                if (actual['sessions'] != snapshot['sessions'] or actual['events'] != snapshot['events']
                        or normalize(actual['records']) != normalize(snapshot['records'])):
                    raise AgentError('RUNTIME_MIGRATION_VERIFY', 'Redis 导入核对失败；保留备份与源文件')
            item.update(status='migrated', backup=str(backup), sha256=digest, namespace=store.prefix)
            atomic_json(directory / 'manifest.json', report)
    report.update(status='complete', manifest=str(directory / 'manifest.json'))
    atomic_json(directory / 'manifest.json', report)
    return report
