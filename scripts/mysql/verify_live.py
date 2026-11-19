"""Read-only application smoke, plus a temporary authentication session removed afterward."""
from hashlib import sha256
import json
from pathlib import Path
import sys

import httpx
from sqlalchemy import text
base=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(base/'src'))
from finagent.config import load_settings
from finagent.storage.database import get_database
from finagent.storage.migration import discover
from finagent.web.auth_store import AuthStore, digest
from finagent.web.auth import cookie_name
from finagent.web.store import WebStore
from finagent.web.user_runtime import user_settings
from finagent.research.store import ResearchStore

settings=load_settings(base/'finagent.toml')
db=get_database(settings.database)
db.check()
sources=[s['path'] for s in discover(settings.data_dir)]
before={str(p):sha256(p.read_bytes()).hexdigest() for p in sources}
with db.engine.connect() as c:
    current=c.scalar(text('SELECT CURRENT_USER()'))
    grants='\n'.join(r[0] for r in c.execute(text('SHOW GRANTS FOR CURRENT_USER()')))
if not current.startswith('finagent_app@') or any(x in grants.upper() for x in ('GRANT OPTION','ALL PRIVILEGES','CREATE','ALTER','DROP')):
    raise ValueError('Runtime connection is not using the limited application account')
auth=AuthStore(settings.data_dir/'auth',settings.auth,database=db)
users=auth.users()
if not users:
    raise ValueError('No migrated accounts found')
user=auth.user(users[0]['id'])
clone=user_settings(settings,user['id'])
store=WebStore(clone.data_dir/'web',database=db)
expected=len(store.all_sessions())
token,_=auth.new_session(user)
try:
    with httpx.Client(base_url='http://127.0.0.1:8000',trust_env=False,timeout=10) as client:
        health=client.get('/api/v1/health')
        if health.status_code!=200:
            raise ValueError('Health endpoint failed')
        if client.get('/api/v1/sessions').status_code!=401:
            raise ValueError('Private endpoint must reject anonymous access')
        client.cookies.set(cookie_name(settings.auth),token,domain='127.0.0.1',path='/')
        response=client.get('/api/v1/sessions')
        response.raise_for_status()
        if len(response.json()['items'])!=expected:
            raise ValueError('HTTP history does not match migrated namespace')
        client.get('/api/v1/workspace').raise_for_status()
finally:
    with auth.db() as s:
        s.remove('auth_sessions',token_hash=digest(token))
research=ResearchStore.from_settings(settings)
with research.connect() as s:
    runs=s.rows('research_runs')
    reservations=s.count('research_reservations')
artifacts=0
for run in runs:
    research.snapshot(run['id'])
    for task in research.tasks(run['id']):
        if task.get('artifact'):
            research.read_artifact(task)
            artifacts+=1
after={str(p):sha256(p.read_bytes()).hexdigest() for p in sources}
if before!=after:
    raise ValueError('Legacy SQLite changed during MySQL runtime checks')
print(json.dumps(dict(status='ok',runtime_account=current,limited_runtime_privileges=True,
    authenticated_web_sessions=expected,research_runs=len(runs),research_reservations=reservations,
    checked_artifacts=artifacts,legacy_sqlite_unchanged=True),ensure_ascii=False,indent=2))
