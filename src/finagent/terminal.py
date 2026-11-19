"""Natural-language run completion, separate from opt-in financial verification."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from .errors import AgentError


@dataclass(frozen=True)
class TerminalResponse:
    answer: str
    status: str = "completed"
    format: str = "text"
    evidence_ids: tuple[str, ...] = ()
    calculation_ids: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


def _strings(value):
    return tuple(item for item in value if isinstance(item, str) and item.strip()) if isinstance(value, list) else ()


def read_terminal_response(content) -> TerminalResponse:
    """Require visible text, not a schema. Unwrap old replies for compatibility only."""
    if not isinstance(content, str):
        if content is None:
            raise AgentError("EMPTY_RESPONSE", "模型未返回可见的最终回复")
        raise AgentError("MODEL_PROTOCOL_ERROR", "最终回复必须是文本")
    text = content.strip()
    if not text:
        raise AgentError("EMPTY_RESPONSE", "模型未返回可见的最终回复")
    try:
        previous = json.loads(text, strict=False)
    except ValueError:
        previous = None
    if (isinstance(previous, dict) and isinstance(previous.get("status"), str)
            and previous["status"] in {"completed", "partial", "needs_clarification"}
            and isinstance(previous.get("answer"), str)):
        answer = previous["answer"].strip()
        if not answer:
            raise AgentError("EMPTY_RESPONSE", "模型未返回可见的最终回复")
        return TerminalResponse(answer, previous["status"], "legacy_json",
                                _strings(previous.get("evidence_ids")),
                                _strings(previous.get("calculation_ids")),
                                _strings(previous.get("missing")))
    return TerminalResponse(text)


def response_records(response, ctx, store, *, require_data):
    """Describe observed coverage and sources without rejecting or rewriting prose.

    These records establish resource provenance, not sentence-level correctness.
    Missing observations downgrade runtime coverage without another model call.
    """
    usable = {key: store.load_result(key) for key in ctx.result_tools if key not in ctx.invalidated_result_ids}
    usable = {key: value for key, value in usable.items() if value.status in ("ok", "partial") and value.data}
    perceptions = {key: value for key, value in usable.items()
                   if key in ctx.perception_results and value.snapshot_id and store.has_raw(value.snapshot_id)}
    raw = sorted({snapshot for value in usable.values() for snapshot in [value.snapshot_id, *value.snapshot_ids]
                  if snapshot and store.has_raw(snapshot)})
    calculations = sorted(key for key in usable
                          if ctx.result_tools[key] in {"calculate_market_metrics", "execute_program", "calculate_closing_review", "calculate_technical_divergence"})
    references = set(re.findall(r"(?:raw_[a-f0-9]{64}|result_[a-f0-9]{32})", response.answer))
    references.update(response.evidence_ids)
    references.update(response.calculation_ids)
    warnings = []
    if require_data and not perceptions:
        warnings.append({"code": "DATA_UNAVAILABLE", "message": "本次数据任务尚未取得可用观测"})
    missing_tools = sorted(ctx.required_tools - {ctx.result_tools[key] for key in usable})
    if missing_tools:
        warnings.append({"code": "REQUIRED_TOOL_MISSING", "message": "尚缺任务所需工具结果：" + ", ".join(missing_tools)})
    if any(value.stale for value in perceptions.values()):
        warnings.append({"code": "STALE_DATA", "message": "本次观测包含过期缓存"})
    if any(ctx.result_tools[key] == "calculate_closing_review" and not value.data.get("complete")
           for key, value in usable.items()):
        warnings.append({"code": "INCOMPLETE_REVIEW", "message": "收盘复盘仍有数据完整性缺口"})
    if any(ctx.result_tools[key] == "calculate_technical_divergence" and not value.data.get("complete")
           for key, value in usable.items()):
        warnings.append({"code": "INCOMPLETE_DIVERGENCE", "message": "背离分析有数据缺口或仅能识别快照候选"})
    return {
        "available_evidence_ids": raw,
        "available_calculation_ids": calculations,
        "cited_evidence_ids": [key for key in raw if key in references],
        "cited_calculation_ids": [key for key in calculations if key in references],
        "unresolved_reference_ids": sorted(references - set(raw) - set(usable)),
        "completion_warnings": warnings,
    }
