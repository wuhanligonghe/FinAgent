from datetime import datetime, time
import re

from ..errors import AgentError
from ..runtime import TZ, now
from ..session import Conversation
from ..data.evidence import EvidenceStore
from .contracts import ResearchRequest, ROLES


def request_from_goal(goal, settings, *, symbol=None, as_of=None, analysts=None, depth=None, market_window=120, session_id=None,
                      pipeline=None, debate_rounds=None, risk_rounds=None, horizon=None, risk_profile=None, position_context=None):
    if depth not in (None, "specialist") or pipeline not in (None, "research") or any(v is not None for v in (debate_rounds, risk_rounds, horizon, risk_profile, position_context)):
        raise AgentError("MODE_REQUIRED", "请通过 /research 或 research --mode 显式选择研究流程")
    if settings.nlu.backend == "legacy_regex":
        if not analysts:
            raise AgentError("NEEDS_CLARIFICATION", "请指定分析维度；深度研究请使用 /research")
        return legacy_request_from_goal(goal, settings, symbol=symbol, as_of=as_of, analysts=analysts, depth="specialist",
            market_window=market_window, session_id=session_id, pipeline="research")
    from ..nlu.parser import prepare
    conversation = Conversation(EvidenceStore(settings.data_dir), session_id or "research-entry")
    if session_id is None:
        conversation.state = {"symbol": None, "scope": {}, "preferences": {}}
    prepared = prepare(goal, settings, conversation, symbol=symbol, as_of=as_of, research_options={"analysts": analysts})
    return prepared.research_request(settings, market_window=market_window, session_id=session_id)


def legacy_request_from_goal(goal, settings, *, symbol=None, as_of=None, analysts=None, depth="deep", market_window=120, session_id=None,
                      pipeline=None, debate_rounds=None, risk_rounds=None, horizon=None, risk_profile=None, position_context=None):
    if depth != "specialist" or pipeline not in (None, "research"):
        raise AgentError("MODE_REQUIRED", "旧规则不能启动深度研究，请显式选择模式")
    from .routing import pipeline_from_goal
    from .decision_contracts import DecisionRequest
    selected = pipeline or ("research" if depth == "specialist" else pipeline_from_goal(goal))
    if selected not in ("research", "trading") or depth == "specialist" and selected == "trading":
        raise AgentError("INVALID_RESEARCH_MODE", "完整决策须 deep；专项使用 research")
    if selected == "research" and any(x is not None for x in (debate_rounds, risk_rounds, horizon, risk_profile, position_context)):
        raise AgentError("INVALID_RESEARCH_MODE", "research 模式不能传决策链专用参数")
    decision_fields = {}
    if selected == "trading":
        decision_fields = {"debate_rounds": debate_rounds if debate_rounds is not None else settings.decision.debate_rounds,
                           "risk_rounds": risk_rounds if risk_rounds is not None else settings.decision.risk_rounds}
        match = re.search(r"(?:未来|接下来)\s*[一二三四六十\d]+(?:[—到至-][一二三四六十\d]+)?\s*(?:个?月|年|周|个?交易日)", goal)
        decision_fields["decision_horizon"] = horizon or (match[0] if match else "未来 1—3 个月")
        decision_fields["risk_profile"] = risk_profile or "unspecified"
        decision_fields["position_context"] = position_context or "unknown"
        # Validate modes/budget before any online name resolution.
        from .workflow import validate_config
        from .decision_contracts import DecisionScope
        validate_config(DecisionScope(goal=goal, symbol="000001.SZ", as_of=now(), analysts=analysts or list(ROLES), **decision_fields), settings.decision)
    cutoff = datetime.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    if cutoff is None:
        match = re.search(r"(?:截至|截止|截至到|截止到)\s*(20\d{2}-\d{2}-\d{2})", goal)
        if match:
            cutoff = datetime.combine(datetime.fromisoformat(match[1]).date(), time(23, 59, 59), TZ)
            if cutoff > now():
                cutoff = now() if cutoff.date() == now().date() else cutoff
    conversation = Conversation(EvidenceStore(settings.data_dir), session_id or "research-entry")
    if session_id is None:
        conversation.state = {"symbol": None, "scope": {}, "preferences": {}}
    ctx = conversation.context(goal, settings.limits, symbol=symbol, as_of=cutoff)
    if not ctx.symbol:
        from ..data.service import DataService
        explicit = re.findall(r"研究对象[:：]\s*([^；，。\n]{1,80})", goal)
        query = explicit[-1] if explicit else re.split(r"[；，。]|(?:从|的)(?:技术|基本|政策|情绪|新闻)", goal)[0]
        query = re.sub(r"截至\s*20\d{2}-\d{2}-\d{2}", "", query).strip()
        query = re.sub(r"^(?:请|帮我|对|深入分析|深度分析|深度研究|深入研究|分析|研究)+", "", query).strip()
        query = re.sub(r"(?:这只股票|股票|一下)$", "", query).strip()
        if query and len(query) <= 80 and query not in ("它", "这只", "该公司", "这家公司"):
            service = DataService(settings)
            try:
                value = service.resolve_entity(query, ctx)
                service.store.save_result(value)
            finally:
                service.close()
        if not ctx.symbol:
            raise AgentError("NEEDS_CLARIFICATION", ctx.clarification or "请明确要研究的沪深股票名称或代码；当前无法唯一确定")
    cls = DecisionRequest if selected == "trading" else ResearchRequest
    return cls(goal=goal, symbol=ctx.symbol, depth=depth, analysts=analysts or list(ROLES), as_of=ctx.as_of,
               market_window=market_window, session_id=session_id, resolution_source_requests=ctx.source_requests, **decision_fields)
