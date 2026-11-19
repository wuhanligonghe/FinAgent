"""Real MySQL isolation for tests that own a deployment-wide authentication DB."""
import os
from uuid import uuid4
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from finagent.storage.database import close_databases, REVISION
from finagent.storage.schema import metadata


def isolated_database(case):
    original = make_url(os.environ['FINAGENT_MYSQL_URL'])
    if not original.database or not original.database.startswith('finagent_test'):
        raise RuntimeError('Authentication tests require a disposable finagent_test* database')
    name = 'finagent_test_' + uuid4().hex
    admin = create_engine(original, hide_parameters=True)
    with admin.begin() as c:
        c.execute(text('CREATE DATABASE `' + name + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'))
    url = original.set(database=name)
    engine = create_engine(url, hide_parameters=True)
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)'))
        c.execute(text('INSERT INTO alembic_version VALUES (:revision)'), {'revision':REVISION})
    engine.dispose()
    env = patch.dict(os.environ, {'FINAGENT_MYSQL_URL':url.render_as_string(hide_password=False)})
    env.start()
    def cleanup():
        close_databases()
        env.stop()
        with admin.begin() as c:
            c.execute(text('DROP DATABASE `' + name + '`'))
        admin.dispose()
    case.addCleanup(cleanup)
