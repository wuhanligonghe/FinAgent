"""Offline, explicit-owner migration. Never claims legacy data on first login."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from ..harness.state import RuntimeStore, atomic_json
from .auth_store import AuthStore
from .user_runtime import user_settings


def hashes(directory):
    return {str(p.relative_to(directory)): sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file() and not p.name.endswith(('-wal', '-shm'))}


def copy_consistent(source, target):
    if source.is_symlink():
        raise ValueError('迁移不接受符号链接')
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for path in source.iterdir():
            if not path.name.endswith(('-wal', '-shm', '.lock')):
                copy_consistent(path, target / path.name)
    elif source.suffix == '.sqlite3':
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True)) as before, closing(sqlite3.connect(target)) as after:
            before.backup(after)
            if after.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('数据库完整性检查未通过')
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


PATH_KEYS = {'report', 'artifact', 'path', 'full_output', 'transcript_ref', 'run_dir', 'directory', 'cwd', 'workspace', 'archives', 'artifact_refs'}


def rewrite_metadata(value, old, new, owner, key=''):
    if isinstance(value, dict):
        result = {k: rewrite_metadata(v, old, new, owner, k) for k, v in value.items()}
        if isinstance(result.get('memory_scope'), dict):
            result['memory_scope']['user_id'] = owner
        if 'schema_version' in result and 'session_id' in result and 'user_constraints' in result:
            result['owner_user_id'] = owner
            result['legacy_output_roots'] = [str(old / 'v3' / 'sessions' / result['session_id'])]
        # Only parse host-produced JSON. User prose and financial source text stay verbatim.
        if (result.get('_finagent_origin') == 'runtime_context' or result.get('role') == 'tool') and isinstance(result.get('content'), str):
            try:
                result['content'] = json.dumps(rewrite_metadata(json.loads(result['content']), old, new, owner), ensure_ascii=False)
            except ValueError:
                pass
        return result
    if isinstance(value, list):
        return [rewrite_metadata(v, old, new, owner, key) for v in value]
    if isinstance(value, str) and key in PATH_KEYS and (value == str(old) or value.startswith(str(old) + '/')):
        return str(new) + value[len(str(old)):]
    return value


def migrate_legacy(settings, owner, *, apply=False):
    base = settings.data_dir.resolve()
    from ..storage.database import get_database
    from ..storage.scope import resolve_scope
    from ..storage.schema import metadata
    database = get_database(settings.database)
    auth = AuthStore(base / 'auth', settings.auth, database=database)
    user = auth.user(owner)
    if not user:
        raise ValueError('先创建并显式选择接收历史数据的账号 ID')
    destination = user_settings(settings, owner).data_dir
    marker = base / 'migration' / f'{owner}.json'
    if marker.exists():
        return {'status':'already_migrated', 'owner':owner, 'destination':str(destination)}
    sources = [base / name for name in ('web', 'v3', 'library', 'evidence', 'results', 'research', 'research.sqlite3') if (base / name).exists()]
    report = {'status':'dry_run', 'owner':owner, 'sources':[str(p) for p in sources], 'destination':str(destination)}
    if not apply:
        return report
    if not sources:
        raise ValueError('没有待迁移的旧数据')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('目标用户已经有数据；禁止覆盖，请选择空账号并先完成迁移')
    # The same lease is held for the entire authenticated Web service lifetime.
    with RuntimeStore(base / 'web_server', settings=settings.runtime).lease('finsight_web_server'), RuntimeStore(base / 'v3', settings=settings.runtime).lease('finsight_web_server'):
        old_web = base / 'web' / 'web.sqlite3'
        if old_web.exists():
            with closing(sqlite3.connect(f'{old_web.as_uri()}?mode=ro', uri=True)) as db:
                if db.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running','cancelling')").fetchone()[0]:
                    raise ValueError('旧 Web 队列尚未停止，请先完成维护停写')
        source_scope = resolve_scope(database, base)
        with database.transaction(source_scope, lock=True) as db:
            from ..storage.schema import web_runs, research_runs
            if db.count("web_runs", where=[web_runs.c.status.in_(["queued", "running", "cancelling"])]) or db.count("research_runs", where=[research_runs.c.status == "running"]):
                raise ValueError("仍有运行中的业务任务，请先停写")
            business_snapshot = {t.name: db.rows(t.name) for t in metadata.sorted_tables if "namespace_id" in t.c}
        runtime = RuntimeStore(base / 'v3', settings=settings.runtime)
        runtime_snapshot = runtime.snapshot()
        if any(entry['kind'] == 'work' and entry['body'].get('status') == 'running'
               for entry in runtime_snapshot['records']):
            raise ValueError('仍有运行中的旧任务，请在维护窗口停止后再迁移')
        token = uuid4().hex
        (base / 'migration').mkdir(exist_ok=True)
        (base / 'migration').chmod(0o700)
        backup = base / 'migration' / f'backup_{owner}_{token}'
        stage = base / 'migration' / f'stage_{owner}_{token}'
        for source in sources:
            copy_consistent(source, backup / source.name)
        atomic_json(backup / 'runtime_redis.json', runtime_snapshot)
        atomic_json(backup / 'mysql_business.json', business_snapshot)
        shutil.copytree(backup, stage)
        (stage / 'mysql_business.json').unlink()
        (stage / 'runtime_redis.json').unlink()  # Keep the offline snapshot only in the backup.
        old_memory = stage / 'v3' / 'memory' / 'users' / 'local'
        if old_memory.exists():
            old_memory.rename(old_memory.with_name(owner))
        memory = stage / 'v3' / 'memory'
        # Support the older, unscoped memory layout as well.
        if memory.exists():
            target = memory / 'users' / owner
            target.mkdir(parents=True, exist_ok=True)
            project = settings.memory.project_id or 'workspace_' + sha256(str(settings.harness.workspace.resolve()).encode()).hexdigest()[:16]
            for name in ('forgotten.json', 'forgotten_sources.json'):
                legacy = memory / name
                if legacy.exists():
                    previous = json.loads((target/name).read_text()) if (target/name).exists() else []
                    from ..harness.memory import MemoryStore
                    previous.extend(MemoryStore._scoped_key(project, value) for value in json.loads(legacy.read_text()))
                    atomic_json(target/name, list(set(previous)))
            for path in memory.glob('mem_*.json'):
                path.rename(target / path.name)
            for path in target.glob('mem_*.json'):
                record = json.loads(path.read_text())
                record['user_id'] = owner
                if 'project_id' not in record:
                    record['project_id'] = settings.memory.project_id or 'workspace_' + sha256(str(settings.harness.workspace.resolve()).encode()).hexdigest()[:16]
                atomic_json(path, record)
        for path in stage.rglob('*.json'):
            value = json.loads(path.read_text())
            updated = rewrite_metadata(value, base, destination, owner)
            if value != updated:
                atomic_json(path, updated)
        # Historical SQLite files remain offline backup material. Business
        # records now move through a namespace-bound MySQL transaction.
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage.rename(destination)
        target_scope = resolve_scope(database, destination, owner_user_id=owner, importing=True)
        with database.transaction(target_scope, lock=True, unchecked=True) as db:
            for name, rows in business_snapshot.items():
                for original in rows:
                    row = rewrite_metadata(original, base, destination, owner)
                    for key in ("scope", "config", "body", "command", "result"):
                        if row.get(key):
                            try:
                                row[key] = json.dumps(rewrite_metadata(json.loads(row[key]), base, destination, owner), ensure_ascii=False)
                            except (ValueError, TypeError):
                                pass
                    if name == "memory_vectors":
                        row["memory_user_id"] = owner
                    if name == "namespace_sequences":
                        db.upsert(name, row, ["last_value"])
                    else:
                        db.add(name, row)
        target_runtime = RuntimeStore(destination / 'v3', settings=settings.runtime, allow_legacy=True)
        target_runtime.restore(rewrite_metadata(runtime_snapshot, base, destination, owner), source='account_migration')
        with database.transaction(unchecked=True) as db:
            db.change("storage_namespaces", {"status": "ready"}, id=target_scope.id)
        staged_hashes = hashes(destination)
        atomic_json(marker, {**report, 'status':'complete', 'backup':str(backup), 'files':staged_hashes})
        return {**report, 'status':'complete', 'backup':str(backup), 'files':len(staged_hashes)}
