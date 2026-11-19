"""Small persistent scope memory, protocol-safe compaction and explicit recovery."""
from __future__ import annotations

from datetime import datetime, timedelta
from importlib.resources import files
import json
import re
from time import monotonic

from .config import Limits
from .data.evidence import write_json
from .data.symbols import normalize_symbol
from .errors import AgentError
from .runtime import ExecutionContext, now


HISTORY_TURNS = 4
HISTORY_USER_CHARS = 1200
HISTORY_ANSWER_CHARS = 1800
_HISTORICAL_ID = re.compile(r"(?:raw_[a-f0-9]{64}|result_[a-f0-9]{32}|doc_[a-f0-9]{64}(?::fact:\d+)?)")


def recent_dialogue(turns):
    """Bound untrusted dialogue excerpts; never carry tool messages or permissions."""
    return [{"user": _HISTORICAL_ID.sub("[历史引用]", str(turn.get("user", "")))[:HISTORY_USER_CHARS],
             "assistant": _HISTORICAL_ID.sub("[历史引用]", str(turn.get("assistant", "")))[:HISTORY_ANSWER_CHARS],
             "status": str(turn.get("status", ""))[:32]}
            for turn in turns[-HISTORY_TURNS:] if isinstance(turn, dict)]


def compact_messages(messages, ctx, store):
    assistants = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
    if len(assistants) < 4:
        return messages, False
    cut = assistants[-3]
    if cut and messages[cut-1]["role"] == "system":
        cut -= 1
    # The prefix must contain complete tool pairs. No dangling requests move
    # into the summary and no orphan result is retained in the tail.
    requested = {c["id"] for m in messages[:cut] for c in m.get("tool_calls", [])}
    paired = {m["tool_call_id"] for m in messages[:cut] if m["role"] == "tool"}
    if requested != paired:
        return messages, False
    index = []
    for key, tool in ctx.result_tools.items():
        value = store.load_result(key)
        index.append({"result_id": key, "tool": tool, "status": value.status, "snapshot_id": value.snapshot_id,
                      "snapshot_ids": value.snapshot_ids, "units": value.units, "price_basis": value.price_basis,
                      "error_code": value.error.code if value.error else None,
                      "missing": value.data.get("missing", []), "invalidated": key in ctx.invalidated_result_ids})
    summary = {"compacted_history": True, "symbol": ctx.symbol, "market": "CN_A", "as_of": ctx.as_of.isoformat(),
               "loaded_skills": ctx.loaded_skills,
               "scope": ctx.scope, "document_ids": sorted(ctx.document_ids), "result_index": index,
               "instruction": "摘要只保留状态及证据索引，不代表证据支持结论。单位/未解决缺口保留；通过read_evidence回读原始结果。"}
    summary["user_updates"] = [m["content"] for m in messages[:cut] if m["role"] == "user"]
    # The optional history excerpt is a user message before the current goal.
    # Retain both without promoting historical content to system instructions.
    prefix_end = 3
    while prefix_end < len(messages) and messages[prefix_end]["role"] == "user":
        prefix_end += 1
    return messages[:prefix_end] + [{"role": "system", "content": json.dumps(summary, ensure_ascii=False)}] + messages[cut:], True


def restore_context(run_id, store):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise AgentError("INVALID_RUN_ID", "任务ID无效")
    directory = store.root / "runs" / run_id
    if not directory.resolve().is_relative_to((store.root / "runs").resolve()):
        raise AgentError("RESUME_DENIED", "恢复目录越界")
    if not (directory / "state.json").is_file():
        raise AgentError("RUN_NOT_FOUND", "任务恢复点不存在")
    state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    if state["state"] == "completed":
        raise AgentError("ALREADY_COMPLETED", "已完成任务不重复执行；请查看报告或另起任务获取最新数据")
    ctx = ExecutionContext(Limits.model_validate(state["limits"]), as_of=datetime.fromisoformat(state["as_of"]), symbol=state["symbol"])
    ctx.started = monotonic() - max(0, ctx.limits.wall_seconds-state["remaining_seconds"])
    for name in ("scope", "scope_constraints", "result_tools", "retrieval_rounds", "calculation_attempts", "calculation_failures", "event_search_rounds", "source_requests", "source_events", "cutoff_explicit", "clarification", "conversation_history", "loaded_skills"):
        setattr(ctx, name, state.get(name, getattr(ctx, name)))
    for name in ("document_ids", "evidence_ids", "perception_results", "required_tools", "invalidated_result_ids", "queried_symbols", "call_ids"):
        setattr(ctx, name, set(state.get(name, [])))
    for key, tool in ctx.result_tools.items():
        value = store.load_result(key)
        # Dynamic quotes are rechecked, while historical cutoff and immutable
        # reports remain unchanged. Recovery never turns them into today's data.
        if tool == "get_quote" and value.data_time and now() - datetime.fromisoformat(value.data_time) > timedelta(minutes=5):
            ctx.invalidated_result_ids.add(key)
    return state, ctx


class Conversation:
    def __init__(self, store, session_id="default"):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", session_id):
            raise AgentError("INVALID_SESSION_ID", "会话名仅允许字母数字下划线和短横线")
        self.store, self.path = store, store.root / "sessions" / f"{session_id}.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"symbol": None, "scope": {}, "preferences": {}}

    def context(self, goal, limits, symbol=None, as_of=None):
        from .data.symbols import clean_name, INDICES
        catalog = json.loads(files("finagent").joinpath("resources/securities.json").read_text(encoding="utf-8"))
        catalog += [{"symbol": symbol, **entry} for symbol, entry in INDICES.items()]
        names = {entry["symbol"] for entry in catalog if any(clean_name(n) in clean_name(goal) for n in [entry["name"], *entry.get("aliases", [])])}
        codes = re.findall(r"(?<!\d)(?:SH|SZ)?\d{6}(?:\.(?:SH|SZ))?(?!\d)", goal, re.I)
        for code in codes:
            names.add(normalize_symbol(code))
        inherited = self.state.get("symbol")
        switching = bool(re.search(r"换成|换一家|另一家公司|改查|第[二三四五六七八九十]家公司", goal))
        if len(names) == 1:
            company = next(iter(names))
        elif names or switching:
            company = None
        else:
            referential = re.search(r"^(那|该|这|它|上一|上年|今年|去年|昨天|今天|还有|再|收入|净利|营业|利润率|资产负债|总资产|总负债|现金流|20\d{2}年)|该公司|这家公司", goal)
            company = inherited if referential else None
        # --symbol is a fallback, not a lock. An explicit company or a resolved
        # conversational reference takes precedence over the startup default.
        if symbol and company is None and not names and not switching:
            company = symbol
        ctx = ExecutionContext(limits, as_of=as_of or now(), symbol=company, cutoff_explicit=as_of is not None)
        ctx.conversation_history = recent_dialogue(self.state.get("history", []))
        # Scope may inherit across an explicit company switch, but evidence never does.
        ctx.scope = dict(self.state.get("scope", {}))
        ctx.scope.pop("plan", None)
        ctx.scope["display_preferences"] = dict(self.state.get("preferences", {}))
        ctx.scope.pop("restored_at", None)
        if ctx.symbol != self.state.get("symbol"):
            ctx.scope.pop("research_id", None)
        period = ctx.scope.get("report_period")
        if re.search(r"上一年|去年|上年", goal) and period:
            stamp = datetime.fromisoformat(period)
            ctx.scope["report_period"] = stamp.replace(year=stamp.year-1, day=min(stamp.day, 28) if stamp.month == 2 else stamp.day).date().isoformat()
        match = re.search(r"(20\d{2})年?\s*(年报|年度|半年报|半年度|一季报|三季报)", goal)
        if match:
            ctx.scope["report_period"] = match[1] + {"年报": "-12-31", "年度": "-12-31", "半年报": "-06-30", "半年度": "-06-30", "一季报": "-03-31", "三季报": "-09-30"}[match[2]]
        elif re.search(r"20\d{2}年", goal) and not re.search(r"半年|季度|单季|Q[1-4]", goal, re.I):
            year = re.search(r"(20\d{2})年", goal)[1]
            suffix = ctx.scope.get("report_period", "2000-12-31")[4:]
            ctx.scope["report_period"] = year + suffix
        elif re.search(r"20\d{2}年", goal):
            # Let the model clarify/normalize an unfamiliar quarter description;
            # do not lock an inherited annual end date over explicit wording.
            ctx.scope.pop("report_period", None)
        day = re.search(r"20\d{2}-\d{2}-\d{2}", goal)
        if day and re.search(r"复盘|行情|表现|涨|跌", goal):
            ctx.scope["trade_date"] = day[0]
        if "今天" in goal:
            ctx.scope["trade_date"] = now().date().isoformat()
        elif "昨天" in goal:
            anchor = datetime.fromisoformat(ctx.scope["trade_date"]).date() if "该交易日的前一日" in goal and ctx.scope.get("trade_date") else now().date()
            ctx.scope["trade_date"] = (anchor - timedelta(days=1)).isoformat()
        if not as_of and ctx.scope.get("trade_date") and re.search(r"复盘|行情|表现|涨|跌|昨天|今天", goal):
            day = datetime.fromisoformat(ctx.scope["trade_date"]).date()
            if day < now().date():
                from .market import TradingCalendar
                actual, _ = TradingCalendar().session(day)
                ctx.as_of = datetime.combine(actual, datetime.max.time().replace(microsecond=0), now().tzinfo)
        if ctx.scope.get("report_period"):
            period = ctx.scope["report_period"]
            allowed = {period}
            if re.search(r"同比|增长|比较|变动|变化", goal):
                stamp = datetime.fromisoformat(period)
                allowed.add(stamp.replace(year=stamp.year-1, day=min(stamp.day, 28) if stamp.month == 2 else stamp.day).date().isoformat())
            if len(set(re.findall(r"(20\d{2})年", goal))) > 1:
                allowed.update(year + period[4:] for year in re.findall(r"(20\d{2})年", goal))
            ctx.scope_constraints.update(report_period=period, allowed_report_periods=sorted(allowed))
        if ctx.scope.get("trade_date"):
            ctx.scope_constraints["requested_trade_date"] = ctx.scope["trade_date"]
        profit_text = goal if re.search(r"归母|归属.*净利润|全部净利润|合并净利润", goal) else str(ctx.scope.get("metric", ""))
        profit_metrics = []
        if re.search(r"归母|归属.*净利润", profit_text):
            profit_metrics.append("attributable_net_profit")
        if re.search(r"全部净利润|合并净利润", profit_text):
            profit_metrics.append("net_profit")
        if profit_metrics:
            ctx.scope_constraints["profit_metrics"] = profit_metrics
        return ctx

    def remember(self, result):
        state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
        body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        messages = json.loads((result.run_dir / "messages.json").read_text(encoding="utf-8"))
        user_texts = []
        for message in messages:
            if message["role"] != "user":
                continue
            content = message.get("content") or ""
            try:
                payload = json.loads(content)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and "conversation_history" in payload:
                continue
            user_texts.append(content)
        # Natural-language replies can carry the conversation without claiming
        # fact verification or granting access to old evidence. Do not remember
        # failed output or data conclusions without any current observations.
        recallable = body.get("final_protocol_verified") or (
            body.get("terminal_response_validated")
            and not any(item["code"] == "DATA_UNAVAILABLE" for item in body.get("completion_warnings", [])))
        turn = {"run_id": result.run_id, "user": "\n".join(user_texts) or state["goal"],
                "assistant": result.answer if recallable else "",
                "status": result.status}
        history = [item for item in self.state.get("history", [])
                   if isinstance(item, dict) and item.get("run_id") != result.run_id]
        history.append(turn)
        self.state["history"] = [dict(item, **excerpt) for item, excerpt in
                                 zip(history[-HISTORY_TURNS:], recent_dialogue(history))]
        self.state.update(symbol=state["symbol"], scope=state.get("scope", {}), last_run_id=result.run_id)
        self.state["last_response_type"] = "chat"
        write_json(self.path, self.state)

    def remember_research(self, state):
        """Store only a derived summary; no evidence grants cross conversations."""
        research_id = state["id"]
        self.state.update(symbol=state["scope"]["symbol"], scope={"research_id": research_id}, last_research_id=research_id,
                          last_response_type="research",
                          research_summary={"research_id": research_id, "content_status": state["content"], "goal": state["scope"]["goal"]})
        history = list(self.state.get("history", []))
        history.append({"run_id": research_id, "user": state["scope"]["goal"],
                        "assistant": "已保存研究报告，研究ID：" + research_id + "，内容状态：" + state["content"] + "。需要时按ID回读，历史摘要不是证据。",
                        "status": state["status"]})
        self.state["history"] = history[-HISTORY_TURNS:]
        write_json(self.path, self.state)

    def preference(self, name, value=None):
        if name not in ("display_unit", "watchlist"):
            raise AgentError("PREFERENCE_DENIED", "仅存储用户明确给出的展示单位或关注列表")
        if value is None:
            self.state["preferences"].pop(name, None)
        else:
            self.state["preferences"][name] = value
        self.state.setdefault("scope", {})["display_preferences"] = dict(self.state["preferences"])
        write_json(self.path, self.state)

    def forget(self):
        self.path.unlink(missing_ok=True)
        self.state = {"symbol": None, "scope": {}, "preferences": {}}
