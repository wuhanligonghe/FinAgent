"""Upgrade using an explicitly supplied maintenance URL; never creates users."""
import argparse
import os
from pathlib import Path
import re
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config

base = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(base / 'src'))
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--create-database', action='store_true')
a = p.parse_args()
raw = os.environ.get('FINAGENT_MYSQL_URL')
if not raw:
    p.error('Set FINAGENT_MYSQL_URL to the maintenance connection URL')
url = make_url(raw)
if url.drivername != 'mysql+pymysql' or not url.database or not re.fullmatch(r'[A-Za-z0-9_]{1,64}', url.database):
    p.error('Expected mysql+pymysql URL with an explicit database name')
if a.create_database:
    engine = create_engine(url.set(database=""), hide_parameters=True)
    with engine.begin() as c:
        c.execute(text('CREATE DATABASE IF NOT EXISTS `' + url.database + '` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'))
    engine.dispose()
command.upgrade(Config(str(base/'alembic.ini')), 'head')
print('MySQL schema upgraded; no users, grants or passwords changed.')
