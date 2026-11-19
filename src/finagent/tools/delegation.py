from __future__ import annotations

from datetime import datetime
from time import monotonic
from typing import Literal

from pydantic import Field

from ..config import StrictModel
from ..data.contracts import Observation
from ..errors import AgentError
from .registry import Tool


class StartResearchArgs(StrictModel):
    parsed_request_id: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")
    goal: str = Field(min_length=1, max_length=12000)
    analysts: list[str] = Field(default_factory=lambda: ["technical", "sentiment", "news", "fundamentals", "policy"], min_length=1, max_length=5)
    pipeline: Literal["research", "trading"] | None = None
    depth: Literal["deep", "specialist"] = "specialist"


class ReadResearchArgs(StrictModel):
    research_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    offset: int = Field(0, ge=0)
    limit: int = Field(6000, ge=1, le=12000)


def register_delegation(registry, service):
    def start(args, ctx):
        from ..research.entry import request_from_goal
        from ..research.coordinator import ResearchCoordinator
        if args.depth != "specialist" or args.pipeline not in (None, "research"):
            raise AgentError("RESEARCH_DENIED", "聊天工具只允许专项分析；深度研究请由用户使用 /research 显式选择")
        if not ctx.symbol:
            raise AgentError("NEEDS_CLARIFICATION", "先确认研究对象")
        started = monotonic()
        try:
            if service.settings.nlu.backend == "qwen3_ollama":
                a = ctx.nlu_admission
                if not a or args.parsed_request_id != a["id"] or a["task_type"] != "stock_analysis" or a["depth"] == "direct":
                    raise AgentError("RESEARCH_DENIED", "缺少本轮已确认的研究解析凭据")
                if (ctx.symbol != a["symbol"] or ctx.as_of.isoformat() != a["as_of"] or args.goal != a["goal"] or
                        args.depth != a["depth"] or set(args.analysts) != set(a["analysts"]) or args.pipeline not in (None, a["pipeline"])):
                    raise AgentError("SCOPE_MISMATCH", "研究工具参数与宿主确认范围不一致")
                from ..research.contracts import ResearchRequest
                from ..research.decision_contracts import DecisionRequest
                fields = dict(goal=a["goal"], symbol=a["symbol"], as_of=ctx.as_of, depth=a["depth"], analysts=list(a["analysts"]),
                              news_days=a["news_days"], policy_days=a["policy_days"], resolution_source_requests=0)
                if a["pipeline"] == "trading":
                    fields.update(debate_rounds=service.settings.decision.debate_rounds, risk_rounds=service.settings.decision.risk_rounds,
                                  decision_horizon=a["horizon"])
                request = (DecisionRequest if a["pipeline"] == "trading" else ResearchRequest)(**fields)
            else:
                request = request_from_goal(args.goal, service.settings, symbol=ctx.symbol, as_of=ctx.as_of, analysts=args.analysts, pipeline=args.pipeline, depth=args.depth)
            result = ResearchCoordinator(service.settings).run(request, cancelled=ctx.cancelled,
                        prior_usage={"model": ctx.model_attempts, "tool": ctx.tool_attempts, "source": ctx.source_requests})
        finally:
            # Delegated work has its own bounded wall-time; don't double-charge
            # its waiting duration against the ordinary outer 180s limit.
            ctx.started += monotonic() - started
        ctx.scope["research_id"] = result["id"]
        return Observation(status="ok" if result["content"] == "complete" else "partial", source="research_runtime",
                           data={"research_id": result["id"], "content_status": result["content"], "report_path": result["report"], "budget": result["budget"]})

    def read(args, ctx):
        from pathlib import Path
        from ..research.store import ResearchStore
        if ctx.scope.get("research_id") != args.research_id:
            raise AgentError("RESEARCH_DENIED", "研究未与当前任务关联")
        row = ResearchStore.from_settings(service.settings).get(args.research_id)
        import json
        stored_scope = json.loads(row["scope"])
        if datetime.fromisoformat(stored_scope["as_of"]) > ctx.as_of:
            raise AgentError("AFTER_CUTOFF", "研究报告晚于当前任务截止时间")
        if ctx.symbol and ctx.symbol != stored_scope["symbol"]:
            raise AgentError("SCOPE_MISMATCH", "报告所属证券与本轮研究对象不同")
        if not row["report"]:
            raise AgentError("REPORT_UNAVAILABLE", "研究尚无报告")
        path = Path(row["report"])
        expected = ResearchStore.from_settings(service.settings).directory(args.research_id)
        if not path.resolve().is_relative_to(expected.resolve()):
            raise AgentError("REPORT_DENIED", "报告路径无效")
        text = path.read_text(encoding="utf-8")
        # Adoption is explicit at read time, not from the conversation summary.
        # These old records are contextual evidence, never fresh perceptions.
        for grant in ResearchStore.from_settings(service.settings).grants(args.research_id):
            value = service.store.load_result(grant["result_id"])
            from ..research.review import check_result
            try:
                check_result(value, ctx)
            except AgentError:
                continue
            ctx.result_tools[value.result_id] = grant["tool"]
            ctx.evidence_ids.update([value.result_id, *value.snapshot_ids])
            if value.snapshot_id:
                ctx.evidence_ids.add(value.snapshot_id)
        return Observation(status="ok", source="research_artifact", data={"research_id": args.research_id, "text": text[args.offset:args.offset + args.limit],
                            "offset": args.offset, "total_chars": len(text), "original_scope": stored_scope,
                            "note": "宿主已按当前范围重新授予可用的旧研究证据；这些记录不代表本轮新鲜取数"})

    registry.register(Tool("start_stock_research", "仅启动宿主已确认的专项分析；不得自行启动深度研究或完整决策，缺少维度须询问用户", StartResearchArgs, start, False))
    registry.register(Tool("read_research_artifact", "回读已关联的研究报告；不能传路径或将报告当原始证据", ReadResearchArgs, read, False))
