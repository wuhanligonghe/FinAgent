"""Track actual visible character ranges; archived text may be read again."""
import json

from .errors import AgentError


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def next_unread(ranges, offset):
    for start, end in merge_ranges(ranges):
        if start <= offset < end:
            offset = end
    return offset


def visible_ranges(messages, key, field="evidence_id"):
    ranges = []
    tool_name = "read_evidence" if field == "evidence_id" else "read_output"
    calls = {call.get("id") for message in messages if message.get("role") == "assistant"
             for call in message.get("tool_calls", [])
             if call.get("function", {}).get("name") == tool_name}
    for message in messages:
        if message.get("role") != "tool" or message.get("tool_call_id") not in calls:
            continue
        try:
            body = json.loads(message.get("content") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict):
            continue
        data = body.get("data", body)
        if not isinstance(data, dict) or data.get(field) != key:
            continue
        text, offset = data.get("text"), data.get("offset", 0)
        if isinstance(text, str) and type(offset) is int:
            ranges.append([offset, offset + len(text)])
    return merge_ranges(ranges)


def original_evidence(store, ctx, requested):
    """A read result is an alias, never a new source or another user's grant."""
    current, seen = requested, set()
    for _ in range(16):
        if current not in ctx.evidence_ids:
            raise AgentError("EVIDENCE_DENIED", "原始证据未在本任务登记")
        if current in seen:
            raise AgentError("EVIDENCE_ALIAS_CYCLE", "回读记录包含循环引用")
        seen.add(current)
        if ctx.result_tools.get(current) != "read_evidence":
            return current
        current = store.load_result(current).data.get("evidence_id")
        if not isinstance(current, str):
            raise AgentError("EVIDENCE_ALIAS_INVALID", "回读记录缺少原始证据ID")
    raise AgentError("EVIDENCE_ALIAS_LIMIT", "回读记录嵌套过深，请使用原始证据ID")
