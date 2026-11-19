"""Activate the already verified local import and user services.

Requires operator approval for accounts/service installation. Does not import
SQLite or change users/grants; setup_accounts.py and verified import run first.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

base=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(base/'src'))
from finagent.config import load_settings
from finagent.storage.database import get_database, close_databases


def control(action, unit=None):
    args=['systemctl','--user',action]+([unit] if unit else [])
    result=subprocess.run(args,capture_output=True,text=True)
    if result.returncode == 0:
        return
    # Some restricted execution environments report failure after systemd has
    # completed the D-Bus request. Verify the actual postcondition explicitly.
    if action == 'daemon-reload':
        states=[subprocess.run(['systemctl','--user','show',name,'-p','NeedDaemonReload','--value'],capture_output=True,text=True)
            for name in ('finsight-mysql.service','finsight-web.service')]
        okay=all(r.returncode==0 and r.stdout.strip()=='no' for r in states)
    elif action in ('enable','start'):
        probe='is-enabled' if action=='enable' else 'is-active'
        expected='enabled' if action=='enable' else 'active'
        deadline=time.monotonic()+20
        okay=False
        while time.monotonic()<deadline:
            check=subprocess.run(['systemctl','--user',probe,unit],capture_output=True,text=True)
            if check.stdout.strip()==expected:
                okay=True
                break
            time.sleep(.2)
    else:
        okay=False
    if not okay:
        raise RuntimeError('User service operation did not reach its expected state: '+action+' '+str(unit))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    if not a.apply:
        print('Preview: install project MySQL/Web user units, write runtime URL to .env, restart private MySQL and Web.')
        return
    folder=base/'.finagent/services/mysql'
    accounts=json.loads((folder/'accounts.json').read_text())
    if accounts.get('status')!='ready':
        raise ValueError('Account setup incomplete')
    runtime_url=accounts['urls']['finagent_app']
    os.environ['FINAGENT_MYSQL_URL']=runtime_url
    settings=load_settings(base/'finagent.toml')
    db=get_database(settings.database)
    db.check()
    with db.transaction() as s:
        imports=s.rows('storage_import_batches',status='verified')
    if len(imports)!=1:
        raise ValueError('Expected exactly one verified initial import')
    backup=Path(json.loads(imports[0]['manifest'])['backup'])/'activation'
    backup.mkdir(exist_ok=True);backup.chmod(0o700)
    envfile=base/'.env'
    old=envfile.read_bytes() if envfile.exists() else b''
    saved=backup/'env.before-mysql'
    if not saved.exists():
        saved.write_bytes(old);saved.chmod(0o600)
    # Preserve the original source configuration for a coordinated rollback.
    shutil.copy2(base/'finagent.toml',backup/'finagent.toml')
    lines=old.decode().splitlines()
    lines=[line for line in lines if not line.strip().startswith('FINAGENT_MYSQL_URL=')]
    lines+=['','FINAGENT_MYSQL_URL='+runtime_url]
    fd,name=tempfile.mkstemp(prefix='.env.mysql-',dir=base)
    with os.fdopen(fd,'w') as f:
        f.write('\n'.join(lines)+'\n')
    os.chmod(name,0o600);os.replace(name,envfile)
    units=Path.home()/'.config/systemd/user'
    units.mkdir(parents=True,exist_ok=True)
    for name in ('finsight-mysql.service','finsight-web.service'):
        destination=units/name
        source=base/'deploy/systemd'/name
        if destination.exists() or destination.is_symlink():
            before=backup/(name+'.before')
            if not before.exists():
                before.write_bytes(destination.read_bytes())
            if destination.resolve()==source.resolve():
                continue
            destination.unlink()
        destination.symlink_to(source)
    close_databases()
    control('daemon-reload')
    control('enable','finsight-mysql.service')
    # Transition only the dedicated daemon started during this migration.
    active=subprocess.run(['systemctl','--user','is-active','finsight-mysql.service'],capture_output=True,text=True).stdout.strip()=='active'
    socket=folder/'run/mysql.sock'
    if not active and socket.exists():
        pid=int((folder/'run/mysql.pid').read_text())
        command=(Path('/proc')/str(pid)/'cmdline').read_bytes().decode().split('\0')
        if '--datadir='+str(folder/'data') not in command:
            raise ValueError('PID does not belong to the project MySQL instance')
        os.kill(pid,signal.SIGTERM)
        deadline=time.monotonic()+45
        while socket.exists() and time.monotonic()<deadline:
            time.sleep(.2)
        if socket.exists():
            raise RuntimeError('MySQL did not stop within the maintenance timeout')
    control('start','finsight-mysql.service')
    control('start','finsight-web.service')
    print('Activated runtime URL and MySQL/Web user services; credentials were not printed.')

if __name__=='__main__':
    main()
