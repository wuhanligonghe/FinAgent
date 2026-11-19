"""Run against a disposable MySQL database; never touches runtime accounts."""
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

base = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(base / 'src'))
from finagent.storage.schema import metadata
from finagent.storage.database import REVISION

url = os.environ.get('FINAGENT_MYSQL_URL')
if not url:
    raise SystemExit('Set FINAGENT_MYSQL_URL to a disposable finagent_test* database')
parsed = make_url(url)
if not parsed.database or not parsed.database.startswith('finagent_test'):
    raise SystemExit('Refusing to initialize a database without the finagent_test prefix')
admin = create_engine(parsed.set(database=""), hide_parameters=True)
with admin.begin() as c:
    c.execute(text('CREATE DATABASE IF NOT EXISTS `' + parsed.database + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'))
admin.dispose()
engine = create_engine(parsed, hide_parameters=True)
metadata.create_all(engine)
with engine.begin() as c:
    c.execute(text('CREATE TABLE IF NOT EXISTS alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY)'))
    c.execute(text('INSERT IGNORE INTO alembic_version VALUES (:revision)'), {'revision':REVISION})
engine.dispose()
args = sys.argv[1:] or ['discover', '-s', 'tests', '-t', '.', '-q']
raise SystemExit(subprocess.call([sys.executable, '-m', 'unittest', *args], cwd=base))
