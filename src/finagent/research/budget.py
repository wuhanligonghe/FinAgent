from __future__ import annotations

import json
import time
from uuid import uuid4

from ..errors import AgentError
from ..runtime import now


class ResearchBudget:
    def __init__(self, store, run, owner, config, cancelled):
        self.store, self.run, self.owner, self.config, self.cancelled = store, run, owner, config, cancelled

    def remaining(self, reserved=False):
        if self.cancelled.is_set():
            raise AgentError("CANCELLED", "研究已取消")
        row = self.store.get(self.run)
        if row["owner"] != self.owner or row["status"] != "running":
            raise AgentError("STALE_RESEARCH_OWNER", "研究执行权已失效")
        elapsed = row["active_used"] + max(0, time.time() - row["segment_started"])
        remaining = self.config.wall_seconds - elapsed - self.held("time", reserved)
        if remaining <= 0:
            raise AgentError("TIME_BUDGET", "研究活动时间额度耗尽")
        return remaining

    def held(self, kind, access=False):
        names = {"model": "reserve_model", "tool": "reserve_tool", "source": "reserve_source", "time": "reserve_seconds"}
        if hasattr(self.config, "final_model"):
            if access == "final":
                return 0
            if access == "downstream":
                return {"model": self.config.final_model, "tool": self.config.final_tool,
                        "source": 0, "time": self.config.final_seconds}[kind]
            return getattr(self.config, names[kind])
        return 0 if access else getattr(self.config, names[kind])

    def reserve(self, kind, task, reserved=False):
        self.remaining(reserved)
        total = {"model": self.config.model_calls, "tool": self.config.tool_calls, "source": self.config.source_requests}[kind]
        held = self.held(kind, reserved)
        return self.store.reserve_budget(self.run, self.owner, kind, task, total, held)

    def settle(self, operation, usage=None, state="finished"):
        # Unknown or interrupted requests remain charged.
        self.store.settle_reservation(self.run, self.owner, operation, usage, state)


class BudgetedModel:
    def __init__(self, model, budget, task, reserved=False):
        self.model, self.budget, self.task, self.reserved = model, budget, task, reserved
        self.settings = getattr(model, "settings", None)
        self.protocol = getattr(model, "protocol", "synthetic")

    def complete(self, messages, tools, timeout):
        operation = self.budget.reserve("model", self.task, self.reserved)
        try:
            reply = self.model.complete(messages, tools, min(timeout, self.budget.config.request_seconds, self.budget.remaining(self.reserved)))
            self.budget.settle(operation, reply.usage or None)
            self.budget.remaining(self.reserved)
            return reply
        except BaseException:
            self.budget.settle(operation, state="failed")
            raise
