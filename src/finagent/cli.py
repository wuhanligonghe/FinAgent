"""One persistent Agent entrypoint, plus explicitly separate maintenance/research."""
from __future__ import annotations
import argparse
import json
import sys
from .config import load_settings
from .errors import AgentError
from .harness.host import SessionHost
from .harness.terminal import run_terminal

MAINTENANCE = {'research','research-status','research-report','research-resume','research-trace',
 'import-policy','import-research-signal','nlu-evaluate','nlu-check','nlu-parse','nlu-benchmark','intent-parse',
 'tool','import-market','legacy-run','session','demo','evidence','probe','evaluate'}

def _main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description='FinSight v3 持续 Agent（基于 learn-claude-code Harness）')
    parser.add_argument('--config')
    parser.add_argument('--session',default='default',help='持久会话名称')
    parser.add_argument('--resume',help='恢复已有会话，不是旧 run ID')
    parser.add_argument('command',nargs='?',help='可选独立命令：research、tools、nlu-check 等')
    args, rest = parser.parse_known_args(values)
    if args.command == 'auth':
        from .web.auth_cli import main as auth_main
        from .web.auth_store import AuthError
        try:
            return auth_main(rest, load_settings(args.config))
        except EOFError:
            print('未读取到密码，账号操作已取消', file=sys.stderr)
            return 2
        except (AuthError, AgentError, ValueError, OSError) as exc:
            print(exc.message if isinstance(exc, AuthError) else str(exc), file=sys.stderr)
            return 2
    if args.command in {'run','chat','resume'}:
        print('旧单次 run/chat 入口已移除。直接运行 finagent 进入持续会话；恢复用 --resume SESSION_ID，深度研究用 research。')
        return 2
    if args.command in MAINTENANCE:
        from .maintenance import main as maintenance
        prefix = ['--config',args.config] if args.config else []
        try:
            return maintenance(prefix + [args.command] + rest, settings_override=load_settings(args.config))
        except (AgentError,ValueError,OSError) as exc:
            print(str(exc),file=sys.stderr)
            return 2
    if rest or args.command not in {None,'tools'}:
        parser.error('未知参数或命令；普通问答请直接进入持续会话')
    try:
        settings = load_settings(args.config)
        from uuid import uuid4
        host = SessionHost(settings,("inspect_" + uuid4().hex[:8]) if args.command=='tools' else (args.resume or args.session),write=print)
        if args.command=='tools':
            try:
                print(json.dumps(host.pool.schemas(),ensure_ascii=False,indent=2))
            finally:
                host.close()
            return 0
        if args.resume and host.store.load(args.resume) is None:
            host.close()
            raise ValueError('会话不存在；旧运行记录使用 legacy-run 查看')
        try:
            return run_terminal(settings,host=host)
        finally:
            host.close()
    except (AgentError,ValueError,OSError) as exc:
        print(str(exc),file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

def main(argv=None):
    from .storage.database import close_databases
    try:
        return _main(argv)
    finally:
        close_databases()

if __name__=='__main__':
    raise SystemExit(main())
