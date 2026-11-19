#!/usr/bin/env python3
"""Readiness check without opening or modifying any runtime namespace."""
import argparse
import time

import redis

from finagent.config import load_settings
from finagent.harness.state import redis_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--wait', type=float, default=0)
    args = parser.parse_args()
    settings = load_settings(args.config).runtime
    client = redis_client(settings.redis_url.get_secret_value(), min(settings.connect_timeout, .5), min(settings.socket_timeout, .5))
    deadline = time.monotonic() + min(max(args.wait, 0), 30)
    while True:
        try:
            client.ping()
            print('Redis runtime ready')
            return
        except redis.RedisError:
            if time.monotonic() >= deadline:
                parser.exit(1, 'Redis runtime unavailable\n')
            time.sleep(.1)


if __name__ == '__main__':
    main()
