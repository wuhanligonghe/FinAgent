from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
from threading import Event, RLock, Thread
from time import sleep

from ..errors import AgentError
from ..harness.host import SessionHost
from ..harness.progress import ToolProgressReporter
from ..harness.research import ResearchService, launch_research, read_report
from ..harness.state import RuntimeStore
from ..runtime import now
from .diagnostics import failure_diagnostic


logger = logging.getLogger(__name__)


class RunCancellation(Event):
    def __init__(self, cancelled):
        super().__init__()
        self.cancelled = cancelled

    def clear(self):
        if self.cancelled():
            self.set()
        else:
            super().clear()


class SessionManager:
    def __init__(self, settings, store, host_factory=SessionHost, *, executor=None, resources=None):
        self.settings = settings.model_copy(deep=True)
        self.settings.harness.mcp_servers = {}
        self.store, self.host_factory = store, host_factory
        self.runtime = RuntimeStore(settings.data_dir / "v3", settings=settings.runtime)
        self.hosts, self.locks, self.active = {}, {}, {}
        self.lock, self.stopped = RLock(), Event()
        self.owns_executor = executor is None
        self.resources = resources
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="finsight")
        self.cleaner = ToolProgressReporter(lambda _: None, (settings.model.api_key.get_secret_value(),))
        self.observer = None

    def start(self):
        self.store.recover()
        for session_id in self.store.all_sessions():
            lease_name = "host_" + hashlib.sha256(session_id.encode()).hexdigest()[:24]
            try:
                with self.runtime.lease(lease_name):
                    for work in self.runtime.records("work", session_id):
                        if work["status"] == "running":
                            work.update(status="interrupted", reason="原进程执行线程已不可用；未自动重放")
                            self.runtime.put("work", session_id, work)
                            self.runtime.emit(session_id, "task_notification", work, event_id=work["id"], notify=True)
            except AgentError as exc:
                if exc.code != "SESSION_BUSY":
                    raise
        self.observer = Thread(target=self.observe, name="finsight_events", daemon=True)
        self.observer.start()

    def session_lock(self, session_id):
        with self.lock:
            return self.locks.setdefault(session_id, RLock())

    def host(self, session_id):
        self.store.session(session_id)
        with self.lock:
            if session_id not in self.hosts:
                if self.stopped.is_set():
                    raise AgentError("SERVICE_STOPPING", "服务正在关闭")
                if len(self.hosts) >= 32:
                    raise AgentError("SESSION_LIMIT", "当前已打开32个会话，请重启服务释放空闲会话")
                host = self.host_factory(self.settings.model_copy(deep=True), session_id)
                owner = self.settings.memory.user_id
                if host.state.get('owner_user_id', owner) != owner:
                    host.close()
                    raise AgentError('NOT_FOUND', '会话不存在')
                host.state['owner_user_id'] = owner
                host.background.owner_user_id = owner
                host.tool_filter = {
                    *host.finance_tool_names,
                    "todo_write", "load_skill", "compact", "read_output", "search_output", "update_state",
                    "start_deep_research",
                    "list_research", "research_status", "read_research_report", "select_tool_groups", "search_tools",
                }
                if self.settings.harness.memory_enabled:
                    host.tool_filter.add("memory_write")
                original_start = host.pool.tools["start_deep_research"].handler
                def bounded_start(arguments, context, ledger):
                    prior = any(work.get("resources", {}).get("source_id") == arguments.get("source_id")
                        and work.get("resources", {}).get("symbol") == (arguments.get("symbol") or context.symbol)
                        and work.get("resources", {}).get("mode") == arguments.get("mode", "deep_research")
                        for work in self.runtime.records("work", session_id) if work.get("kind") == "research")
                    if prior:
                        return original_start(arguments, context, ledger)
                    return self.launch_bounded(original_start, arguments, context, ledger)
                host.pool.tools["start_deep_research"].handler = bounded_start
                self.hosts[session_id] = host
            return self.hosts[session_id]

    def launch_bounded(self, launch, *args, **kwargs):
        with self.lock:
            if self.resources:
                return self.resources.launch_research(self, launch, *args, **kwargs)
            if sum(job["status"] == "running" for job in self.research_jobs()) >= 3:
                raise AgentError("RESEARCH_LIMIT", "当前已有3份后台研究，请等待完成后再启动")
            return launch(*args, **kwargs)

    def submit(self, session_id, key, command):
        with self.lock:
            if self.stopped.is_set():
                raise AgentError("SERVICE_STOPPING", "服务正在关闭")
            self.store.session(session_id)
            value, created = (self.resources.enqueue(self, session_id, key, command) if self.resources
                              else self.store.create_run(session_id, key, command))
            if created:
                self.executor.submit(self.execute, value["id"])
        return self.public_run(value)

    @staticmethod
    def public_run(value):
        return {key: value[key] for key in ("id", "seq", "session_id", "status", "result", "error", "created_at")}

    def execute(self, run_id):
        value = self.store.run(run_id)
        session_id, command = value["session_id"], value["command"]
        with self.session_lock(session_id):
            with self.lock:
                if self.stopped.is_set() or self.store.run(run_id)["status"] != "queued":
                    return
                self.active[session_id] = run_id
                self.store.update_run(run_id, "running")
            try:
                host = self.host(session_id)
                self.store.event(session_id, "run.started", {"run_id": run_id})
                with self.lock:
                    cancelled = self.store.run(run_id)["status"] == "cancelling"
                if cancelled:
                    self.store.update_run(run_id, "cancelled")
                    return
                host.ctx.cancelled = RunCancellation(lambda: self.store.run(run_id)["status"] == "cancelling")
                kind = command["kind"]
                if kind == "research":
                    if not host.ctx.cutoff_explicit:
                        host.ctx.as_of = now()
                    host.foreground = True
                    source = host._register_user(command["text"])
                    options = {"as_of": command["as_of"]} if command.get("as_of") else {}
                    output = self.launch_bounded(launch_research, host, symbol=command["symbol"], mode=command["mode"],
                                             goal=command["text"], source_id=source, command=True, **options)
                    host.checkpoint()
                    result = {"answer": "公司深度研究已在后台启动，可继续对话。", **output}
                    status = "completed"
                elif kind == "resume":
                    job = self.research_job(command["research_id"])
                    if job["status"] not in {"interrupted", "failed", "cancelled"}:
                        raise AgentError("RESEARCH_STATE", "仅中断、失败或取消的研究可以恢复")
                    service = ResearchService(host.settings.model_copy(deep=True))
                    research_id = command["research_id"]
                    def resume(cancel, work_id):
                        return service.resume(research_id, cancelled=cancel,
                            progress=lambda text: host.event("research_progress", work_id=work_id, text=str(text)))
                    output = self.launch_bounded(host.background.launch, "恢复公司研究", resume, kind="research", resources={
                        "research_id": research_id, "symbol": job["symbol"], "goal": job["title"]})
                    result, status = {"answer": "已请求恢复原研究。", **output}, "completed"
                else:
                    text = command.get("text")
                    original_filter = host.tool_filter
                    try:
                        if kind == "question":
                            self.report_owner(command["research_id"])
                            if command["research_id"] not in host.state["research_links"]:
                                host.state["research_links"].append(command["research_id"])
                            read_report(host, command["research_id"])
                            text = f"请先读取已关联的历史研究报告 {command['research_id']}，按原范围回答以下追问，不启动新研究：\n{text}"
                            host.tool_filter = original_filter - {"start_deep_research"}
                        output = host.submit(text)
                    finally:
                        host.tool_filter = original_filter
                    result = {"answer": output.answer, "model_calls": output.model_calls, "tool_calls": output.tool_calls}
                    status = output.status
                    if status == "failed":
                        diagnostic = failure_diagnostic(output.reason, output.error_retryable)
                        result["diagnostic"] = diagnostic
                        result["answer"] = diagnostic["hint"] + " 会话已保留，可展开下方的故障诊断。"
                        if host.ctx.perception_results:
                            result["answer"] += " 已取得的资料保存在运行记录中。"
                with self.lock:
                    if self.store.run(run_id)["status"] == "cancelling":
                        status = "cancelled"
                    self.store.update_run(run_id, status, result)
                self.store.event(session_id, "message.completed", {"run_id": run_id, "status": status})
            except Exception as exc:
                logger.exception("Web run failed: %s", run_id)
                code = exc.code if isinstance(exc, AgentError) else "EXECUTION_FAILED"
                message = self.cleaner.clean(exc.message) if isinstance(exc, AgentError) else "执行未完成，请查看服务日志。"
                self.store.update_run(run_id, "failed", error=f"{code}: {message}")
                self.store.event(session_id, "run.failed", {"run_id": run_id, "code": code, "message": message})
            finally:
                with self.lock:
                    self.active.pop(session_id, None)

    def cancel(self, run_id):
        with self.lock:
            value = self.store.run(run_id)
            if value["status"] == "queued":
                self.store.update_run(run_id, "cancelled")
            elif value["status"] in {"running", "cancelling"}:
                self.store.update_run(run_id, "cancelling")
                host = self.hosts.get(value["session_id"])
                if host:
                    host.ctx.cancelled.set()
            self.store.event(value["session_id"], "run.cancel_requested", {"run_id": run_id})
        return self.public_run(self.store.run(run_id))

    def transform(self, event):
        kind, payload = event["kind"], event["payload"]
        if kind in {"tool_started", "tool_result"}:
            result = payload.get("result") or {}
            return ("tool.started" if kind == "tool_started" else "tool.completed", {
                "name": self.cleaner.clean(payload.get("name", "")), "call_id": payload.get("call_id"),
                "status": ("failed" if result.get("error") else result.get("status", "completed")) if isinstance(result, dict) else "completed"})
        if kind == "research_progress":
            return "research.progress", {"work_id": payload.get("work_id"), "message": self.cleaner.clean(payload.get("text", ""), 300)}
        if kind == "research_started":
            return "research.started", {key: payload.get(key) for key in ("research_id", "work_id", "symbol", "as_of")}
        if kind == "task_notification" and payload.get("kind") == "research":
            result = payload.get("result") or {}
            resources = payload.get("resources") or {}
            return "research.finished", {"work_id": payload["id"], "status": payload["status"],
                "research_id": result.get("research_id") or result.get("id") or resources.get("research_id")}
        return None

    def observe(self):
        while not self.stopped.wait(.35):
            try:
                for session_id in self.store.all_sessions():
                    after = self.store.cursor(session_id)
                    rows = self.runtime.events(session_id, after=after, limit=100)
                    self.store.project(session_id, rows, self.transform)
                    host = self.hosts.get(session_id)
                    if not host or not host.store.pending(session_id):
                        continue
                    with self.lock:
                        if session_id in self.active:
                            continue
                    pending = host.store.pending(session_id)
                    signature = hashlib.sha256(json.dumps([event["event_id"] for event in pending]).encode()).hexdigest()
                    self.submit(session_id, "notification:" + signature, {"kind": "notification"})
            except Exception:
                logger.exception("Web event projection failed")

    def research_jobs(self):
        jobs = []
        for session_id in self.store.all_sessions():
            for work in self.runtime.records("work", session_id):
                if work.get("kind") != "research":
                    continue
                resources, result = work.get("resources", {}), work.get("result") or {}
                research_id = result.get("research_id") or result.get("id") or resources.get("research_id")
                jobs.append({"work_id": work["id"], "research_id": research_id, "session_id": session_id,
                    "title": resources.get("goal", work.get("label", "公司研究")), "symbol": resources.get("symbol"),
                    "as_of": resources.get("as_of"), "status": work["status"], "progress": self.cleaner.clean(resources.get("last_progress", ""), 300),
                    "content_status": result.get("content_status") or result.get("content"),
                    "report_available": bool(result.get("report_available") or result.get("report"))})
        unique = {}
        for job in jobs:
            unique[job["research_id"] or job["work_id"]] = job
        return list(reversed(list(unique.values())))

    def research_job(self, reference):
        value = next((job for job in self.research_jobs() if reference in (job["work_id"], job["research_id"])), None)
        if value is None:
            raise AgentError("NOT_FOUND", "研究不属于 Web 工作区")
        return value

    def report_owner(self, research_id):
        return self.research_job(research_id)["session_id"]

    def cancel_research(self, reference):
        job = self.research_job(reference)
        host = self.hosts.get(job["session_id"])
        if job["status"] == "running" and host:
            host.background.cancel(job["work_id"])
        elif job["status"] == "running":
            raise AgentError("RESEARCH_STATE", "原研究线程已不在当前进程，请先恢复会话状态")
        return job

    def busy(self):
        return bool(self.active or self.store.pending_count() or any(j['status'] == 'running' for j in self.research_jobs()))

    def close(self):
        self.stopped.set()
        if self.observer:
            self.observer.join(timeout=3)
        for host in list(self.hosts.values()):
            host.ctx.cancelled.set()
            host.background.cancel()
        if self.owns_executor:
            self.executor.shutdown(wait=True, cancel_futures=True)
        else:
            # Queued shared-executor items observe stopped; wait for active hosts.
            while self.active:
                sleep(.05)
        self.store.recover()
        for host in list(self.hosts.values()):
            host.close()
        while any(host.background.active() for host in self.hosts.values()):
            sleep(.05)
