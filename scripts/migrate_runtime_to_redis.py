#!/usr/bin/env python3
"""Preview/import existing CLI and authenticated Web runtimes into Redis."""
import argparse
import json

from finagent.config import load_settings
from finagent.harness.runtime_migration import migrate_runtimes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(migrate_runtimes(load_settings(args.config), apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
