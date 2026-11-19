"""Real MySQL contracts: namespaces, concurrency, WAL import and crash recovery."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from contextlib import closing
from pathlib import Path
from threading import Barrier, Event
from tempfile import TemporaryDirectory
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from pydantic import SecretStr
from sqlalchemy.exc import OperationalError
from finagent.config import Settings
from finagent.errors import AgentError
from finagent.storage.database import get_database
from finagent.storage.migration import import_sqlite, prepare
from finagent.web.auth_store import AuthStore, AuthError, password_hash
from finagent.web.store import WebStore
from finagent.research.store import ResearchStore
from finagent.research.contracts import ResearchRequest, ResearchScope
from finagent.runtime import now
from tests.mysql_support import isolated_database
from tests.auth_support import PASSWORD


class MySQLStorageTests(unittest.TestCase):
    def setUp(self):
        isolated_database(self)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=self.root)
        self.database = get_database(self.settings.database)

    def auth(self):
        return AuthStore(self.root / 'auth', self.settings.auth, database=self.database)

    def test_same_ids_are_isolated_and_no_sqlite_is_created(self):
        a, b = WebStore(self.root/'a/web'), WebStore(self.root/'b/web')
        for store in (a,b):
            with store.db() as s:
                s.add('web_sessions', dict(id='same',seq=1,title=store.root.parent.name,created_at=now().isoformat()))
            store.event('same','created',{},'same-source')
        self.assertEqual(a.session('same')['title'],'a')
        self.assertEqual(b.session('same')['title'],'b')
        self.assertEqual(a.events()[0]['sequence'],1)
        self.assertEqual(b.events()[0]['sequence'],1)
        self.assertEqual(list(self.root.rglob('*.sqlite3')),[])

    def test_missing_auth_metadata_never_silently_resets_existing_history(self):
        auth = self.auth()
        with auth.db() as s:
            s.remove('auth_meta', meta_key='csrf_key')
        with self.assertRaises(AgentError) as error:
            self.auth()
        self.assertEqual(error.exception.code, 'DATABASE_DATA_MISSING')

    def test_missing_namespace_is_not_silently_recreated(self):
        from finagent.storage.schema import storage_namespaces
        store = WebStore(self.root/'web')
        # A marker from a backup must not attach to a new empty database.
        other = self.root/'other'
        other.mkdir()
        (other/'.storage_namespace.json').write_text(json.dumps({'namespace_id':store.scope.id}))
        with self.assertRaises(AgentError) as error:
            WebStore(other/'web')
        self.assertEqual(error.exception.code, 'DATABASE_NAMESPACE_CHANGED')

    def test_concurrent_idempotency_and_last_queue_slot(self):
        store = WebStore(self.root/'web')
        session = store.create_session('new')['id']
        gate = Barrier(8)
        def run(i):
            gate.wait()
            return store.create_run(session,'same',{'text':'hello'},1)
        with ThreadPoolExecutor(8) as pool:
            values=list(pool.map(run, range(8)))
        self.assertEqual(sum(v[1] for v in values),1)
        self.assertEqual(len({v[0]['id'] for v in values}),1)
        self.assertEqual(len(store.events()),1)
        with self.assertRaises(AgentError) as err:
            store.create_run(session,'same',{'text':'different'},1)
        self.assertEqual(err.exception.code,'IDEMPOTENCY_CONFLICT')
        with self.assertRaises(AgentError) as err:
            store.create_run(session,'next',{},1)
        self.assertEqual(err.exception.code,'QUEUE_FULL')

    def test_opaque_keys_preserve_case_and_trailing_spaces(self):
        store = WebStore(self.root/'web')
        session = store.create_session('new')['id']
        values = [store.create_run(session,key,{},8) for key in ('Key','key','key ')]
        self.assertTrue(all(created for _,created in values))
        self.assertEqual(len({run['id'] for run,_ in values}),3)

    def test_cache_delete_failure_keeps_memory_forgetting_effective(self):
        from finagent.harness.memory import MemoryStore
        store = MemoryStore(self.root/'memory', database=self.database)
        record = store.write('preference','keep answers short',source='synthetic')
        with patch.object(store.vectors,'backend',side_effect=AgentError('DATABASE_UNAVAILABLE','synthetic')):
            self.assertEqual(store.forget(record['id']),{'forgotten':record['id']})
        self.assertEqual(store.records(),[])
        self.assertEqual(store.vectors.last_error,'DATABASE_UNAVAILABLE')

    def test_invite_and_initial_limit_row_are_atomic(self):
        auth=self.auth()
        invite=auth.create_invite()
        gate=Barrier(2)
        def register(i):
            gate.wait()
            try:
                auth.create_user('user'+str(i),PASSWORD,invite=invite,require_invite=True)
                return True
            except AuthError:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(register,range(2))),1)
        gate=Barrier(8)
        def limit(i):
            gate.wait()
            try:
                auth.limit('new-key',3,60)
                return True
            except AuthError:
                return False
        with ThreadPoolExecutor(8) as pool:
            self.assertEqual(sum(pool.map(limit,range(8))),3)

    def test_auth_retention_prunes_expired_sessions_and_limit_windows(self):
        auth=self.auth()
        with auth.db() as s:
            s.add('auth_limits',dict(limit_key='expired',started=0,count=1))
            s.add('auth_sessions',dict(token_hash='0'*64,user_id=None,csrf_hash='1'*64,auth_version=None,
                remember=0,created_at=0,last_seen_at=0,expires_at=1,revoked_at=None))
        auth.limit('fresh',3,60)
        auth.new_session()
        with auth.db() as s:
            self.assertIsNone(s.one('auth_limits',limit_key='expired'))
            self.assertIsNone(s.one('auth_sessions',token_hash='0'*64))

    def test_only_one_last_budget_reservation_and_pending_stays_charged(self):
        store=ResearchStore(self.root)
        scope=ResearchScope(**ResearchRequest(goal='synthetic',symbol='600519.SH',as_of=now()).model_dump())
        run=store.create(scope,self.settings.research,'synthetic')
        owner=store.acquire(run,'synthetic')
        gate=Barrier(6)
        def reserve(i):
            gate.wait()
            try:
                return store.reserve_budget(run,owner,'model','task',1,0)
            except AgentError as e:
                self.assertEqual(e.code,'RESEARCH_BUDGET')
                return None
        with ThreadPoolExecutor(6) as pool:
            values=list(pool.map(reserve,range(6)))
        self.assertEqual(sum(v is not None for v in values),1)
        self.assertEqual(store.usage(run)['calls']['model'],1)
        with self.assertRaises(AgentError):
            store.reserve_budget(run,'stale-owner','tool','task',10,0)
        self.assertEqual(store.usage(run)['calls'],{'model':1})

    def test_projection_rollback_deduplication_and_cursor(self):
        store=WebStore(self.root/'web')
        session=store.create_session('new')['id']
        rows=[dict(seq=10,body='{}'),dict(seq=11,body='{}')]
        transform=lambda row:('test',{})
        original=store._event
        def failing(db,*args):
            original(db,*args)
            raise ValueError('simulated crash')
        with patch.object(store,'_event',side_effect=failing), self.assertRaises(ValueError):
            store.project(session,rows,transform)
        self.assertEqual(store.cursor(session),0)
        self.assertEqual(store.events(),[])
        store.project(session,rows,transform)
        store.project(session,rows,transform)
        store.project(session,[rows[0]],transform)
        self.assertEqual(store.cursor(session),11)
        self.assertEqual([e['sequence'] for e in store.events()],[1,2])

    def test_event_cursor_never_passes_an_uncommitted_earlier_event(self):
        store = WebStore(self.root/'web')
        session = store.create_session('new')['id']
        inserted, release = Event(), Event()
        def first():
            with store.db() as db:
                store._event(db,session,'first',{},'first')
                inserted.set()
                if not release.wait(5):
                    raise RuntimeError('test barrier timeout')
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(first)
            self.assertTrue(inserted.wait(5))
            b = pool.submit(store.event,session,'second',{},'second')
            try:
                self.assertEqual(store.events(),[])
                self.assertFalse(b.done())
            finally:
                release.set()
            a.result(); b.result()
        self.assertEqual([e['sequence'] for e in store.events()],[1,2])

    def seed_legacy(self):
        owner='usr_'+'a'*32
        p=self.root/'auth/auth.sqlite3';p.parent.mkdir()
        with closing(sqlite3.connect(p)) as c, c:
            c.executescript('''CREATE TABLE users(id TEXT PRIMARY KEY,username_key TEXT,email_key TEXT,display_name TEXT,password_hash TEXT,status TEXT,auth_version INTEGER,created_at REAL,updated_at REAL);
                CREATE TABLE auth_meta(key TEXT PRIMARY KEY,value TEXT);''')
            c.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?)',(owner,'legacy',None,'Legacy',password_hash(PASSWORD),'active',1,1.,1.))
            c.execute('INSERT INTO auth_meta VALUES(?,?)',('csrf_key','ab'*32))
        for workspace in [self.root,self.root/'users'/owner]:
            p=workspace/'web/web.sqlite3';p.parent.mkdir(parents=True)
            c=sqlite3.connect(p)
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''CREATE TABLE sessions(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,title TEXT,created_at TEXT);
                CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT UNIQUE,session_id TEXT,body TEXT);
                CREATE TABLE cursors(session_id TEXT PRIMARY KEY,seq INTEGER);''')
            c.execute('INSERT INTO sessions VALUES(42,?,?,?)',('same','old',now().isoformat()))
            c.execute('INSERT INTO events VALUES(7,?,?,?)',('same-source','same',json.dumps({'type':'test','session_id':'same'})))
            c.execute('INSERT INTO cursors VALUES(?,?)',('same',99))
            c.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='events'")
            c.commit()
            self.addCleanup(c.close) # Keep WAL live through backup/import.
        return owner

    def test_wal_import_preserves_per_user_sequences_and_old_login(self):
        owner=self.seed_legacy()
        _, preview=prepare(self.root)
        self.assertEqual(preview['status'],'dry_run')
        report=import_sqlite(self.settings,self.database,apply=True)
        self.assertEqual(report['status'],'complete')
        self.assertTrue((Path(report['backup'])/'snapshot.json').is_file())
        a=WebStore(self.root/'web');b=WebStore(self.root/'users'/owner/'web')
        for store in [a,b]:
            self.assertEqual(store.events()[0]['sequence'],7)
            self.assertEqual(store.cursor('same'),99)
            store.event('same','new',{},'new')
            self.assertEqual(store.events(after=7)[0]['sequence'],101)
        auth=self.auth()
        self.assertEqual(auth.authenticate('legacy',PASSWORD,'127.0.0.1')['id'],owner)
        self.assertEqual(auth.csrf_key,bytes.fromhex('ab'*32))
        self.assertEqual(import_sqlite(self.settings,self.database,apply=True)['status'],'already_migrated')

    def test_interrupted_import_resumes_without_overwriting_conflicts(self):
        self.seed_legacy()
        from finagent.storage.database import Session
        original=Session.add
        def fail(s,name,values):
            if name=='web_events':
                raise ValueError('simulated import crash')
            return original(s,name,values)
        with patch.object(Session,'add',fail), self.assertRaises(ValueError):
            import_sqlite(self.settings,self.database,apply=True)
        self.database.checked=False
        with self.assertRaises(AgentError) as e:
            self.database.check()
        self.assertEqual(e.exception.code,'DATABASE_IMPORT_INCOMPLETE')
        with self.database.transaction(unchecked=True) as s:
            s.change('users',{'display_name':'conflict'})
        with self.assertRaises(ValueError):
            import_sqlite(self.settings,self.database,apply=True)
        with self.database.transaction(unchecked=True) as s:
            s.change('users',{'display_name':'Legacy'})
        self.assertEqual(import_sqlite(self.settings,self.database,apply=True)['status'],'complete')

    def test_import_refuses_a_live_cli_host_lease(self):
        from finagent.harness.state import RuntimeStore
        self.seed_legacy()
        runtime = RuntimeStore(self.root/'v3')
        (runtime.root/'sessions'/'occupied').mkdir(parents=True)
        with runtime.lease('host_' + sha256(b'occupied').hexdigest()[:24]):
            with self.assertRaises(AgentError) as error:
                import_sqlite(self.settings,self.database,apply=True)
        self.assertEqual(error.exception.code,'SESSION_BUSY')
        with self.database.transaction(unchecked=True) as s:
            self.assertEqual(s.count('storage_import_batches'),0)

    def test_empty_old_event_table_keeps_autoincrement_high_water(self):
        self.seed_legacy()
        with closing(sqlite3.connect(self.root/'web/web.sqlite3')) as c,c:
            c.execute('DELETE FROM events')
        import_sqlite(self.settings,self.database,apply=True)
        store=WebStore(self.root/'web')
        self.assertEqual(store.events(),[])
        store.event('same','new',{},'new')
        self.assertEqual(store.events()[0]['sequence'],101)

    def test_unknown_commit_error_does_not_retry_or_expose_credentials(self):
        self.database.check()
        exc=OperationalError('private sql',{},Exception('secret-password'))
        with patch.object(self.database.engine,'begin',side_effect=exc) as begin:
            with self.assertRaises(AgentError) as e:
                self.database.atomic(lambda s:None)
        self.assertEqual(begin.call_count,1)
        self.assertNotIn('secret-password',str(e.exception))
