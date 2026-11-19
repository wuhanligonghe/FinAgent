from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from threading import Event
from time import monotonic
from zoneinfo import ZoneInfo
from typing import Any

from .config import Limits
from .errors import AgentError

TZ = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    return datetime.now(TZ)


@dataclass
class ExecutionContext:
    limits: Limits = field(default_factory=Limits)
    as_of: datetime = field(default_factory=lambda: now())
    symbol: str | None = None
    cancelled: Event = field(default_factory=Event)
    started: float = field(default_factory=monotonic)
    source_requests: int = 0
    evidence_ids: set[str] = field(default_factory=set)
    evidence_read_ranges: dict = field(default_factory=dict)
    evidence_read_visible: Any = None
    clarification: str | None = None
    source_events: list[dict] = field(default_factory=list)
    run_id: str | None = None
    run_dir: Path | None = None
    result_tools: dict[str, str] = field(default_factory=dict)
    perception_results: set[str] = field(default_factory=set)
    required_tools: set[str] = field(default_factory=set)
    document_ids: set[str] = field(default_factory=set)
    retrieval_rounds: int = 0
    calculation_attempts: int = 0
    calculation_failures: int = 0
    scope: dict = field(default_factory=dict)
    event_search_rounds: int = 0
    cutoff_explicit: bool = False
    invalidated_result_ids: set[str] = field(default_factory=set)
    scope_constraints: dict = field(default_factory=dict)
    conversation_history: list[dict] = field(default_factory=list)
    queried_symbols: set[str] = field(default_factory=set)
    research_budget: Any = None
    research_task: str = ""
    research_reserved: bool = False
    frozen_symbols: frozenset[str] | None = None
    frozen_as_of: datetime | None = None
    submitted_analysis: dict | None = None
    grant_result: Any = None
    output_base: Path | None = None
    submission_attempts: int = 0
    submit_reserve: int = 2
    model_attempts: int = 0
    tool_attempts: int = 0
    # Ephemeral host admission. Never restore research permission from scope/history.
    nlu_admission: dict | None = None
    nlu_clarifier: Any = None
    execution_mode: str = "chat"
    loaded_skills: dict[str, str] = field(default_factory=dict)
    operation_ledger: Any = None
    cwd: Path | None = None
    actor: str = "lead"
    assignment: dict | None = None
    call_ids: set[str] = field(default_factory=set)

    def __post_init__(self):
        if self.as_of.tzinfo is None or self.as_of > now():
            raise AgentError("INVALID_CUTOFF", "截止时间必须带时区且不晚于当前时间")

    def check(self) -> float:
        if self.cancelled.is_set():
            raise AgentError("CANCELLED", "用户取消了任务")
        remaining = self.limits.wall_seconds - (monotonic() - self.started)
        if remaining <= 0:
            raise AgentError("TIME_BUDGET", "任务时间预算耗尽")
        if self.frozen_as_of is not None and self.as_of != self.frozen_as_of:
            raise AgentError("SCOPE_MISMATCH", "专家不能更改研究截止时间")
        if self.research_budget is not None:
            remaining = min(remaining, self.research_budget.remaining(self.research_reserved))
        return remaining

    def request_timeout(self, configured: float) -> float:
        return min(configured, self.limits.request_seconds, self.check())

    def source_request(self):
        self.check()
        if self.source_requests >= self.limits.source_requests:
            raise AgentError("SOURCE_BUDGET", "数据源请求预算耗尽")
        if self.operation_ledger is not None:
            self.operation_ledger.charge("source")
        operation = None
        if self.research_budget is not None:
            operation = self.research_budget.reserve("source", self.research_task, self.research_reserved)
        self.source_requests += 1
        return operation

    def bind_symbol(self, symbol: str):
        """Select the current instrument; a task may query several securities."""
        if self.frozen_symbols is not None and symbol not in self.frozen_symbols:
            raise AgentError("SCOPE_MISMATCH", "证券不在冻结的研究范围内")
        self.symbol = symbol
        self.queried_symbols.add(symbol)
