"""One-time cleanup when resuming sessions written before Task Frame removal.

Durable transcripts stay intact. Only the working checkpoint and context views
are migrated; user messages, TODOs, evidence IDs and explicit cutoffs survive.
"""
import json


RETIRED_TOOLS = {"update_task_frame", "read_task_frames"}
FRAME_KEYS = {"task_frames", "active_task_frame_id", "task_frame_events", "task_frame_requests"}
SCOPE_KEYS = {"report_period", "metric", "task_type", "task_frame_id"}
CONSTRAINT_KEYS = {"report_period", "allowed_report_periods", "profit_metrics", "financial_basis",
                   "consolidation", "currency", "task_include", "task_exclude", "excluded_metrics"}


def clean_scope(context):
    scope = context.get("scope", {})
    if "task_frame_id" in scope:
        for key in SCOPE_KEYS:
            scope.pop(key, None)
        for key in CONSTRAINT_KEYS:
            context.get("scope_constraints", {}).pop(key, None)


def migrate_session(saved):
    if not saved:
        return
    state = saved["state"]
    for key in FRAME_KEYS:
        state.pop(key, None)
    for name in RETIRED_TOOLS:
        state.get("_tool_catalog", {}).pop(name, None)
    for entry in state.get("evidence_registry", {}).values():
        entry.pop("task_id", None)
        entry.pop("task_revision", None)
    clean_scope(saved.get("context", {}))
    for message in saved.get("messages", []):
        # Keep assistant/tool history paired. Retired calls remain historical
        # observations, but cannot be discovered or executed in the live pool.
        refs = message.get("_tool_references", [])
        if refs:
            message["_tool_references"] = [r for r in refs if r.get("tool_name") not in RETIRED_TOOLS]
        for block in message.get("_anthropic_content", []):
            if block.get("type") == "tool_search_tool_result" and isinstance(block.get("content"), dict):
                content = block["content"]
                content["tool_references"] = [r for r in content.get("tool_references", [])
                                               if r.get("tool_name") not in RETIRED_TOOLS]
        if message.get("_finagent_origin") != "runtime_context":
            continue
        try:
            view = json.loads(message["content"])
        except (ValueError, TypeError, KeyError):
            continue
        if ("task_understanding" not in view
                and not FRAME_KEYS.intersection(view.get("session", {}))
                and "task_frame_id" not in view.get("scope", {})):
            continue
        view.pop("task_understanding", None)
        for key in FRAME_KEYS:
            view.get("session", {}).pop(key, None)
        clean_scope(view)
        message["content"] = json.dumps(view, ensure_ascii=False)
