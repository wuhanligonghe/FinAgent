"""Host-owned execution choices. Language models cannot mint a selection."""
from __future__ import annotations

from datetime import datetime
import json
from typing import Literal
from uuid import uuid4
from pydantic import Field, model_validator

from ..errors import AgentError
from ..runtime import now
from .contracts import ROLES
from .decision_contracts import DecisionRequest, DecisionScope
from .store import ResearchStore

MODES = {"deep_research": "深度研究报告", "full_decision": "完整研究与决策"}


class SelectedRequest(DecisionRequest):
    workflow_version: Literal["2.2"] = "2.2"
    execution_mode: Literal["deep_research", "full_decision"]
    selection_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    selection_source: Literal["terminal_menu", "cli_flag", "chat_request"]
    risk_rounds: int = Field(0, ge=0, le=3)
    parent_research_id: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def valid_mode(self):
        if self.execution_mode == "deep_research" and (self.risk_rounds or self.risk_profile != "unspecified" or self.position_context != "unknown"):
            raise ValueError("深度研究报告不接受风险讨论或持仓参数")
        if self.execution_mode == "full_decision" and not self.risk_rounds:
            raise ValueError("完整决策至少一轮风险讨论")
        return self


class SelectedScope(DecisionScope, SelectedRequest):
    workflow_version: Literal["2.2"] = "2.2"
    risk_rounds: int = Field(0, ge=0, le=3)


def config_for(scope, settings):
    cfg = settings.decision
    if getattr(scope, "execution_mode", None) != "deep_research":
        return cfg
    downstream = 8 * scope.debate_rounds + 4
    upstream = len(scope.analysts) * cfg.expert_model_calls + 4 + 2 * cfg.followup_model_calls + 4
    return type(cfg).model_validate({**cfg.model_dump(), "model_calls": min(cfg.model_calls, upstream + downstream),
        "reserve_model": downstream, "tool_calls": min(cfg.tool_calls, cfg.tool_calls - cfg.reserve_tool + 16),
        "reserve_tool": 16, "wall_seconds": min(cfg.wall_seconds, 1080), "reserve_seconds": 240,
        "final_model": 2, "final_tool": 4, "final_seconds": 120,
        "max_attempts": min(cfg.max_attempts, len(scope.analysts) + 7 + 2 * scope.debate_rounds)})


def select(settings, *, mode, symbol, source, goal="", analysts=None, as_of=None, debate_rounds=None,
           risk_rounds=None, horizon=None, risk_profile=None, position_context=None, session_id=None,
           market_window=120, resolution_source_requests=0, parent_research_id=None):
    """Called only from trusted command/menu handlers with explicit typed fields."""
    if mode not in MODES:
        raise AgentError("MODE_REQUIRED", "请选择 deep_research 或 full_decision")
    if mode == "deep_research" and any(v is not None for v in (risk_rounds, risk_profile, position_context)):
        raise AgentError("INVALID_RESEARCH_MODE", "深度研究报告不接受风险讨论专用参数")
    cutoff = datetime.fromisoformat(as_of) if isinstance(as_of, str) else as_of or now()
    request = SelectedRequest(goal=goal or MODES[mode], symbol=symbol, as_of=cutoff,
        execution_mode=mode, selection_id=uuid4().hex, selection_source=source,
        analysts=list(ROLES) if analysts is None else analysts, session_id=session_id, market_window=market_window,
        debate_rounds=debate_rounds or settings.decision.debate_rounds,
        risk_rounds=(risk_rounds or settings.decision.risk_rounds) if mode == "full_decision" else 0,
        decision_horizon=horizon or "未来 1—3 个月", risk_profile=risk_profile or "unspecified",
        position_context=position_context or "unknown", resolution_source_requests=resolution_source_requests,
        parent_research_id=parent_research_id)
    scope = SelectedScope(**request.model_dump())
    from .workflow import validate_config
    validate_config(scope, config_for(scope, settings))
    store = ResearchStore.from_settings(settings)
    store.create_selection(request.selection_id, scope.scope_hash, source)
    return request


def consume(store, scope):
    store.consume_selection(scope)


def bind_run(store, scope, run):
    store.bind_selection(scope, run)
    store.event(run, "user_selection", selection_id=scope.selection_id, source=scope.selection_source,
                scope_hash=scope.scope_hash, execution_mode=scope.execution_mode)


def authorize_resume(store, run):
    """Only original scope is authorized; history/model prose never grants access."""
    row = store.get(run)
    scope = json.loads(row["scope"])
    events = [e for e in store.events(run) if e["kind"] in ("user_selection", "explicit_user_start")]
    valid = any(json.loads(e["body"]).get("scope_hash") == scope.get("scope_hash") for e in events)
    if not valid:
        raise AgentError("LEGACY_SELECTION_REQUIRED", "无法核验此任务的用户启动记录；保留只读，请显式选择模式后新建关联研究")
    store.event(run, "explicit_resume", authorization_id=uuid4().hex, scope_hash=scope["scope_hash"],
                workflow_version=scope.get("workflow_version", "legacy"))


def resume_selected(settings, run, **options):
    authorize_resume(ResearchStore.from_settings(settings), run)
    from .coordinator import ResearchCoordinator
    return ResearchCoordinator(settings).resume(run, **options)
