"""Recheck saved real responses OFFLINE; never relabel this as a live run."""
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from finagent.config import load_settings
from finagent.data.evidence import EvidenceStore, write_json
from finagent.research.contracts import AnalysisSubmission
from finagent.research.review import validate_claims, checked_subset
from finagent.runtime import ExecutionContext, now


def main():
    settings = load_settings()
    evidence = EvidenceStore(settings.data_dir)
    results = []
    for run in sorted((settings.data_dir / "phase2_validation").glob("model_*.json")):
        state = json.loads(run.read_text(encoding="utf-8"))["result"]
        directory = settings.data_dir / "research" / state["id"]
        for task in state["tasks"]:
            candidates, ctx = [], None
            for path in (directory / "tasks" / task["id"]).glob("*/loop/*/state.json"):
                body = json.loads(path.read_text(encoding="utf-8"))
                ctx = ExecutionContext(as_of=datetime.fromisoformat(body["as_of"]), symbol=body["symbol"])
                ctx.frozen_symbols = frozenset([state["scope"]["symbol"], "000300.SH"])
                ctx.scope = body["scope"]
                ctx.evidence_ids, ctx.result_tools = set(body["evidence_ids"]), body["result_tools"]
                ctx.invalidated_result_ids = set(body.get("invalidated_result_ids", []))
                messages = json.loads((path.parent / "messages.json").read_text(encoding="utf-8"))
                for message in messages:
                    for call in message.get("tool_calls") or []:
                        if call["function"]["name"] == "submit_analysis":
                            try:
                                candidates.append(json.loads(call["function"]["arguments"]))
                            except ValueError:
                                pass
            if ctx is not None:
                recovered = checked_subset(candidates, ctx, evidence)
                results.append({"source_research_id": state["id"], "task_id": task["id"], "original_status": task["status"],
                    "candidate_count": len(candidates), "checked_subset": recovered,
                    "semantic_review": "not_performed", "live_execution": False})
    output = settings.data_dir / "phase2_validation" / ("offline_response_audit_" + now().strftime("%Y%m%d_%H%M%S") + ".json")
    write_json(output, {"mode": "offline_saved_response_audit", "results": results, "note": "不改写原始真实模型失败，不代表新模型执行成功"})
    for source in sorted({r["source_research_id"] for r in results}):
        selected = [r for r in results if r["source_research_id"] == source and r["checked_subset"]]
        if not selected:
            continue
        lines = ["# 已保存真实响应的离线核验摘要", "这是离线重核，不是一次新模型研究；原始真实模型任务仍保留失败状态。",
                 "仅保留程序检查通过的部分主张，语义支持未完成人工审阅；内容状态：partial。", "来源研究：" + source]
        for row in selected:
            lines.append("## " + row["task_id"])
            lines.append(row["checked_subset"]["report_markdown"])
            for claim in row["checked_subset"]["claims"]:
                for ref in claim["evidence_refs"]:
                    value = evidence.load_result(ref["result_id"])
                    lines.append("- 来源：" + str(value.source) + "；定位：" + ref["locator"] + "；证据：" + ref["snapshot_id"])
        (output.parent / ("recovered_" + source + ".md")).write_text("\n\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output), "tasks": len(results), "with_checked_subset": sum(bool(r["checked_subset"]) for r in results)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
