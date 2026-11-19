"""Authentication and adversarial A/B isolation checks with synthetic models."""
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from threading import Event
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from finagent.config import Settings, AuthSettings
from finagent.harness.host import SessionHost
from finagent.models import ModelReply, ToolCall
from finagent.web.app import create_app
from finagent.web.auth import cookie_name
from finagent.web.auth_store import AuthStore, AuthError, digest
from finagent.web.migration import migrate_legacy
from finagent.web.store import WebStore
from tests.auth_support import login, PASSWORD
from tests.support import ScriptedModel


class AuthTests(unittest.TestCase):
    def setUp(self):
        from tests.mysql_support import isolated_database
        isolated_database(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.settings.harness.workspace = Path(self.temp.name)
        self.settings.harness.memory_enabled = False
        self.factory = lambda settings, session_id: SessionHost(settings, session_id,
            model=ScriptedModel(ModelReply({'role':'assistant', 'content':'synthetic reply'})))
        self.app = create_app(self.settings, host_factory=self.factory, testing=True)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.auth = self.app.state.auth
        self.a = self.auth.create_user('alice', PASSWORD, 'alice@example.com')
        self.b = self.auth.create_user('bob', PASSWORD)

    def other(self, name='bob'):
        client = TestClient(self.app)
        self.addCleanup(client.close)
        login(client, name)
        return client

    def test_private_routes_default_deny_and_auth_never_bypassed_by_testing(self):
        for path in ['/api/v1/workspace', '/api/v1/settings', '/api/v1/sessions', '/api/v1/events', '/api/v1/documents', '/api/v1/research']:
            self.assertEqual(self.client.get(path).status_code, 401, path)
        self.assertEqual(self.client.get('/api/v1/health').status_code, 200)
        self.assertEqual(self.client.get('/openapi.json').status_code, 200 if (Path('web/dist/index.html').exists()) else 503)
        self.assertEqual(self.client.post('/api/v1/auth/login', json={'identifier':'alice','password':PASSWORD}).status_code, 403)
        self.assertIn(self.client.get('/login', headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate'}).status_code, (200,503))
        with self.assertRaises(ValueError):
            AuthSettings(mode='local', allowed_hosts=['public.example.com'])
        with self.assertRaises(ValueError):
            AuthSettings(mode='local', allowed_origins=['https://public.example.com'])

    def test_rotation_cookie_flags_hashes_and_remember_expiry(self):
        self.client.headers['Origin'] = 'http://testserver'
        pre = self.client.get('/api/v1/auth/csrf').json()['csrf_token']
        old = self.client.cookies.get(cookie_name(self.settings.auth))
        response = self.client.post('/api/v1/auth/login', json={'identifier':'ALICE@example.com', 'password':PASSWORD, 'remember':True}, headers={'X-CSRF-Token':pre})
        self.assertEqual(response.status_code, 200, response.text)
        token = self.client.cookies.get(cookie_name(self.settings.auth))
        self.assertNotEqual(token, old)
        self.assertIsNone(self.auth.resolve(old))
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertIn('Max-Age=2592000', response.headers['set-cookie'])
        self.assertNotIn('Domain=', response.headers['set-cookie'])
        with self.auth.db() as db:
            raw = '\n'.join(str(tuple(r.values())) for r in db.rows('auth_sessions'))
            hashed = db.one('users', id=self.a['id'])['password_hash']
        self.assertNotIn(token, raw); self.assertNotIn(PASSWORD, hashed)
        self.assertTrue(hashed.startswith('$argon2id$'))
        self.assertEqual(self.client.get('/api/v1/auth/me').json()['user']['id'], self.a['id'])

    def test_origin_csrf_logout_password_reset_and_disable(self):
        login(self.client)
        self.assertEqual(self.client.post('/api/v1/sessions', json={}, headers={'X-CSRF-Token':'bad'}).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/sessions', json={}, headers={'Origin':'https://evil.example'}).status_code, 403)
        token = self.client.cookies.get(cookie_name(self.settings.auth))
        self.assertEqual(self.client.post('/api/v1/auth/logout').status_code, 204)
        self.assertIsNone(self.auth.resolve(token))
        self.assertEqual(self.client.get('/api/v1/auth/me').status_code, 401)
        login(self.client)
        self.auth.update_user(self.a['id'], password=PASSWORD + '-new')
        self.assertEqual(self.client.get('/api/v1/workspace').status_code, 401)
        login(self.client, password=PASSWORD + '-new')
        self.auth.update_user(self.a['id'], status='disabled')
        self.assertEqual(self.client.get('/api/v1/workspace').status_code, 401)

    def test_session_absolute_and_idle_expiry_are_server_enforced(self):
        login(self.client)
        token = self.client.cookies.get(cookie_name(self.settings.auth))
        with self.auth.db() as db:
            db.change('auth_sessions', {'last_seen_at':time.time()-7201}, token_hash=digest(token))
        self.assertEqual(self.client.get('/api/v1/auth/me').status_code, 401)
        login(self.client)
        with self.auth.db() as db:
            db.change('auth_sessions', {'expires_at':time.time()-1})
        self.assertEqual(self.client.get('/api/v1/auth/me').status_code, 401)

    def test_unknown_account_and_wrong_password_share_error_and_rate_limit(self):
        self.client.headers['Origin'] = 'http://testserver'
        self.client.headers['X-CSRF-Token'] = self.client.get('/api/v1/auth/csrf').json()['csrf_token']
        def attempt(name):
            return self.client.post('/api/v1/auth/login', json={'identifier':name, 'password':'wrong password'})
        known, unknown = attempt('alice'), attempt('does-not-exist')
        self.assertEqual(known.status_code, 401)
        self.assertEqual(known.json()['message'], unknown.json()['message'])
        for _ in range(4): attempt('does-not-exist')
        limited = attempt('does-not-exist')
        self.assertEqual(limited.status_code, 429)
        self.assertIn('retry-after', limited.headers)

    def test_invite_is_single_use_email_bound_and_registration_can_close(self):
        self.client.headers['Origin'] = 'http://testserver'
        self.client.headers['X-CSRF-Token'] = self.client.get('/api/v1/auth/csrf').json()['csrf_token']
        invite = self.auth.create_invite('new@example.com')
        body = {'username':'new_user', 'email':'other@example.com', 'password':PASSWORD, 'invite':invite}
        self.assertEqual(self.client.post('/api/v1/auth/register', json=body).status_code, 422)
        body['email'] = 'new@example.com'
        self.assertEqual(self.client.post('/api/v1/auth/register', json=body).status_code, 201)
        body.update(username='another', email=None)
        self.assertEqual(self.client.post('/api/v1/auth/register', json=body).status_code, 422)
        self.auth.settings.registration = 'closed'
        self.assertEqual(self.client.post('/api/v1/auth/register', json=body).status_code, 403)

    def test_ab_sessions_runs_documents_memory_and_events_are_private(self):
        login(self.client)
        bob = self.other()
        session = self.client.post('/api/v1/sessions', json={'title':'Alice private'}).json()['id']
        context = self.app.state.users.for_user(self.a['id'])
        run, _ = context.store.create_run(session, 'private', {'kind':'message','text':'Alice secret'})
        context.store.update_run(run['id'], 'completed', {'answer':'Alice private answer'})
        context.manager.settings.harness.memory_enabled = True
        context.manager.settings.memory.embedding_backend = 'disabled'
        memory = context.manager.host(session).memory.write('私有偏好', 'Alice prefers short answers', source='synthetic')
        context.store.event(session, 'private.event', {'text':'Alice event'})
        for method, path, data in [
            ('get',f'/api/v1/sessions/{session}/messages',None),
            ('post',f'/api/v1/sessions/{session}/messages',{'text':'read Alice'}),
            ('get',f'/api/v1/runs/{run["id"]}',None),
            ('post',f'/api/v1/runs/{run["id"]}/cancel',None),
            ('get',f'/api/v1/sessions/{session}/memories',None),
            ('delete',f'/api/v1/sessions/{session}/memories/{memory["id"]}',None),
            ('get',f'/api/v1/events?session_id={session}',None),
        ]:
            response = bob.request(method, path, json=data, headers={'Idempotency-Key':'cross-user'})
            self.assertEqual(response.status_code, 404, (path,response.text))
            self.assertNotIn('Alice secret', response.text)
        self.assertEqual(bob.get('/api/v1/workspace').json(), {'sessions':[], 'research':[]})
        self.assertEqual(self.client.get('/api/v1/documents').status_code, 404)
        self.assertEqual(bob.get('/api/v1/documents').status_code, 404)
        bob_context = self.app.state.users.for_user(self.b['id'])
        self.assertNotEqual(bob_context.settings.data_dir, context.settings.data_dir)
        with patch('starlette.requests.Request.is_disconnected', new=AsyncMock(side_effect=[False,True])):
            response = bob.get('/api/v1/events?after=0')
        self.assertNotIn('Alice', response.text)

    def test_sse_rechecks_revocation_before_delivering_batch(self):
        login(self.client)
        context = self.app.state.users.for_user(self.a['id'])
        session = self.client.post('/api/v1/sessions', json={}).json()['id']
        context.store.event(session, 'sensitive', {'text':'must not send'})
        original = context.store.events
        token = self.client.cookies.get(cookie_name(self.settings.auth))
        def revoked(*args):
            result = original(*args); self.auth.revoke(token); return result
        with patch.object(context.store, 'events', side_effect=revoked), patch('starlette.requests.Request.is_disconnected', new=AsyncMock(return_value=False)):
            response = self.client.get('/api/v1/events')
        self.assertNotIn('must not send', response.text)

    def test_cookie_switch_is_rejected_before_old_frontend_can_read_new_user_data(self):
        login(self.client)
        response = self.client.get('/api/v1/workspace', headers={'X-FinSight-User':self.b['id']})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['code'], 'IDENTITY_CHANGED')
        self.assertEqual(self.client.get('/api/v1/events?expected_user_id=' + self.b['id']).status_code, 401)

    def test_invite_concurrent_consumption_creates_only_one_account(self):
        token = self.auth.create_invite()
        def consume(username):
            try:
                self.auth.create_user(username, PASSWORD, invite=token, require_invite=True)
                return 'ok'
            except AuthError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(consume, ['invite_one','invite_two']))
        self.assertEqual(results.count('ok'), 1, results)
        self.assertEqual(results.count('REGISTRATION_DENIED'), 1, results)

    def test_logout_keeps_background_job_but_disabling_owner_cancels_it(self):
        login(self.client)
        session = self.client.post('/api/v1/sessions', json={}).json()['id']
        context = self.app.state.users.for_user(self.a['id'])
        host = context.manager.host(session)
        stop = Event()
        job = context.manager.launch_bounded(host.background.launch, 'synthetic research',
            lambda cancel, work_id: cancel.wait(5), cancel=stop, kind='research')
        record = context.manager.runtime.records('work', session)[0]
        self.assertEqual(record['resources']['owner_user_id'], self.a['id'])
        with self.assertRaises(Exception) as denied:
            context.manager.launch_bounded(lambda: None)
        self.assertEqual(denied.exception.code, 'RESEARCH_LIMIT')
        self.assertEqual(self.client.post('/api/v1/auth/logout').status_code, 204)
        self.assertFalse(stop.is_set())
        self.assertIn(job['work_id'], host.background.handles)
        self.auth.update_user(self.a['id'],status='disabled')
        self.assertTrue(stop.wait(4))

    def test_global_queue_is_bounded_across_users_and_idempotency_still_works(self):
        self.settings.auth.queue_global = 1
        a = self.app.state.users.for_user(self.a['id'])
        b = self.app.state.users.for_user(self.b['id'])
        sa = a.store.create_session('a')['id']; sb = b.store.create_session('b')['id']
        command = {'kind':'message','text':'synthetic'}
        first, _ = self.app.state.users.enqueue(a.manager,sa,'same',command)
        duplicate, created = self.app.state.users.enqueue(a.manager,sa,'same',command)
        self.assertFalse(created); self.assertEqual(first['id'],duplicate['id'])
        with self.assertRaises(Exception) as denied:
            self.app.state.users.enqueue(b.manager,sb,'other',command)
        self.assertEqual(denied.exception.code,'QUEUE_FULL')

    def test_server_cookie_and_explicit_origin_policy(self):
        config = self.settings.model_copy(deep=True)
        config.data_dir = self.settings.data_dir / 'server'
        config.auth = AuthSettings(mode='server', allowed_hosts=['app.example.com'], allowed_origins=['https://app.example.com'])
        from tests.mysql_support import isolated_database
        from finagent.config import DatabaseSettings
        from finagent.storage.database import get_database
        isolated_database(self)
        config.database = DatabaseSettings()
        AuthStore(config.data_dir / 'auth', config.auth, database=get_database(config.database)).create_user('alice', PASSWORD)
        app = create_app(config, host_factory=self.factory)
        with TestClient(app, base_url='https://app.example.com') as client:
            client.headers['Origin'] = 'https://app.example.com'
            csrf = client.get('/api/v1/auth/csrf').json()['csrf_token']
            result = client.post('/api/v1/auth/login', json={'identifier':'alice','password':PASSWORD}, headers={'X-CSRF-Token':csrf})
            self.assertEqual(result.status_code, 200)
            self.assertIn('__Host-finsight_session=', result.headers['set-cookie'])
            self.assertIn('Secure', result.headers['set-cookie'])
            self.assertEqual(client.get('/api/v1/workspace').status_code, 200)
            self.assertEqual(client.get('/api/v1/workspace', headers={'Origin':'http://app.example.com'}).status_code, 403)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        from tests.mysql_support import isolated_database
        isolated_database(self)

    def test_migration_is_explicit_preserves_sources_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            settings.harness.workspace = Path(directory)
            settings.memory.embedding_backend = 'disabled'
            auth = AuthStore(settings.data_dir / 'auth', settings.auth)
            owner = auth.create_user('owner', PASSWORD)['id']
            web = WebStore(settings.data_dir / 'web')
            session = web.create_session('old private session')
            host = SessionHost(settings, session['id'], model=ScriptedModel(ModelReply({'role':'assistant','content':'done'})))
            host.memory.write('旧偏好','keep this source verbatim',source='source_1')
            archived = host.context.offload('verbatim archived source', 'history')['full_output']
            host.state['archives'] = [archived]
            host.checkpoint(); host.close()
            self.assertEqual(migrate_legacy(settings,owner)['status'],'dry_run')
            result = migrate_legacy(settings,owner,apply=True)
            self.assertEqual(result['status'],'complete')
            target = settings.data_dir / 'users' / owner
            self.assertEqual(WebStore(target/'web').session(session['id'])['title'],'old private session')
            clone = settings.model_copy(deep=True); clone.data_dir=target; clone.memory.user_id=owner
            restored = SessionHost(clone,session['id'],model=ScriptedModel(ModelReply({'role':'assistant','content':'done'})))
            try:
                self.assertEqual(restored.state['memory_scope']['user_id'],owner)
                self.assertEqual(restored.memory.records()[0]['body'],'keep this source verbatim')
                Path(archived).write_text('old shared directory must not be read')
                self.assertEqual(restored.context.read_output(archived)['text'],'verbatim archived source')
                with self.assertRaises(Exception):
                    restored.context.read_output(str(settings.data_dir/'auth/auth.sqlite3'))
            finally: restored.close()
            self.assertFalse((settings.data_dir/'web/web.sqlite3').exists())
            self.assertEqual(WebStore(settings.data_dir/'web').session(session['id'])['title'], 'old private session')
            self.assertEqual(migrate_legacy(settings,owner,apply=True)['status'],'already_migrated')
            other=auth.create_user('another',PASSWORD)['id']
            (settings.data_dir/'users'/other).mkdir()
            (settings.data_dir/'users'/other/'existing').write_text('keep')
            with self.assertRaises(ValueError): migrate_legacy(settings,other,apply=True)
