"""Consistent logical backup/restore for this schema; coordinate Redis/files separately.

Export requires --output. Restore requires --restore FILE --apply and an empty,
pre-upgraded target. No database/account creation or destructive cleanup occurs.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

from sqlalchemy import select, text
base=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(base/'src'))
from finagent.config import load_settings
from finagent.storage.database import get_database, REVISION
from finagent.storage.schema import metadata


def encode(v):
    return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--output',type=Path)
    group.add_argument('--restore',type=Path)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    db=get_database(load_settings().database)
    db.check()
    if a.output:
        # REPEATABLE READ keeps every table at one MySQL transaction snapshot.
        with db.engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
            with c.begin():
                tables={t.name:[dict(r) for r in c.execute(select(t)).mappings()] for t in metadata.sorted_tables}
        content={'schema_revision':REVISION,'tables':tables}
        payload={**content,'sha256':sha256(encode(content).encode()).hexdigest()}
        fd=os.open(a.output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as f:
            f.write(encode(payload))
        print('MySQL backup written with mode 0600; '+str(sum(len(v) for v in tables.values()))+' records.')
        return
    payload=json.loads(a.restore.read_text())
    digest=payload.pop('sha256')
    if payload.get('schema_revision') != REVISION or sha256(encode(payload).encode()).hexdigest()!=digest:
        raise ValueError('Backup revision or digest mismatch')
    if set(payload['tables']) != set(metadata.tables):
        raise ValueError('Backup table set mismatch')
    if not a.apply:
        print('Restore preview: '+str(sum(len(v) for v in payload['tables'].values()))+' records; use --apply for an empty target.')
        return
    with db.engine.begin() as c:
        for t in metadata.sorted_tables:
            if c.execute(select(t).limit(1)).first():
                raise ValueError('Target must be empty: '+t.name)
        for t in metadata.sorted_tables:
            rows=payload['tables'][t.name]
            # Compare keys first; prevent partial/malformed schema exports.
            if any(set(r)!=set(t.c.keys()) for r in rows):
                raise ValueError('Backup columns mismatch: '+t.name)
            for offset in range(0,len(rows),250):
                c.execute(t.insert(),rows[offset:offset+250])
        for t in metadata.sorted_tables:
            actual=[dict(r) for r in c.execute(select(t)).mappings()]
            if sorted(map(encode,actual)) != sorted(map(encode,payload['tables'][t.name])):
                raise ValueError('Restore verification failed: '+t.name)
    print('Restore committed; every record verified against the backup.')

if __name__=='__main__':
    main()
