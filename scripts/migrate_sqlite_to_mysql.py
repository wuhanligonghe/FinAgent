"""Preview by default; --apply requires a stopped deployment and empty target."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from finagent.config import load_settings
from finagent.storage.database import get_database
from finagent.storage.migration import import_sqlite, prepare
from finagent.errors import AgentError


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--backup-dir', type=Path)
    a = p.parse_args()
    settings = load_settings(a.config)
    try:
        if a.apply:
            report = import_sqlite(settings, get_database(settings.database), apply=True, backup_dir=a.backup_dir)
        else:
            _, report = prepare(settings.data_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (AgentError, ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
