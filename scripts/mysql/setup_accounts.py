"""Operator-approved local account setup; review before executing.

Creates only finagent_app and finagent_maintenance on FinAgent's private server.
Does not alter the root password or any other project's MySQL accounts.
"""
import json
import os
from pathlib import Path
import secrets
from sqlalchemy.engine import make_url, URL
import pymysql

base = Path(__file__).resolve().parents[2]
folder = base / '.finagent/services/mysql'
raw = os.environ.get('FINAGENT_MYSQL_ADMIN_URL')
if not raw:
    raise SystemExit('Set FINAGENT_MYSQL_ADMIN_URL; this action requires operator approval.')
url = make_url(raw)
socket = str(folder/'run/mysql.sock')
if url.query.get('unix_socket') != socket or url.database != 'finagent':
    raise SystemExit('Refusing to change accounts outside the dedicated FinAgent server/database')
output = folder/'accounts.json'
if output.exists():
    raise SystemExit('Account credential file already exists; refusing to overwrite')
passwords = {name:secrets.token_urlsafe(32) for name in ('finagent_app','finagent_maintenance')}
folder.chmod(0o700)
# Persist the recovery material before CREATE USER, without printing secrets.
fd=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
with os.fdopen(fd,'w') as f:
    json.dump({'status':'pending','passwords':passwords},f)
c=pymysql.connect(unix_socket=socket,user=url.username,password=url.password or '',autocommit=True)
try:
    with c.cursor() as q:
        for user,password in passwords.items():
            q.execute('CREATE USER %s@localhost IDENTIFIED BY %s',(user,password))
        q.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON finagent.* TO finagent_app@localhost')
        q.execute('GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX,DROP,REFERENCES ON finagent.* TO finagent_maintenance@localhost')
    urls={user:URL.create('mysql+pymysql',username=user,password=password,database='finagent',
        query={'unix_socket':socket,'charset':'utf8mb4'}).render_as_string(hide_password=False) for user,password in passwords.items()}
    output.write_text(json.dumps({'status':'ready','urls':urls}))
    output.chmod(0o600)
    print('Created runtime and maintenance accounts; secrets are in the private accounts.json file.')
finally:
    c.close()
