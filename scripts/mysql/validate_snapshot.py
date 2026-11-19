"""Validate real business data through an isolated copy, without stopping Web.

This is a migration rehearsal, not a coordinated live-data cutover.
Requires an empty finagent_test* MySQL database initialized with upgrade.py.
"""
from pathlib import Path
import json
import os
import sqlite3
import sys
import tempfile

from sqlalchemy.engine import make_url
base=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(base/'src'))
from finagent.config import load_settings
from finagent.storage.database import get_database
from finagent.storage.migration import discover, copy_tree, import_sqlite

url=make_url(os.environ['FINAGENT_MYSQL_URL'])
if not url.database or not url.database.startswith('finagent_test'):
    raise SystemExit('Rehearsal requires an empty finagent_test* database')
settings=load_settings()
original=settings.data_dir.resolve()
clone=Path(tempfile.mkdtemp(prefix='finagent-mysql-rehearsal-'))
clone.chmod(0o700)
for item in discover(original):
    copy_tree(item['path'],clone/item['path'].relative_to(original))
for root in {item['root'] for item in discover(original)}:
    if (root/'research').exists():
        copy_tree(root/'research',clone/root.relative_to(original)/'research')
    path=clone/root.relative_to(original)/'research.sqlite3'
    if path.exists():
        from contextlib import closing
        with closing(sqlite3.connect(path)) as c,c:
            rows=list(c.execute('SELECT id,report FROM runs WHERE report IS NOT NULL'))
            for identifier,report in rows:
                selected=Path(report)
                if not selected.is_relative_to(original):
                    raise ValueError('Report outside original workspace')
                c.execute('UPDATE runs SET report=? WHERE id=?',(str(clone/selected.relative_to(original)),identifier))
settings.data_dir=clone
result=import_sqlite(settings,get_database(settings.database),apply=True)
print(json.dumps({'status':result['status'],'rehearsal_root':str(clone),'backup':result['backup'],
    'tables':result['sources']},ensure_ascii=False,indent=2))
