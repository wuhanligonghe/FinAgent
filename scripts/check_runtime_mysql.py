"""Wait for the configured runtime account and schema, without logging secrets."""
import argparse
from pathlib import Path
import sys
import time
base = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(base/'src'))
from finagent.config import load_settings
from finagent.storage.database import get_database
from finagent.errors import AgentError

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--wait',type=float,default=10)
a=p.parse_args()
deadline=time.monotonic()+a.wait
last='DATABASE_UNAVAILABLE'
while True:
    try:
        db=get_database(load_settings().database)
        db.checked=False
        db.check()
        db.close()
        print('MySQL runtime connection and schema ready')
        break
    except AgentError as error:
        last=error.code
        if time.monotonic() >= deadline:
            raise SystemExit(last)
        time.sleep(.2)
