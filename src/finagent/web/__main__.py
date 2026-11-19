import argparse


def main():
    parser = argparse.ArgumentParser(description="FinSight 登录与独立用户 Web 工作台")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    import uvicorn
    from ..config import load_settings
    from .app import create_app
    settings = load_settings(args.config)
    if args.host not in {'127.0.0.1', 'localhost', '::1'} and settings.auth.mode != 'server':
        parser.error('外部监听要求配置 auth.mode=server 及 HTTPS 站点 Origin')
    uvicorn.run(create_app(settings), host=args.host, port=args.port,
                workers=1, proxy_headers=False, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
