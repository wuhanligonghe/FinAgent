from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import copy
import time
from uuid import uuid4

from ..errors import AgentError
from ..runtime import ExecutionContext, now
from ..session import recent_dialogue
from .contracts import NLUResult, ROLES
from .qwen3 import get_parser
from .normalization import normalize


@dataclass
class Pending:
    text: str
    result: NLUResult
    slot: str
    candidates: list
    source_requests: int
    as_of: datetime
    created: float


@dataclass
class PreparedRequest:
    text: str
    result: NLUResult
    ctx: ExecutionContext
    mode: str
    roles: list
    pipeline: str | None
    news_days: int
    policy_days: int
    horizon: str
    request_id: str

    def research_request(self, settings, **options):
        from ..research.contracts import ResearchRequest
        if self.mode != "specialist" or self.pipeline != "research" or not self.ctx.symbol:
            raise AgentError("INVALID_RESEARCH_MODE", "聊天只允许已确认对象的专项分析；深度研究请使用 /research")
        if any(options.get(k) is not None for k in ("debate_rounds", "risk_rounds", "horizon", "risk_profile", "position_context")):
            raise AgentError("INVALID_RESEARCH_MODE", "专项分析不接受决策链参数")
        return ResearchRequest(goal=self.text, symbol=self.ctx.symbol, depth="specialist", analysts=self.roles,
            as_of=self.ctx.as_of, market_window=options.get("market_window", 120), news_days=self.news_days,
            policy_days=self.policy_days, session_id=options.get("session_id"), resolution_source_requests=self.ctx.source_requests)


def context_summary(conversation, pending=None):
    state = conversation.state
    scope = {k: v for k, v in state.get("scope", {}).items() if k in ("report_period", "trade_date", "metric", "lookback_days")}
    result = {"confirmed_symbol": state.get("symbol"), "scope": scope,
              "history": [t["user"][:1000] for t in recent_dialogue(state.get("history", []))[-2:]],
              "research_id": state.get("last_research_id")}
    if pending:
        result.update(pending_slot=pending.slot,
                      dimensions=[r for r, v in pending.result.dimension_state.items() if v == "include"],
                      history=[*result["history"], pending.text[:1000]][-3:], candidates=pending.candidates[:10])
    return result


def semantic_route(result):
    if result.dialogue_act == "cancel":
        raise AgentError("CANCELLED", "已取消当前请求")
    if result.task_type != "stock_analysis":
        return "direct", [], None
    roles = [r for r in ROLES if result.dimension_state[r] == "include"]
    if not roles:
        raise AgentError("NEEDS_CLARIFICATION", "希望了解技术、情绪、新闻、基本面或政策中的哪些维度？深度研究请使用 /research 显式选择。")
    return "specialist", roles, "research"


def _clarify(message, text, result, ctx, slot="semantics", candidates=None):
    error = AgentError("NEEDS_CLARIFICATION", message)
    error.pending = Pending(text, result, slot, list(candidates or []), ctx.source_requests, ctx.as_of, time.monotonic())
    raise error


def _prepare(text, settings, conversation, *, symbol=None, as_of=None, parser=None, service=None, pending=None, research_options=None):
    """Exactly one semantic prediction per turn. Host modes are never model labels."""
    started = time.perf_counter()
    selected_symbol = None
    if pending and time.monotonic() - pending.created > 900:
        raise AgentError("NLU_CLARIFICATION_EXPIRED", "候选确认已过期，请重新输入请求")
    if pending and text.isdecimal() and pending.candidates:
        if parser is None:
            current = get_parser(settings)
            current.ensure_identity()
            if current.fingerprint != pending.result.bundle_sha256:
                raise AgentError("NLU_MODEL_CHANGED", "候选确认期间模型已改变，请重新输入请求")
        index = int(text) - 1
        if not 0 <= index < len(pending.candidates):
            raise AgentError("NEEDS_CLARIFICATION", "候选序号无效，请重新选择当前列表中的序号")
        selected_symbol = pending.candidates[index]["symbol"]
        result = pending.result.model_copy(deep=True)
        original = pending.text
    else:
        result = (parser or get_parser(settings)).parse(text, context_summary(conversation, pending))
        result = NLUResult.model_validate(result).check_text(text)
        original = text
        if pending and (result.model_id, result.bundle_sha256, result.schema_version) != (pending.result.model_id, pending.result.bundle_sha256, pending.result.schema_version):
            raise AgentError("NLU_MODEL_INCOMPATIBLE", "澄清过程中模型已改变，请重新输入原请求")
        if pending and pending.slot == "entity" and result.dialogue_act in ("clarification_answer", "correct", "switch_entity"):
            shifted = [e.model_copy(update={"start": e.start + len(pending.text) + 1, "end": e.end + len(pending.text) + 1}) for e in result.entities if e.type in ("COMPANY", "STOCK_CODE")]
            if result.admission == "accept" and any(e.role == "target" for e in shifted):
                original = pending.text + "\n" + text
                result = pending.result.model_copy(update={"entities": sorted([e for e in pending.result.entities if e.type not in ("COMPANY", "STOCK_CODE")] + shifted, key=lambda e: e.start),
                    "entity_binding": result.entity_binding, "dialogue_act": "clarification_answer"}, deep=True)
        elif pending and pending.slot == "semantics" and result.dialogue_act == "clarification_answer":
            replaced = {e.type for e in result.entities}
            if replaced & {"COMPANY", "STOCK_CODE"}:
                replaced.update(("COMPANY", "STOCK_CODE"))
            original = pending.text + "\n" + text
            entities = [e for e in pending.result.entities if e.type not in replaced]
            entities += [e.model_copy(update={"start": e.start + len(pending.text) + 1, "end": e.end + len(pending.text) + 1}) for e in result.entities]
            binding = result.entity_binding if replaced & {"COMPANY", "STOCK_CODE"} else pending.result.entity_binding
            dimensions = {r: result.dimension_state[r] if result.dimension_state[r] != "unspecified" else pending.result.dimension_state[r] for r in ROLES}
            result = result.model_copy(update={"entities": sorted(entities, key=lambda e: e.start), "entity_binding": binding,
                "task_type": pending.result.task_type, "dimension_state": dimensions}, deep=True)
    result.check_text(original)
    if result.diagnostic_only:
        raise AgentError("NLU_MODEL_UNQUALIFIED", "诊断预测不能授予执行权限")
    options = research_options or {}
    if any(options.get(k) is not None for k in ("depth", "pipeline", "debate_rounds", "risk_rounds", "horizon", "risk_profile", "position_context")):
        raise AgentError("MODE_REQUIRED", "深度研究模式必须由 /research 或 research --mode 显式选择")
    if options.get("analysts"):
        if set(options["analysts"]) - set(ROLES):
            raise AgentError("INVALID_RESEARCH_MODE", "分析维度无效")
        if result.task_type != "stock_analysis" or any(result.dimension_state[r] == "exclude" for r in options["analysts"]):
            raise AgentError("INVALID_RESEARCH_MODE", "显式分析师与正文语义冲突")
        result = result.model_copy(update={"dimension_state": {r: "include" if r in options["analysts"] else result.dimension_state[r] for r in ROLES}})

    try:
        normalized = normalize(result, as_of=as_of or (pending.as_of if pending else None), inherited=conversation.state.get("scope"))
    except AgentError as exc:
        if exc.code == "NEEDS_CLARIFICATION":
            cutoff = pending.as_of if pending else (datetime.fromisoformat(as_of) if isinstance(as_of, str) else as_of or now())
            ctx = ExecutionContext(settings.limits, as_of=cutoff)
            ctx.source_requests = pending.source_requests if pending else 0
            _clarify(exc.message, original, result, ctx)
        raise
    ctx = ExecutionContext(settings.limits, as_of=normalized["as_of"], cutoff_explicit=normalized["cutoff_explicit"])
    ctx.scope = normalized["scope"]
    ctx.scope_constraints.update(normalized["constraints"])
    ctx.scope.setdefault("display_preferences", {}).update(conversation.state.get("preferences", {}))
    ctx.conversation_history = recent_dialogue(conversation.state.get("history", []))
    ctx.source_requests = pending.source_requests if pending else 0
    if result.input_status != "ok" or result.admission != "accept":
        _clarify("请求过长，请缩短或拆分" if result.input_status == "too_long" else "无法可靠理解请求，请明确对象和分析维度；深度研究请使用 /research", original, result, ctx)
    try:
        mode, roles, pipeline = semantic_route(result)
    except AgentError as exc:
        if exc.code == "NEEDS_CLARIFICATION":
            _clarify(exc.message, original, result, ctx)
        raise
    needs_entity = result.task_type in ("stock_analysis", "data_query")
    if needs_entity:
        targets = [e for e in result.entities if e.type in ("COMPANY", "STOCK_CODE") and e.role == "target"]
        excluded = {e.text for e in result.entities if e.role == "excluded"}
        if not selected_symbol and (bool(targets) != (result.entity_binding in ("explicit_single", "explicit_multiple"))):
            _clarify("对象绑定与原文实体不一致，请明确股票名称或代码", original, result, ctx, "entity")
        if targets or excluded or result.dialogue_act in ("switch_entity", "correct"):
            # A failed switch must never leave the old company executable on
            # the next turn. No evidence/research association survives it.
            conversation.state["symbol"] = None
            conversation.state.pop("last_research_id", None)
            conversation.state.get("scope", {}).pop("research_id", None)
            from ..data.evidence import write_json
            write_json(conversation.path, conversation.state)
        if selected_symbol:
            from ..data.symbols import normalize_symbol
            ctx.bind_symbol(normalize_symbol(selected_symbol))
        elif targets:
            own_service = service is None
            if own_service:
                from ..data.service import DataService
                service = DataService(settings)
            resolved = set()
            try:
                for query in dict.fromkeys(e.text for e in targets):
                    if query in excluded:
                        _clarify("同一证券同时被要求和排除，请明确研究对象", original, result, ctx)
                    if ctx.source_requests >= 20:
                        raise AgentError("SOURCE_BUDGET", "名称解析请求额度耗尽")
                    value = service.resolve_entity(query, ctx)
                    service.store.save_result(value)
                    if not value.data.get("selected"):
                        ctx.symbol = None
                        _clarify(ctx.clarification or "请确认研究对象", original, result, ctx, "entity", value.data.get("candidates", []))
                    resolved.add(value.data["selected"]["symbol"])
            finally:
                if own_service:
                    service.close()
            if len(resolved) != 1:
                ctx.symbol = None
                _clarify("当前研究支持单标的，请明确选择哪家公司", original, result, ctx, "entity")
            ctx.bind_symbol(next(iter(resolved)))
        elif result.entity_binding == "reference" and conversation.state.get("symbol") and not excluded:
            ctx.bind_symbol(conversation.state["symbol"])
        elif result.entity_binding == "absent" and symbol and not excluded and result.dialogue_act not in ("switch_entity", "correct"):
            from ..data.symbols import normalize_symbol
            ctx.bind_symbol(normalize_symbol(symbol))
        else:
            _clarify("请明确股票名称或代码；当前没有可唯一继承的对象", original, result, ctx, "entity")
    request_id = uuid4().hex
    ctx.nlu_admission = {"id": request_id, "goal": original, "task_type": result.task_type, "symbol": ctx.symbol,
                         "execution_mode": "chat", "depth": mode, "analysts": tuple(roles), "pipeline": pipeline, "as_of": ctx.as_of.isoformat(),
                         "news_days": normalized["news_days"], "policy_days": normalized["policy_days"], "horizon": normalized["horizon"]}
    def clarify_response(reply):
        # Ordinary-tool clarification also uses the same NLU, never the old
        # substring/year rules. Research mode changes require a new root task.
        state = copy.deepcopy(conversation)
        state.state.update(symbol=ctx.symbol, scope=copy.deepcopy(ctx.scope))
        waiting = Pending(original, result, "semantics", [], ctx.source_requests, ctx.as_of, time.monotonic())
        return prepare(reply, settings, state, parser=parser, service=service if not own_service else None,
                       pending=waiting)
    # own_service is only set on explicit entity resolution; don't capture a
    # service we just closed or an unbound local in reference/concept requests.
    own_service = service is None or locals().get("own_service", False)
    ctx.nlu_clarifier = clarify_response
    ctx.scope["nlu"] = {"backend": result.backend, "model_id": result.model_id, "bundle_sha256": result.bundle_sha256,
                        "model_sha256": result.model_sha256, "schema_version": result.schema_version, "prompt_sha256": result.prompt_sha256,
                        "quality_status": result.quality_status, "unqualified_authorized": result.unqualified_authorized,
                        "task_type": result.task_type, "sources": normalized["sources"], "parsed_request_id": request_id}
    ctx.frozen_symbols = frozenset([ctx.symbol]) if ctx.symbol else None
    ctx.frozen_as_of = ctx.as_of
    from ..data.evidence import write_json
    log = {"model_id": result.model_id, "backend": result.backend, "bundle_sha256": result.bundle_sha256,
           "model_sha256": result.model_sha256, "schema_version": result.schema_version, "prompt_sha256": result.prompt_sha256,
           "quality_status": result.quality_status, "unqualified_authorized": result.unqualified_authorized,
           "usage": result.usage,
           "elapsed_seconds": time.perf_counter() - started,
           "request_id": request_id, "admission": "accept", "task_type": result.task_type, "mode": mode, "roles": roles, "sources": normalized["sources"]}
    if settings.nlu.store_raw_text:
        log["text"] = original
    write_json(settings.data_dir / "nlu" / (request_id + ".json"), log)
    return PreparedRequest(original, result, ctx, mode, roles, pipeline, normalized["news_days"], normalized["policy_days"], normalized["horizon"], request_id)


def prepare(text, settings, conversation, **kwargs):
    started = time.perf_counter()
    try:
        return _prepare(text, settings, conversation, **kwargs)
    except AgentError as exc:
        from ..data.evidence import write_json
        pending = getattr(exc, "pending", None)
        log = {"backend": settings.nlu.backend, "error_code": exc.code, "reason": exc.message,
               "admission": "clarify" if exc.code == "NEEDS_CLARIFICATION" else "reject",
               "elapsed_seconds": time.perf_counter() - started}
        if pending:
            log.update(model_id=pending.result.model_id, bundle_sha256=pending.result.bundle_sha256,
                       reasons=pending.result.reasons, source_requests=pending.source_requests)
        if settings.nlu.store_raw_text:
            log["text"] = text
        write_json(settings.data_dir / "nlu" / (uuid4().hex + ".json"), log)
        raise
