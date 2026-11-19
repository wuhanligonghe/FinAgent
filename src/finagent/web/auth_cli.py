"""Operator-only account management; passwords are read interactively."""
import argparse
from getpass import getpass
import json

from .auth_store import AuthStore


def main(argv, settings):
    parser = argparse.ArgumentParser(prog='finagent auth', description='服务器账号管理，不接受命令行明文密码')
    commands = parser.add_subparsers(dest='action', required=True)
    create = commands.add_parser('create-user')
    create.add_argument('username'); create.add_argument('--email'); create.add_argument('--display-name')
    commands.add_parser('list-users')
    invite = commands.add_parser('invite'); invite.add_argument('--email'); invite.add_argument('--hours', type=int, default=48)
    for action in ('reset-password', 'disable', 'enable'):
        commands.add_parser(action).add_argument('user_id')
    migrate = commands.add_parser('migrate-legacy'); migrate.add_argument('--owner', required=True); migrate.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    from ..storage.database import get_database
    store = AuthStore(settings.data_dir / 'auth', settings.auth, database=get_database(settings.database))
    if args.action in {'create-user', 'reset-password'}:
        first, second = getpass('密码（至少15字符）: '), getpass('再次输入: ')
        if first != second:
            raise ValueError('两次密码不一致')
        if args.action == 'create-user':
            result = store.create_user(args.username, first, args.email, args.display_name)
        else:
            store.update_user(args.user_id, password=first); result = {'status':'password_reset', 'user_id':args.user_id}
    elif args.action == 'list-users':
        result = store.users()
    elif args.action == 'invite':
        if not 1 <= args.hours <= 720:
            raise ValueError('邀请码有效期须为1至720小时')
        result = {'invite':store.create_invite(args.email, args.hours), 'note':'单次使用，请通过受信任渠道交付'}
    elif args.action in {'disable', 'enable'}:
        store.update_user(args.user_id, status='disabled' if args.action == 'disable' else 'active')
        result = {'status':args.action, 'user_id':args.user_id}
    else:
        from .migration import migrate_legacy
        result = migrate_legacy(settings, args.owner, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
