"""Host dispatch for already classified research artifact requests."""
from pathlib import Path
from ..errors import AgentError


def artifact_request(prepared, settings, conversation, progress=None):
    task = prepared.result.task_type
    if task not in ("research_status", "research_report", "research_resume"):
        return None
    research_id = conversation.state.get("last_research_id")
    if not research_id:
        raise AgentError("RESEARCH_REQUIRED", "当前会话没有研究，请使用显式 research-status/report/resume 命令提供研究ID")
    from ..research.store import ResearchStore
    store = ResearchStore.from_settings(settings)
    if task == "research_resume":
        raise AgentError("NEEDS_CLARIFICATION", f"继续研究会消耗原任务剩余额度，请使用 /research-resume {research_id} 显式恢复；查看报告无需恢复。")
    else:
        state = store.snapshot(research_id)
    if task == "research_report":
        if not state["report"]:
            raise AgentError("REPORT_UNAVAILABLE", "研究尚无报告")
        path = Path(state["report"]).resolve()
        if not path.is_relative_to(store.directory(research_id).resolve()):
            raise AgentError("REPORT_DENIED", "报告路径无效")
        return {"id": research_id, "report_text": path.read_text(encoding="utf-8")}
    return state
