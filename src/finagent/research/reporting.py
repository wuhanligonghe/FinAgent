from __future__ import annotations

import json

from ..analysts.catalog import CATALOG
from ..data.evidence import EvidenceStore, write_json


def export_report(store, run, scope, artifacts, review, synthesis, errors):
    directory = store.directory(run)
    status_by_role = {role: "unavailable" for role in scope.analysts}
    for artifact in artifacts:
        status_by_role[artifact["analyst"]] = artifact["content_status"]
    review_status = "unavailable" if review is None else "issues_found" if review["issues"] else "passed"
    content = "complete" if all(s == "complete" for s in status_by_role.values()) and review_status == "passed" and synthesis else "partial"
    if not any(a.get("claims") for a in artifacts):
        content = "unavailable"
    if synthesis:
        body = synthesis["report_markdown"]
    else:
        body = "综合研判未完成。以下为已接纳的各维分析与缺口。\n\n" + "\n\n".join(
            f"## {CATALOG[a['analyst']].title}\n\n{a['report_markdown']}" for a in artifacts)
    lines = [f"# FinSight 股票研究\n\n研究对象：{scope.symbol}；截止时间：{scope.as_of.isoformat()}。",
             "本次维度：" + "、".join(CATALOG[r].title for r in scope.analysts) + "。", body,
             "## 覆盖与限制\n\n内容状态：" + content + "；交叉审阅：" + review_status + "。",
             *[f"- {CATALOG[r].title}：{state}" for r, state in status_by_role.items()]]
    for a in artifacts:
        lines.extend(f"- {CATALOG[a['analyst']].title}：{gap}" for gap in [*a.get("missing", []), *a.get("limitations", [])])
    lines.extend("- " + e for e in errors)
    if review and review["issues"]:
        lines.append("\n## 交叉审阅与待核对事项\n")
        lines.extend(f"- {i['task_id']} / {i['claim_id']}：{i['description']}" for i in review["issues"])
    lines.append("\n## 来源与关键主张\n")
    evidence = EvidenceStore(store.root)
    references, event_signatures = [], set()
    for a in artifacts:
        for claim in a.get("claims", []):
            lines.append(f"- [{a['task_id']}:{claim['claim_id']}] {claim['text']}")
            for ref in claim["evidence_refs"]:
                value = evidence.load_result(ref["result_id"])
                label = value.source or "已登记原始材料"
                lines.append(f"  - {label}，定位 {ref['locator']}" + (f"，{value.source_url}" if value.source_url else ""))
                references.append({**ref, "claim": f"{a['task_id']}:{claim['claim_id']}", "source": label, "source_url": value.source_url})
                event_signatures.add(ref["snapshot_id"])
            for calculation in claim["calculation_refs"]:
                lines.append(f"  - 确定性计算：{calculation}")
    text = "\n\n".join(lines) + "\n"
    path = directory / "report.md"
    path.write_text(text, encoding="utf-8")
    report = {"schema_version": "1", "research_id": run, "scope": scope.model_dump(mode="json"), "content_status": content,
              "process_status": "full" if synthesis and review is not None and all(s == "complete" for s in status_by_role.values()) else "partial" if artifacts else "unavailable",
              "decision_status": None, "decision_not_applicable_reason": "pipeline_research",
              "review_status": review_status, "coverage": status_by_role, "report_markdown": text,
              "artifacts": artifacts, "review": review, "synthesis": synthesis, "errors": errors,
              "outcome_verified": False, "host_reference_checks": True, "semantic_support": "requires_human_sampling",
              "distinct_source_snapshots": len(event_signatures), "budget": store.usage(run)}
    write_json(directory / "report.json", report)
    write_json(directory / "review.json", review)
    write_json(directory / "evidence_manifest.json", {"references": references, "grants": store.grants(run)})
    return report, path


def export_state(store, run):
    write_json(store.directory(run) / "manifest.json", store.snapshot(run))
    events = store.events(run)
    (store.directory(run) / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
