#!/usr/bin/env python3
"""Run the local primary runtime Redis with AOF durability and no eviction."""
import argparse
import os
from pathlib import Path
import shutil


def main():
    from finagent.config import load_settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--port', type=int, default=6379)
    parser.add_argument('--binary')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Redis 端口无效')
    settings = load_settings(args.config)
    bundled = settings.data_dir / 'services/redis-dist/usr/bin/redis-server'
    executable = args.binary or shutil.which('redis-server') or (str(bundled) if bundled.exists() else None)
    if not executable:
        parser.error('未找到 redis-server，请安装 Redis 或通过 --binary 指定')
    # Preserve argv[0]: Debian's redis-server is a symlink to a multi-call binary.
    executable = str(Path(executable).absolute())
    directory = settings.data_dir / 'services/redis'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    environment = dict(os.environ)
    libraries = settings.data_dir / 'services/redis-dist/usr/lib/x86_64-linux-gnu'
    if libraries.is_dir() and str(bundled.parent.parent.resolve()) in executable:
        environment['LD_LIBRARY_PATH'] = str(libraries)
    command = [executable, '--bind', '127.0.0.1', '--protected-mode', 'yes', '--port', str(args.port),
               '--dir', str(directory), '--appendonly', 'yes', '--appendfsync', 'always', '--save', '',
               '--maxmemory-policy', 'noeviction', '--daemonize', 'no']
    os.execve(executable, command, environment)


if __name__ == '__main__':
    main()
