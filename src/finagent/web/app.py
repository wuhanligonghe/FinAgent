import asyncio
from contextlib import asynccontextmanager
import ipaddress
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import load_settings
from ..storage.database import get_database
from ..data.evidence import EvidenceStore
from ..errors import AgentError
from ..research.store import ResearchStore
from .auth import router as auth_router, cookie_name, auth_response_error
from .auth_store import AuthStore, AuthError
from .user_runtime import UserRuntimeRegistry
from ..harness.state import RuntimeStore
from .schemas import MessageCreate, ResearchCreate, SessionCreate
from .store import encode


def create_app(settings=None, *, host_factory=None, testing=False):
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app):
        if (settings.data_dir / "auth").is_symlink():
            raise ValueError("认证目录不能是符号链接")
        lease = RuntimeStore(settings.data_dir / "web_server", settings=settings.runtime).lease("finsight_web_server")
        lease.__enter__()
        registry = None
        try:
            app.state.auth = AuthStore(settings.data_dir / "auth", settings.auth, database=get_database(settings.database))
            registry = UserRuntimeRegistry(settings, app.state.auth, host_factory)
            app.state.users = registry
            await asyncio.to_thread(registry.start)
            yield
        finally:
            if registry:
                await asyncio.to_thread(registry.close)
            if hasattr(app.state, "auth"):
                app.state.auth.database.close()
            lease.__exit__(None, None, None)

    app = FastAPI(title="FinSight API", version="0.2.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(auth_router)

    @app.exception_handler(AuthError)
    async def auth_error(request, exc):
        return auth_response_error(request, exc)

    @app.middleware("http")
    async def local_boundary(request, call_next):
        request.state.request_id = uuid4().hex
        hostname = request.url.hostname
        allowed_hosts = set(settings.auth.allowed_hosts) | ({"testserver"} if testing else set())
        client = request.client.host if request.client else ""
        try:
            local = ipaddress.ip_address(client).is_loopback
        except ValueError:
            local = testing and client == "testclient"
        origin = request.headers.get("origin")
        allowed_origins = set(settings.auth.allowed_origins)
        if settings.auth.mode == 'local':
            allowed_origins.update({str(request.base_url).rstrip("/"), "http://127.0.0.1:5173", "http://localhost:5173"})
        try:
            public_navigation = (not request.url.path.startswith('/api/') and request.method in {'GET','HEAD'}
                                 and request.headers.get('sec-fetch-mode') == 'navigate')
            if (hostname not in allowed_hosts or (settings.auth.mode == 'local' and not local)
                    or (origin and origin not in allowed_origins)
                    or (request.headers.get('sec-fetch-site') == 'cross-site' and not public_navigation)):
                raise AuthError('LOCAL_ONLY', '仅允许受信任站点访问', 403)
            path = request.url.path
            if path.startswith('/api/'):
                token = request.cookies.get(cookie_name(settings.auth))
                session = await asyncio.to_thread(app.state.auth.resolve, token, touch=path != '/api/v1/events')
                request.state.auth_session = session
                public = path in {'/api/v1/health', '/api/v1/auth/csrf', '/api/v1/auth/login', '/api/v1/auth/register'}
                if not public and (not session or not session['user_id']):
                    raise AuthError('UNAUTHENTICATED', '请先登录')
                expected = request.headers.get('x-finsight-user')
                if path == '/api/v1/events':
                    expected = request.query_params.get('expected_user_id') or expected
                if not public and not path.startswith('/api/v1/auth/') and expected and expected != session['user_id']:
                    raise AuthError('IDENTITY_CHANGED', '登录身份已变化，请重新加载工作区')
                if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                    from urllib.parse import urlsplit
                    referer = urlsplit(request.headers.get('referer', ''))
                    source = origin or (f'{referer.scheme}://{referer.netloc}' if referer.netloc else '')
                    if source not in allowed_origins:
                        raise AuthError('ORIGIN_DENIED', '请求来源验证失败', 403)
                    app.state.auth.check_csrf(session, request.headers.get('x-csrf-token'))
                if not public and not path.startswith('/api/v1/auth/'):
                    request.state.user_context = await asyncio.to_thread(app.state.users.for_user, session['user_id'])
            response = await call_next(request)
        except AuthError as exc:
            response = auth_response_error(request, exc)
        except AgentError as exc:
            if not exc.code.startswith("DATABASE_"):
                raise
            response = JSONResponse({"code":exc.code,"message":exc.message,"request_id":request.state.request_id},status_code=503)
        response.headers['X-Request-ID'] = request.state.request_id
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(AgentError)
    async def agent_error(request, exc):
        status = 404 if exc.code in {"NOT_FOUND", "RESEARCH_NOT_FOUND", "EVIDENCE_NOT_FOUND"} else 409
        if exc.code.startswith("DATABASE_") or exc.code in {"RUNTIME_UNAVAILABLE", "RUNTIME_BUSY", "RUNTIME_DATA_MISSING"}:
            status = 503
        if exc.code in {"RESEARCH_DENIED", "REPORT_DENIED", "PERMISSION_DENIED", "MEMORY_DENIED"}:
            status = 403
        if exc.code in {"INVALID_ID", "INVALID_RESEARCH_ID", "INVALID_DOCUMENT_ID", "INVALID_ARGUMENTS"}:
            status = 422
        return JSONResponse({"code": exc.code, "message": exc.message, "request_id": request.state.request_id}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"code": "VALIDATION_ERROR", "message": "请求字段无效，请检查输入", "request_id": request.state.request_id}, status_code=422)

    def page(items):
        return {"items": items, "next_cursor": items[-1]["seq"] if items else None}

    def key(value):
        if not value or len(value) > 160:
            raise AgentError("INVALID_ARGUMENTS", "提交操作必须提供不超过160字符的 Idempotency-Key")
        return value

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "mode": "authenticated_multi_user", "token_streaming": False}

    @app.get("/api/v1/settings")
    def public_settings():
        return {"mode": "独立用户工作区", "model_configured": bool(settings.model.name and settings.model.base_url),
            "model_proxy_configured": bool(settings.model.proxy_url),
            "token_streaming": False, "web_tools": "金融研究工具；文件系统、Shell 和 MCP 未开放"}

    @app.get("/api/v1/workspace")
    def workspace(request: Request):
        return {"sessions": request.state.user_context.store.sessions(limit=8), "research": request.state.user_context.manager.research_jobs()[:8]}

    @app.post("/api/v1/sessions", status_code=201)
    def create_session(body: SessionCreate, request: Request):
        return request.state.user_context.store.create_session(body.title)

    @app.get("/api/v1/sessions")
    def sessions(request: Request, before: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return page(request.state.user_context.store.sessions(before, limit))

    @app.get("/api/v1/sessions/{session_id}/messages")
    def messages(session_id: str, request: Request, before: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        runs = request.state.user_context.store.runs(session_id, before, limit)
        items = [{**request.state.user_context.manager.public_run(run), "text": run["command"].get("text"), "kind": run["command"]["kind"]} for run in runs]
        return {"session": request.state.user_context.store.session(session_id), "items": items, "next_cursor": items[0]["seq"] if items else None}

    @app.post("/api/v1/sessions/{session_id}/messages", status_code=202)
    def submit_message(session_id: str, body: MessageCreate, request: Request, idempotency_key: str | None = Header(None)):
        return request.state.user_context.manager.submit(session_id, key(idempotency_key), {"kind": "message", "text": body.text})

    @app.get("/api/v1/sessions/{session_id}/memories")
    def memories(session_id: str, request: Request):
        manager = request.state.user_context.manager
        with manager.session_lock(session_id):
            host = manager.host(session_id)
            return {"enabled": host.settings.harness.memory_enabled,
                    "scope": host.state["memory_scope"], "items": host.memory.records()}

    @app.delete("/api/v1/sessions/{session_id}/memories/{memory_id}")
    def forget_memory(session_id: str, memory_id: str, request: Request):
        manager = request.state.user_context.manager
        with manager.session_lock(session_id):
            host = manager.host(session_id)
            try:
                result = host.memory.forget(memory_id)
            except FileNotFoundError:
                raise AgentError("NOT_FOUND", "记忆不存在") from None
            host.recalled = [r for r in host.recalled if r["id"] != memory_id]
            return result

    @app.get("/api/v1/runs/{run_id}")
    def run(run_id: str, request: Request):
        return request.state.user_context.manager.public_run(request.state.user_context.store.run(run_id))

    @app.post("/api/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: str, request: Request):
        return request.state.user_context.manager.cancel(run_id)

    @app.get("/api/v1/events")
    async def events(request: Request, after: int = Query(0, ge=0), session_id: str | None = None, last_event_id: str | None = Header(None)):
        store = request.state.user_context.store
        if session_id:
            store.session(session_id)
        try:
            cursor = max(after, int(last_event_id or 0))
        except ValueError as exc:
            raise AgentError("INVALID_ARGUMENTS", "事件游标无效") from exc
        async def stream():
            nonlocal cursor
            yield "retry: 2000\n\n"
            ticks = 0
            token = request.cookies.get(cookie_name(settings.auth))
            owner = request.state.auth_session['user_id']
            while not await request.is_disconnected():
                identity = await asyncio.to_thread(request.app.state.auth.resolve, token)
                if not identity or identity['user_id'] != owner:
                    return
                batch = await asyncio.to_thread(store.events, cursor, session_id)
                for item in batch:
                    identity = await asyncio.to_thread(request.app.state.auth.resolve, token)
                    if not identity or identity['user_id'] != owner:
                        return
                    cursor = item["sequence"]
                    yield f"id: {cursor}\ndata: {encode(item)}\n\n"
                ticks += 1
                if ticks % 30 == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    @app.post("/api/v1/research", status_code=202)
    def create_research(body: ResearchCreate, request: Request, idempotency_key: str | None = Header(None)):
        return request.state.user_context.manager.submit(body.session_id, key(idempotency_key), {"kind": "research", **body.model_dump(mode="json", exclude={"session_id"})})

    @app.get("/api/v1/research")
    def research_list(request: Request):
        return {"items": request.state.user_context.manager.research_jobs()}

    @app.get("/api/v1/research/{reference}")
    def research_status(reference: str, request: Request):
        job = request.state.user_context.manager.research_job(reference)
        if not job["research_id"]:
            return {**job, "stages": [], "tasks": []}
        state = ResearchStore.from_settings(request.state.user_context.settings).snapshot(job["research_id"])
        return {**job, "execution_status": state["status"], "content_status": state["content"], "review_status": state["review"],
            "stages": [{field: row.get(field) for field in ("role", "stage", "status", "reason")} for row in state["stages"]],
            "tasks": [{field: row.get(field) for field in ("id", "role", "kind", "status", "reason")} for row in state["tasks"]]}

    @app.post("/api/v1/research/{reference}/cancel")
    def cancel_research(reference: str, request: Request):
        return request.state.user_context.manager.cancel_research(reference)

    @app.post("/api/v1/research/{research_id}/resume", status_code=202)
    def resume_research(research_id: str, request: Request, idempotency_key: str | None = Header(None)):
        session_id = request.state.user_context.manager.report_owner(research_id)
        return request.state.user_context.manager.submit(session_id, key(idempotency_key), {"kind": "resume", "research_id": research_id})

    @app.get("/api/v1/research/{research_id}/report")
    def report(research_id: str, request: Request):
        return request.state.user_context.reports.load(research_id)

    @app.get("/api/v1/research/{research_id}/citations/{citation_id}")
    def citation(research_id: str, citation_id: str, request: Request):
        report = request.state.user_context.reports.load(research_id)
        reference = next((item for item in report["citations"] if item["id"] == citation_id), None)
        if reference is None:
            raise AgentError("NOT_FOUND", "引用不存在")
        observation = EvidenceStore(request.state.user_context.settings.data_dir).load_result(reference["result_id"])
        blocks = observation.data.get("blocks", []) if isinstance(observation.data, dict) else []
        excerpts = [block.get("text", "") for block in blocks if block.get("locator") == reference["locator"]]
        return {**reference, "excerpt": "\n".join(excerpts)[:8000] or None, "fetched_at": str(observation.fetched_at or ""),
                "note": "定位来自原始证据；未找到精确片段时请查看原文，不补造页码。"}

    @app.get("/api/v1/research/{research_id}/export")
    def export(research_id: str, request: Request, format: str = "markdown"):
        if format != "markdown":
            raise AgentError("INVALID_ARGUMENTS", "当前仅支持 Markdown 导出")
        report = request.state.user_context.reports.load(research_id)
        return Response(report["markdown"], media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="finsight-{research_id}.md"'})

    @app.post("/api/v1/research/{research_id}/questions", status_code=202)
    def question(research_id: str, body: MessageCreate, request: Request, idempotency_key: str | None = Header(None)):
        session_id = request.state.user_context.manager.report_owner(research_id)
        request.state.user_context.reports.load(research_id)
        return request.state.user_context.manager.submit(session_id, key(idempotency_key), {"kind": "question", "text": body.text, "research_id": research_id})

    frontend = Path(__file__).resolve().parents[3] / "web" / "dist"
    if (frontend / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend_page(path: str):
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"code": "NOT_FOUND", "message": "接口不存在"}, status_code=404)
        if (frontend / "index.html").is_file():
            return FileResponse(frontend / "index.html")
        return JSONResponse({"message": "请先在 web/ 执行 npm install 和 npm run build，或启动 Vite 开发服务。"}, status_code=503)

    return app
