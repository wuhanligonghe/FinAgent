"""Run the project's existing MySQL data directory on a private Unix socket."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

base = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--mysqld', default=os.environ.get('FINAGENT_MYSQLD') or shutil.which('mysqld'))
a = p.parse_args()
if not a.mysqld:
    p.error('Provide --mysqld /path/to/MySQL-8.4/bin/mysqld')
folder = base / '.finagent/services/mysql'
if not (folder / 'data/mysql').is_dir():
    p.error('Initialize a dedicated MySQL data directory before starting this service')
folder.chmod(0o700)
(folder / 'run').mkdir(exist_ok=True)
args = [a.mysqld, '--no-defaults', '--basedir=' + str(Path(a.mysqld).resolve().parent.parent),
    '--datadir=' + str(folder/'data'), '--socket=' + str(folder/'run/mysql.sock'),
    '--pid-file=' + str(folder/'run/mysql.pid'), '--skip-networking', '--mysqlx=OFF',
    '--character-set-server=utf8mb4', '--collation-server=utf8mb4_bin', '--innodb-flush-log-at-trx-commit=1',
    '--log-error=' + str(folder/'server.log')]
os.execv(args[0], args)
