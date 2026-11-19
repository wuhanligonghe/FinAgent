"""Independent deep-research service; ordinary analysis never routes here."""
from datetime import datetime, timedelta
from copy import deepcopy
import json
from pathlib import Path
import re

from ..errors import AgentError


class ResearchService:
    def __init__(self, settings):
        self.settings = settings

    def start(self, mode, symbol, goal, *, cancelled=None, progress=None, request=None, on_created=None, **options):
        from ..research.selection import select
        from ..research.coordinator import ResearchCoordinator
        request = request or select(self.settings, mode=mode, source="cli_flag", symbol=symbol, goal=goal, **options)
        return ResearchCoordinator(self.settings).run(request, cancelled=cancelled, progress=progress, on_created=on_created)

    def status(self, research_id):
        from ..research.store import ResearchStore
        return ResearchStore.from_settings(self.settings).snapshot(research_id)

    def resume(self, research_id, progress=None, cancelled=None):
        from ..research.selection import resume_selected
        return resume_selected(self.settings, research_id, progress=progress, cancelled=cancelled)

    def read(self, research_id, ctx, *, authorized=(), offset=0, limit=None):
        from ..research.store import ResearchStore
        from ..research.review import check_result
        from ..runtime import ExecutionContext
        from ..runtime import now
        from ..data.evidence import EvidenceStore
        if offset < 0 or (limit is not None and limit < 1):
            raise AgentError("INVALID_ARGUMENTS", "报告分页范围无效")
        if research_id not in authorized and ctx.scope.get("research_id")!=research_id:
            raise AgentError("RESEARCH_DENIED","报告未与当前会话关联，也没有用户显式读取授权")
        store = ResearchStore.from_settings(self.settings)
        row = store.get(research_id)
        scope = json.loads(row["scope"])
        if not row["report"]:
            raise AgentError("REPORT_UNAVAILABLE", "研究报告尚未生成")
        if datetime.fromisoformat(scope["as_of"]) > ctx.as_of:
            raise AgentError("AFTER_CUTOFF", "报告晚于当前历史截止时间")
        path = Path(row["report"]).resolve()
        if not path.is_relative_to(store.directory(research_id).resolve()):
            raise AgentError("REPORT_DENIED", "报告路径无效")
        evidence = EvidenceStore(self.settings.data_dir)
        adopted = []
        for grant in store.grants(research_id):
            value = evidence.load_result(grant["result_id"])
            check = ExecutionContext(as_of=ctx.as_of, symbol=value.symbol)
            try:
                check_result(value, check)
            except AgentError:
                continue
            ctx.result_tools[value.result_id] = grant["tool"]
            ctx.evidence_ids.update([value.result_id, *value.snapshot_ids])
            if value.snapshot_id:
                ctx.evidence_ids.add(value.snapshot_id)
            if grant["tool"]=="get_quote" and (value.stale or (value.fetched_at and now()-value.fetched_at>timedelta(minutes=5))):
                ctx.invalidated_result_ids.add(value.result_id)
            if value.symbol:
                ctx.queried_symbols.add(value.symbol)
            adopted.append(value.result_id)
        text = path.read_text(encoding="utf-8")
        end = len(text) if limit is None else offset + limit
        return {"research_id": research_id, "original_scope": scope, "report_path": str(path), "text": text[offset:end],
                "offset": offset, "total_chars": len(text), "next_offset": end if end < len(text) else None,
                "adopted_results": adopted, "note": "历史研究，不代表本次最新行情；当前focus未改变"}


def explicit_request(host, source_id, request_quote, mode):
    """Check user provenance at the launch boundary; never route ordinary chat."""
    if host.parent is not None or not host.foreground:
        raise AgentError("RESEARCH_DENIED", "独立研究只能由主会话的真实用户请求发起")
    latest = host.state.get("user_updates", [])[-1:]
    if not latest or latest[0]["id"] != source_id:
        raise AgentError("RESEARCH_DENIED", "请引用最新真实用户消息的source_id")
    text = latest[0]["text"]
    if not request_quote or request_quote not in text:
        raise AgentError("RESEARCH_DENIED", "启动依据须逐字引用最新用户的研究请求")
    # A quote in a document, a question about the feature, negation and vague
    # 'look deeper' are not a request to spend an independent research budget.
    stripped = re.sub(r'「[^」]*」|“[^”]*”|"[^"\n]*"|`[^`]*`', '', text)
    markers = r"深度研究|深度报告|深度分析报告|研究报告|完整研究|完整决策|deep_research|full_decision"
    actions = r"请|帮我|给我|做|生成|写|出一份|启动|开始|进行|开展|执行|来一份"
    clauses = re.split(r"[。！？!?；;，,\n]", stripped)
    affirmative = [c for c in clauses if re.search(markers, c, re.I) and re.search(actions, c)
                   and not re.search(r"不要|别|不必|不用|先不|暂不|无需|禁止|如果|假如|假设|是否|怎么|如何|什么|区别|合适|比较好|能不能|能否|可以吗|多久|多长|费用|耗时|解释|解读|总结|进度|状态", c)]
    if not affirmative or not any(request_quote in c or c.strip() in request_quote for c in affirmative):
        raise AgentError("RESEARCH_DENIED", "尚无明确启动独立研究的用户请求；普通分析或功能咨询继续在主对话处理")
    if mode == "full_decision" and not any(re.search(r"完整决策|完整研究与决策|风险讨论|交易方案|full_decision", c, re.I) for c in affirmative):
        raise AgentError("RESEARCH_DENIED", "用户未要求完整决策链，请使用deep_research")
    return latest[0]


def launch_research(host, *, mode="deep_research", symbol=None, goal=None, source_id=None,
                    request_quote=None, command=False, **options):
    """Freeze a small handoff on the lead thread and run in an independent job."""
    from ..data.symbols import require_stock
    from ..research.selection import select, config_for
    from ..runtime import now
    if not command:
        source = explicit_request(host, source_id, request_quote, mode)
    else:
        source = next((u for u in host.state.get("user_updates", []) if u["id"] == source_id),
                      {"id": source_id, "text": goal or "独立深度研究"})
    symbol = require_stock(symbol or host.ctx.symbol) if symbol or host.ctx.symbol else None
    if not symbol:
        raise AgentError("NEEDS_CLARIFICATION", "请先通过resolve_entity确认本次深度研究的公司")
    target = goal or source["text"]
    # Rephrasing the goal or changing analyst arguments on a retry must not
    # spend a second budget for the same user request/company/mode.
    for work in host.store.records("work", host.session_id):
        prior = work.get("resources", {})
        if (source_id and work.get("kind") == "research" and prior.get("source_id") == source_id
                and prior.get("symbol") == symbol and prior.get("mode") == mode):
            return {"work_id": work["id"], "status": work["status"], "reused": True,
                    "research_id": work.get("result", {}).get("research_id") or work["resources"].get("research_id")}
    # Use the same cutoff as this chat turn, including an explicit historical one.
    cutoff = host.ctx.as_of
    if options.get("as_of"):
        cutoff = datetime.fromisoformat(options.pop("as_of"))
    constraints = [dict(c) for c in host.state.get("user_constraints", []) if c["status"] == "active"]
    handoff = {"session_id": host.session_id, "source_id": source_id, "user_request": source["text"],
               "symbol": symbol, "as_of": cutoff.isoformat(), "constraints": constraints,
               "scope": deepcopy(host.ctx.scope), "scope_constraints": deepcopy(host.ctx.scope_constraints),
               "todos": deepcopy(host.state.get("todos", []))}
    # Source excerpts carry user intent, not inherited assistant claims/evidence.
    context = json.dumps({"current_user_request": source, "user_constraints": constraints, "scope": handoff["scope"],
                          "scope_constraints": handoff["scope_constraints"], "todos": deepcopy(host.state.get("todos", []))}, ensure_ascii=False, default=str)
    frozen_goal = target + "\n\n[主会话交接：以下是带来源的用户约束与范围，不是新的金融事实]\n" + context
    request = select(host.settings, mode=mode, symbol=symbol, goal=frozen_goal,
                     source="cli_flag" if command else "chat_request", as_of=cutoff,
                     session_id=host.session_id, market_window=host.ctx.scope.get("market_window", 120), **options)
    handoff["selection_id"] = request.selection_id
    cfg = config_for(request, host.settings)
    resources = {"source_id": source_id, "selection_id": request.selection_id, "symbol": symbol,
                 "mode": mode, "as_of": cutoff.isoformat(), "goal": target, "analysts": request.analysts,
                 "budget": {"model_calls": cfg.model_calls, "tool_calls": cfg.tool_calls, "wall_seconds": cfg.wall_seconds},
                 "handoff": handoff}
    service = ResearchService(host.settings.model_copy(deep=True))

    def run(cancel, work_id):
        def update(**fields):
            resources.update(fields)
            work = next(w for w in host.store.records("work", host.session_id) if w["id"] == work_id)
            work["resources"] = dict(resources)
            host.store.put("work", host.session_id, work)

        def created(research_id, scope):
            update(research_id=research_id)
            from ..data.evidence import write_json
            from ..research.store import ResearchStore
            write_json(ResearchStore.from_settings(host.settings).directory(research_id) / "conversation_handoff.json", handoff)
            host.store.emit(host.session_id, "research_started", {"research_id": research_id, "work_id": work_id,
                "symbol": symbol, "as_of": cutoff.isoformat(), "mode": mode},
                event_id="research_started_" + work_id, notify=True)

        def progress(text):
            update(last_progress=str(text), updated_at=now().isoformat())
            host.event("research_progress", work_id=work_id, text=str(text))

        try:
            state = service.start(mode, symbol, frozen_goal, cancelled=cancel, progress=progress,
                                  request=request, on_created=created)
        except AgentError as exc:
            return {"status": "cancelled" if cancel.is_set() else "failed", "error": exc.code,
                    "message": exc.message, "research_id": resources.get("research_id"),
                    "symbol": symbol, "as_of": cutoff.isoformat(), "report_available": False}
        return {"research_id": state["id"], "status": state["status"], "content_status": state["content"],
                "symbol": symbol, "as_of": cutoff.isoformat(), "mode": mode,
                "report_available": bool(state.get("report")),
                "next_action": "用read_research_report读取报告，给出摘要、主要依据与缺口，并承接用户追问"}

    work = host.background.launch("独立深度研究 " + mode, run, kind="research", resources=resources)
    host.state["last_research_work_id"] = work["work_id"]
    host.state.pop("last_read_research_id", None)
    host.checkpoint()
    return {**work, "symbol": symbol, "as_of": cutoff.isoformat(), "mode": mode,
            "budget": resources["budget"], "note": "独立后台研究已启动，可继续聊天；用research_status查看进度"}


def research_jobs(host):
    return [{"work_id": w["id"], "status": w["status"],
             **{k: v for k, v in w.get("resources", {}).items() if k != "handoff"},
             "result": w.get("result"), "error": w.get("error")}
            for w in host.store.records("work", host.session_id) if w.get("kind") == "research"]


def research_ref(host, reference=None):
    if reference is None:
        reference = host.state.get("last_research_work_id") or next(iter(host.state["research_links"][-1:]), None)
    if not reference:
        raise AgentError("RESEARCH_NOT_FOUND", "本会话尚无关联研究，可先列出研究任务或明确报告ID")
    if reference.startswith("work_"):
        work = next((w for w in research_jobs(host) if w["work_id"] == reference), None)
        if not work:
            raise AgentError("RESEARCH_DENIED", "该研究任务不属于当前会话")
        research_id = (work.get("result") or {}).get("research_id") or work.get("research_id")
        return research_id, work
    from .extensions import authorize_report
    return authorize_report(host, reference), None


def research_status(host, reference=None):
    research_id, work = research_ref(host, reference)
    if not research_id:
        return work
    value = ResearchService(host.settings).status(research_id)
    if work:
        value.update(work_id=work["work_id"], last_progress=work.get("last_progress"))
    return value


def read_report(host, reference=None, offset=0, limit=12000):
    research_id, work = research_ref(host, reference or host.state.get("last_read_research_id"))
    if not research_id:
        raise AgentError("REPORT_UNAVAILABLE", "研究尚未生成报告，可先查看research_status")
    value = ResearchService(host.settings).read(research_id, host.ctx,
        authorized=[*host.state["research_links"], *([research_id] if work else [])], offset=offset, limit=limit)
    if research_id not in host.state["research_links"]:
        host.state["research_links"].append(research_id)
    host.state["last_read_research_id"] = research_id
    return value
