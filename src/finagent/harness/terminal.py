"""Interactive s15 host: user input and asynchronous events share one coordinator."""
from concurrent.futures import ThreadPoolExecutor
import json
import queue
import shlex
from threading import Thread
from threading import Event
from uuid import uuid4

from ..console import read_input
from ..errors import AgentError
from .host import SessionHost
from .research import ResearchService


HELP = """直接输入问题，Agent 在持续会话中自主使用工具并承接追问。
/new [名称] 新会话；/sessions 列表；/resume 名称 切换会话
/cancel 取消当前工作；/exit 退出；/compact 压缩；/goal [条件|clear]
/tasks 查看任务；/skills 查看技能；/memory [search 问题|forget ID] 查看/召回/删除长期记忆
/sources 查看来源；/details 查看状态；/help 帮助
/research [--mode deep_research|full_decision] [--symbol CODE] [研究目标]
/research-list；/research-status [ID]；/research-report [ID]；/research-resume ID；/research-trace ID
可直接说“给刚才这家公司做一份深度研究报告”；继承对象、截止与约束，独立后台运行。
普通“深入看看”继续由主对话分析。"""


class ApprovalBroker:
    """Only the input coordinator consumes approval answers, never model workers."""
    def __init__(self):
        self.requests = queue.Queue()
        self.pending = None
        self.closed = Event()

    def ask(self, name, parameters):
        response = queue.Queue(maxsize=1)
        if self.closed.is_set():
            return False
        self.requests.put((name, parameters, response))
        while not self.closed.is_set():
            try:
                return response.get(timeout=.1)
            except queue.Empty:
                pass
        return False

    def answer(self, allowed):
        if self.pending:
            self.pending[2].put(allowed)
            self.pending = None

    def close(self):
        self.closed.set()
        self.answer(False)


def command(host, text, write):
    parts = shlex.split(text)
    name, args = parts[0], parts[1:]
    if name == "/help":
        write(HELP)
    elif name == "/sessions":
        write(json.dumps(host.store.sessions(), ensure_ascii=False))
    elif name in {"/new", "/resume"}:
        target = args[0] if args else "session_" + uuid4().hex[:8]
        if name == "/resume" and host.store.load(target) is None:
            raise ValueError("会话不存在；旧运行记录使用 legacy-run 查看")
        return ("switch", target)
    elif name == "/goal":
        condition = text[len("/goal"):].strip()
        if condition == "clear":
            host.goals.clear()
            host.checkpoint()
            write("已清除目标。")
        elif condition:
            host.goals.set(condition)
            host.checkpoint()
            return ("submit", condition)
        else:
            write(json.dumps(host.state.get("active_goal_ref"), ensure_ascii=False))
    elif name == "/compact":
        host.context.compact(host.messages, host.ctx.limits.context_chars, force=True)
        host.checkpoint()
        write("已整理会话上下文，原始记录可回读。")
    elif name == "/tasks":
        write(json.dumps(host.board.all(), ensure_ascii=False))
    elif name == "/skills":
        write(host.finance.skill_library.catalog_message())
    elif name == "/memory":
        if len(args) == 2 and args[0] == "forget":
            write(json.dumps(host.memory.forget(args[1]), ensure_ascii=False))
        elif args and args[0] == "search":
            if not host.settings.harness.memory_enabled:
                raise ValueError("长期记忆已关闭")
            if len(args) < 2:
                raise ValueError("请提供记忆检索问题")
            records = host.memory.select(" ".join(args[1:]))
            write(json.dumps({"memories": records, "retrieval": host.memory.last_recall}, ensure_ascii=False))
        else:
            write(json.dumps(host.memory.records(), ensure_ascii=False))
    elif name == "/details":
        write(json.dumps({"session": host.session_id, "last_result": host.state.get("last_result"),
                          "usage": host.state["usage"], "ledger": host.state.get("last_ledger"),
                          "fact_validation": host.state.get("fact_validation"), "directory": str(host.directory)}, ensure_ascii=False))
    elif name == "/sources":
        sources = []
        for key, tool in host.ctx.result_tools.items():
            value = host.service.store.load_result(key)
            sources.append({"result_id": key, "tool": tool, "symbol": value.symbol,
                            "source": value.source, "url": value.source_url, "time": value.data_time,
                            "stale": value.stale, "invalidated": key in host.ctx.invalidated_result_ids})
        write(json.dumps(sources, ensure_ascii=False) if sources else "本会话尚未引用外部数据。")
    elif name == "/research":
        import argparse
        parser = argparse.ArgumentParser(prog="/research", exit_on_error=False, add_help=False)
        parser.add_argument("--mode", choices=("deep_research", "full_decision"), default="deep_research")
        parser.add_argument("--symbol")
        parser.add_argument("goal", nargs="*", default=[])
        parsed = parser.parse_args(args)
        if not args:
            write("深度研究继承当前公司、截止与约束。例如：/research 分析现金流质量；也可用 --symbol 指定公司、--mode full_decision 选择完整决策。/research-list 查看研究任务。")
        else:
            from .research import launch_research
            source_id = host._register_user(text)
            work = launch_research(host, mode=parsed.mode, symbol=parsed.symbol,
                                   goal=" ".join(parsed.goal) or "深入研究并给出最终研判",
                                   source_id=source_id, command=True)
            write("已启动独立深度研究：" + json.dumps(work, ensure_ascii=False))
    elif name == "/research-list":
        from .research import research_jobs
        write(json.dumps({"tasks": research_jobs(host), "linked_reports": host.state["research_links"]}, ensure_ascii=False))
    elif name in {"/research-status", "/research-report", "/research-resume", "/research-trace"}:
        if not args and name in {"/research-resume", "/research-trace"}:
            raise ValueError("请提供研究ID")
        service = ResearchService(host.settings)
        if name == "/research-report":
            if args and not args[0].startswith("work_"):
                report = service.read(args[0], host.ctx, authorized=[args[0]])
            else:
                from .research import read_report
                report = read_report(host, args[0] if args else None, limit=None)
            write(report["text"])
            research_id = report["research_id"]
            if research_id not in host.state["research_links"]:
                host.state["research_links"].append(research_id)
            host.state["last_read_research_id"] = research_id
            host.checkpoint()
        elif name == "/research-resume":
            status = service.status(args[0])
            def resume(cancel, work_id):
                state = service.resume(args[0], cancelled=cancel,
                    progress=lambda text: host.event("research_progress", work_id=work_id, text=text))
                return {"research_id": state["id"], "status": state["status"], "content_status": state["content"],
                        "report_available": bool(state.get("report"))}
            work = host.background.launch("恢复独立深度研究", resume, kind="research",
                resources={"research_id": args[0], "symbol": status["scope"]["symbol"], "as_of": status["scope"]["as_of"]})
            host.state["last_research_work_id"] = work["work_id"]
            host.state.pop("last_read_research_id", None)
            host.checkpoint()
            write(json.dumps(work, ensure_ascii=False))
        elif name == "/research-trace":
            from ..research.decision_reporting import read_trace
            from ..research.store import ResearchStore
            write(json.dumps(read_trace(ResearchStore.from_settings(host.settings), args[0], args[1] if len(args)>1 else "all"), ensure_ascii=False))
        else:
            from .research import research_status
            if args and not args[0].startswith("work_"):
                write(json.dumps(service.status(args[0]), ensure_ascii=False, default=str))
            else:
                write(json.dumps(research_status(host, args[0] if args else None), ensure_ascii=False, default=str))
    elif name == "/cancel":
        host.cancel()
        write("已请求取消当前工作，会话与已取得资料保留。")
    elif name in {"/exit", "/quit"}:
        return ("exit", None)
    else:
        write("未知命令。输入 /help 查看帮助。")
    return None


def run_terminal(settings, session_id="default", *, host=None, read=None, write=print):
    """Terminal and events are coordinated outside the model/tool kernel."""
    host = host or SessionHost(settings, session_id, write=write)
    hosts, active = {host.session_id: host}, host
    write("你好，我是 FinSight。当前会话：" + active.session_id + "。输入 /help 查看帮助。")
    # Injected readers are deterministic/scripted clients; real terminals keep
    # input live while the model works and can cancel/steer before its next call.
    if read is not None:
        try:
            while True:
                try:
                    text = read("你 > ").strip()
                except (EOFError, StopIteration, KeyboardInterrupt):
                    break
                if not text:
                    continue
                try:
                    action = command(active, text, write) if text.startswith("/") else ("submit", text)
                    if action:
                        kind, value = action
                        if kind == "exit":
                            break
                        if kind == "switch":
                            if value not in hosts:
                                hosts[value] = SessionHost(settings, value, write=write)
                            active = hosts[value]
                        elif kind == "submit":
                            result = active.submit(value)
                            if result.tool_calls == 0:
                                write("[工具] 本轮未调用工具。")
                            write("FinSight：" + result.answer)
                except (AgentError, ValueError) as exc:
                    write(str(exc))
        finally:
            for current in hosts.values():
                current.close()
        write("已退出 FinSight。")
        return 0

    inputs = queue.Queue()
    approvals = ApprovalBroker()
    host.permissions.approval = approvals.ask
    def pump():
        prompt = "你 > "
        while True:
            try:
                # Keep accepting steering/cancellation while a model is running.
                # After input() initializes the first prompt, the coordinator
                # displays subsequent prompts only after the prior reply.
                value = read_input(prompt)
                prompt = ""
                inputs.put(value)
            except (EOFError, KeyboardInterrupt):
                inputs.put(None)
                return
    prompt_visible = True
    Thread(target=pump, name="terminal_input", daemon=True).start()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session_coordinator")
    future, running, eof = None, None, False
    import signal
    terminating = Event()
    previous_term = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        terminating.set()
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    deferred = []
    for current in hosts.values():
        current.scheduler.start()
    try:
        while True:
            try:
                if future and future.done():
                    try:
                        result = future.result()
                        if result.tool_calls == 0:
                            write("[工具] 本轮未调用工具。")
                        write("FinSight[" + running.session_id + "]：" + result.answer)
                    except Exception as exc:
                        write("工作未完成：" + type(exc).__name__)
                    future, running = None, None
                if approvals.pending is None:
                    try:
                        approvals.pending = approvals.requests.get_nowait()
                        write("操作需要授权：" + approvals.pending[0] + " " + json.dumps(approvals.pending[1], ensure_ascii=False) + "。允许？[y/N]")
                    except queue.Empty:
                        pass
                try:
                    text = inputs.get(timeout=.1)
                    if text is None and prompt_visible:
                        write("")
                    prompt_visible = False
                    if text is None:
                        eof = True
                    else:
                        text = text.strip()
                        if approvals.pending:
                            approvals.answer(text.lower() in {"y", "yes"})
                            if text in {"/cancel", "/exit", "/quit"}:
                                deferred.append(text)
                        elif text:
                            deferred.append(text)
                except queue.Empty:
                    pass
                if deferred:
                    text = deferred.pop(0)
                    if future and not text.startswith("/"):
                        running.enqueue(text)
                        continue
                    if future and text not in {"/cancel", "/exit", "/quit", "/help"}:
                        deferred.insert(0, text)
                        continue
                    action = command(active, text, write) if text.startswith("/") else ("submit", text)
                    if action:
                        kind, value = action
                        if kind == "exit":
                            break
                        if kind == "switch":
                            if value not in hosts:
                                hosts[value] = SessionHost(settings, value, write=write)
                                hosts[value].permissions.approval = approvals.ask
                                hosts[value].scheduler.start()
                            active = hosts[value]
                        elif kind == "submit":
                            running = active
                            future = executor.submit(active.submit, value)
                if future is None and not deferred:
                    pending = next((h for h in hosts.values() if h.store.pending(h.session_id) or not h.steering.empty()), None)
                    if pending and not eof:
                        if prompt_visible:
                            write("")
                            prompt_visible = False
                        running = pending
                        future = executor.submit(pending.submit)
                    elif eof:
                        break
                if (future is None and not deferred and not eof and inputs.empty()
                        and approvals.pending is None and not prompt_visible):
                    print("你 > ", end="", flush=True)
                    prompt_visible = True
            except KeyboardInterrupt:
                if terminating.is_set():
                    break
                if future:
                    running.cancel()
                    write("已请求取消，保留会话。")
                else:
                    break
            except (AgentError, ValueError) as exc:
                write(str(exc))
    finally:
        approvals.close()
        for current in hosts.values():
            current.cancel()
            current.close()
        executor.shutdown(wait=True, cancel_futures=True)
        signal.signal(signal.SIGTERM, previous_term)
    write("已退出 FinSight。")
    return 0
