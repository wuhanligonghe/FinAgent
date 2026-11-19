"""Real Redis runtime contracts; keys are isolated to temporary directory namespaces."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from finagent.config import RuntimeSettings, Settings, load_settings
from finagent.errors import AgentError
from finagent.harness.runtime_migration import migrate_runtimes
from finagent.harness.state import RuntimeStore
from finagent.harness.tasks import TaskBoard
from finagent.security import SecurityGuard


class RedisRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stores = []
        self.addCleanup(self.cleanup_namespaces)
        self.store = self.store_at(self.root / 'v3')

    def store_at(self, root):
        store = RuntimeStore(root)
        self.stores.append(store)
        return store

    def cleanup_namespaces(self):
        for store in self.stores:
            keys = list(store.client.scan_iter(match=store.prefix + '*'))
            if keys:
                store.client.unlink(*keys)

    def test_checkpoints_are_redis_only_and_survive_new_store_instances(self):
        checkpoint = {'messages': [{'role': 'user', 'content': '真实来源原话'}], 'state': {'revision': 3}}
        self.store.save('session_a', checkpoint)
        restored = self.store_at(self.store.root)
        self.assertEqual(restored.load('session_a'), checkpoint)
        self.assertEqual(restored.sessions(), ['session_a'])
        self.assertFalse((self.store.root / 'runtime.sqlite3').exists())
        self.assertEqual(self.store.client.ttl(self.store.prefix + 'sessions'), -1)

    def test_directory_and_session_namespaces_isolate_identical_ids(self):
        other = self.store_at(self.root / 'other_user/v3')
        self.store.save('same_session', {'owner': 'first'})
        other.save('same_session', {'owner': 'second'})
        self.store.put('tasks', 'session_a', {'id': 'same_task', 'owner': 'first'})
        self.store.put('tasks', 'session_b', {'id': 'same_task', 'owner': 'second'})
        self.assertEqual(self.store.load('same_session'), {'owner': 'first'})
        self.assertEqual(other.load('same_session'), {'owner': 'second'})
        self.assertEqual(self.store.records('tasks', 'session_a')[0]['owner'], 'first')
        self.assertEqual(self.store.records('tasks', 'session_b')[0]['owner'], 'second')
        self.assertEqual(other.records('tasks', 'session_a'), [])

    def test_event_deduplication_is_atomic_and_acknowledgments_are_durable(self):
        def emit(_):
            return self.store.emit('session_a', 'notification', {'value': 1}, event_id='same_event', notify=True)
        with ThreadPoolExecutor(max_workers=8) as workers:
            events = list(workers.map(emit, range(24)))
        self.assertTrue(all(event == events[0] for event in events))
        self.assertEqual(len(self.store.events('session_a')), 1)
        self.assertEqual(len(self.store.pending('session_a')), 1)
        self.store.ack(['same_event'])
        emit(None)
        self.assertEqual(self.store_at(self.store.root).pending('session_a'), [])
        self.assertEqual(len(self.store.events('session_a')), 1)

    def test_event_projection_keeps_sequence_order_and_filters_across_pages(self):
        for index in range(105):
            self.store.emit('session_a', 'noise', {'i': index})
        self.store.emit('session_a', 'wanted', {'i': 105}, notify=True)
        wanted = self.store.events('session_a', kind='wanted', limit=1)
        self.assertEqual(len(wanted), 1)
        self.assertEqual(json.loads(wanted[0]['body'])['payload'], {'i': 105})
        self.assertEqual(self.store.events('session_a', after=wanted[0]['seq']), [])
        self.assertEqual(self.store.events('other_session'), [])

    def test_competing_task_claims_have_exactly_one_owner(self):
        board = TaskBoard(self.store, 'session_a', self.root)
        task = board.create('竞争认领')
        barrier = threading.Barrier(2)

        def claim(owner):
            barrier.wait()
            try:
                board.claim(task['id'], owner)
                return owner
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            winners = [value for value in workers.map(claim, ('worker_a', 'worker_b')) if value]
        self.assertEqual(len(winners), 1)
        self.assertEqual(board.get(task['id'])['owner'], winners[0])
        self.assertEqual(board.get(task['id'])['version'], 1)

    def test_same_worker_cannot_atomically_claim_two_tasks(self):
        board = TaskBoard(self.store, 'session_a', self.root)
        tasks = [board.create('first'), board.create('second')]
        barrier = threading.Barrier(2)

        def claim(task):
            barrier.wait()
            try:
                board.claim(task['id'], 'same_worker')
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sum(workers.map(claim, tasks)), 1)

    def test_failed_transaction_does_not_write_mutated_records(self):
        self.store.put('tasks', 'session_a', {'id': 'task_a', 'value': 1})

        def fail(records):
            records['task_a']['value'] = 2
            raise ValueError('rejected')

        with self.assertRaises(ValueError):
            self.store.update_records('tasks', 'session_a', fail)
        self.assertEqual(self.store.records('tasks', 'session_a')[0]['value'], 1)

    def test_snapshot_restore_preserves_notifications_cursors_and_refuses_overwrite(self):
        self.store.save('session_a', {'state': {'revision': 2}})
        self.store.put('work', 'session_a', {'id': 'work_a', 'status': 'completed'})
        self.store.emit('session_a', 'finished', event_id='event_one', notify=True)
        self.store.emit('session_a', 'finished', event_id='event_two', notify=True)
        self.store.ack(['event_one'])
        snapshot = self.store.snapshot()
        target = self.store_at(self.root / 'restored/v3')
        target.restore(snapshot, source='test_snapshot')
        self.assertEqual(target.snapshot(), snapshot)
        last = target.events('session_a')[-1]['seq']
        target.emit('session_a', 'next')
        self.assertEqual(target.events('session_a', after=last)[0]['seq'], last + 1)
        self.assertEqual([event['event_id'] for event in target.pending('session_a')], ['event_two'])
        with self.assertRaises(AgentError) as raised:
            target.restore(snapshot, source='cannot_replace_live_state')
        self.assertEqual(raised.exception.code, 'RUNTIME_NOT_EMPTY')

    def test_missing_namespace_fails_instead_of_rebuilding_empty_history(self):
        self.store.save('session_a', {'state': {'revision': 2}})
        self.store.client.delete(self.store.prefix + 'meta')
        with self.assertRaises(AgentError) as raised:
            RuntimeStore(self.store.root)
        self.assertEqual(raised.exception.code, 'RUNTIME_DATA_MISSING')

    def test_unavailable_redis_has_no_sqlite_fallback_and_hides_credentials(self):
        settings = RuntimeSettings(redis_url='redis://:private-redis-password@127.0.0.1:1/0', connect_timeout=.1, socket_timeout=.1)
        with self.assertRaises(AgentError) as raised:
            RuntimeStore(self.root / 'unavailable', settings=settings)
        self.assertEqual(raised.exception.code, 'RUNTIME_UNAVAILABLE')
        self.assertNotIn('private-redis-password', str(raised.exception))
        self.assertFalse((self.root / 'unavailable/runtime.sqlite3').exists())

    def test_config_redis_secret_is_loaded_and_redacted(self):
        path = self.root / 'finagent.toml'
        path.write_text('')
        url = 'redis://:synthetic-redis-password@127.0.0.1:6379/0'
        (self.root / '.env').write_text('FINAGENT_REDIS_URL=' + url)
        settings = load_settings(path)
        self.assertEqual(settings.runtime.redis_url.get_secret_value(), url)
        self.assertNotIn('synthetic-redis-password', settings.model_dump_json())
        self.assertNotIn('synthetic-redis-password', SecurityGuard(settings).redact(url)[0])

    def test_offline_sqlite_import_includes_wal_and_retains_sources(self):
        base = self.root / 'legacy'
        runtime = base / 'v3'
        runtime.mkdir(parents=True)
        path = runtime / 'runtime.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('CREATE TABLE sessions(id TEXT PRIMARY KEY,body TEXT);'
                             'CREATE TABLE events(seq INTEGER PRIMARY KEY,id TEXT,session TEXT,kind TEXT,body TEXT,delivered INTEGER);'
                             'CREATE TABLE tasks(id TEXT,session TEXT,body TEXT);')
            checkpoint = {'messages': [{'role': 'user', 'content': 'keep source verbatim'}]}
            db.execute('INSERT INTO sessions VALUES(?,?)', ('session_a', json.dumps(checkpoint)))
            event = {'event_id': 'old_event', 'session_id': 'session_a', 'kind': 'finished', 'payload': {}}
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)', (9, 'old_event', 'session_a', 'finished', json.dumps(event), 0))
            db.execute('INSERT INTO tasks VALUES(?,?,?)', ('task_a', 'session_a', json.dumps({'id': 'task_a', 'status': 'pending'})))
            db.commit()
            with self.assertRaises(AgentError) as raised:
                RuntimeStore(runtime)
            self.assertEqual(raised.exception.code, 'RUNTIME_MIGRATION_REQUIRED')
            settings = Settings(data_dir=base)
            self.assertEqual(migrate_runtimes(settings)['status'], 'dry_run')
            report = migrate_runtimes(settings, apply=True)
            self.assertEqual(report['status'], 'complete')
        imported = self.store_at(runtime)
        self.stores.append(RuntimeStore(base / 'web_server'))
        self.assertEqual(imported.load('session_a'), checkpoint)
        self.assertEqual(imported.events('session_a')[0]['seq'], 9)
        self.assertEqual(imported.pending('session_a'), [event])
        imported.emit('session_a', 'new')
        self.assertEqual(imported.events('session_a')[-1]['seq'], 10)
        self.assertTrue(path.exists())
        self.assertTrue(Path(report['stores'][0]['backup']).exists())
        self.assertEqual(migrate_runtimes(settings, apply=True)['stores'][0]['status'], 'already_migrated')


if __name__ == '__main__':
    unittest.main()
