from __future__ import annotations

import json
from uuid import uuid4
from ..data.evidence import write_json
from ..analysts.catalog import CATALOG
from .workflow import ROLE_TITLES


def display(value, labels=None):
    # Render accepted structured values, never generate another model narrative.
    if isinstance(value, list):
        return "\n".join("- " + display(x, labels) for x in value)
    if isinstance(value, dict):
        if {"artifact_id", "artifact_hash", "item_id"} <= value.keys():
            return (labels or {}).get((value["artifact_id"], value["item_id"]), "已登记依据")
        names = {"text": "内容", "claim_refs": "依据", "horizon": "判断期限", "assumptions": "假设", "invalidation_conditions": "失效条件",
                 "target": "回应对象", "stance": "态度", "reason": "理由", "verdict": "裁决", "severity": "严重度", "description": "问题",
                 "state": "状态", "calls": "实际调用", "model": "模型", "tool": "工具", "source": "来源", "cost": "费用",
                 "token_status": "用量状态", "prompt_tokens": "输入token", "completion_tokens": "输出token", "role": "角色"}
        return "；".join(f"{names.get(k, k)}：" + ("、".join(display(x, labels) for x in v) if isinstance(v, list) else display(v, labels)) for k, v in value.items()
                        if v not in (None, "", []) and k not in ("argument_id", "issue_id", "task_id", "artifact_id", "artifact_hash", "claim_id"))
    if value is None:
        return "未知"
    return {**ROLE_TITLES, "bull": "多头", "bear": "空头", "rebut": "反驳", "accept": "接受", "qualify": "附条件接受", "unresolved": "未解决", "conditional": "条件成立",
            "supported": "有依据", "refuted": "不支持", "open": "待处理", "resolved": "已解决", "withdrawn": "已撤回",
            "decided": "形成判断", "abstained": "暂不判断", "unavailable": "未交付", "reported": "已返回", "unknown": "未知",
            "full": "全部阶段交付", "complete": "完整", "partial": "部分完成", "pass": "通过", "limited": "受限", "blocked": "阻断",
            "unspecified": "未指定", "aggressive": "激进", "balanced": "均衡", "conservative": "保守", "not_held": "未持有", "held": "已持有",
            "Buy": "买入", "Overweight": "增持", "Hold": "持有", "Underweight": "减持", "Sell": "卖出", "Wait": "等待"}.get(str(value), str(value))


def render_artifact(a, reference_labels=None):
    keys = ("side", "perspective", "round", "decision_status", "rating", "action", "reason", "arguments", "responses",
            "concessions", "open_questions", "abstention_reason", "no_response_reason", "issue_rulings", "conditions",
            "preconditions", "action_triggers", "invalidation_conditions", "review_triggers", "risk_arguments", "assessment",
            "suggested_constraints", "accepted_rejected_views", "constraints", "unresolved", "limitations")
    labels = {"side": "立场", "perspective": "视角", "round": "轮次", "decision_status": "判断状态", "rating": "评级",
              "action": "方向", "reason": "理由", "arguments": "论点", "responses": "回应", "conditions": "成立条件",
              "invalidation_conditions": "失效条件", "review_triggers": "复核触发", "preconditions": "方案前提",
              "action_triggers": "行动触发", "risk_arguments": "风险论点", "assessment": "评估", "concessions": "承认事项",
              "unresolved": "未决问题", "accepted_rejected_views": "观点采纳与否决", "limitations": "限制",
              "issue_rulings": "争点裁决", "suggested_constraints": "建议约束", "constraints": "约束",
              "abstention_reason": "弃权原因", "no_response_reason": "无可回应论点原因", "open_questions": "待查问题"}
    return "\n\n".join(f"**{labels.get(k, k)}**：" + ("\n\n" if isinstance(a[k], list) else "") + display(a[k], reference_labels)
                        for k in keys if a.get(k) not in (None, "", []))


def export_decision(store, run, scope, artifacts, bundle, errors):
    directory = store.directory(run)
    stages = store.stages(run)
    full = bool(stages) and all(s["status"] == "completed" for s in stages)
    expert = [a for a in artifacts if a.get("analyst") in scope.analysts]
    process = "full" if full else "partial" if expert else "unavailable"
    deep = getattr(scope, "execution_mode", None) == "deep_research"
    final_role = "research_manager" if deep else "portfolio_manager"
    final = next((a for a in reversed(artifacts) if a.get("role") == final_role), None)
    content = "complete" if process == "full" and bundle.get("gate_status") == "pass" else "partial" if any(a.get("claims") for a in expert) else "unavailable"
    state = final["decision_status"] if final else "unavailable"
    labels = {}
    for a in artifacts:
        role = a.get("role", "")
        title = ROLE_TITLES.get(role, CATALOG[role].title if role in CATALOG else "研究审阅")
        if a.get("round"):
            title += f" · 第{a['round']}轮"
        labels[(a["artifact_id"], "$")] = title
        for key, identity in (("claims", "claim_id"), ("arguments", "argument_id"), ("risk_arguments", "argument_id")):
            for index, item in enumerate(a.get(key, []), 1):
                labels[(a["artifact_id"], item[identity])] = title + f" · {'主张' if key == 'claims' else '论点'}{index}"
    header = ["# FinSight 完整研究与决策报告", f"研究对象：{scope.symbol}；证据截止：{scope.as_of.isoformat()}。",
        "研究目标：" + scope.goal[:400],
        f"判断期限：{scope.decision_horizon}（相对证据截止）；流程：完整研究与决策；多空 {scope.debate_rounds} 轮，风险 {scope.risk_rounds} 轮。",
        f"流程状态：{display(process)}；内容状态：{display(content)}；最终判断：{display(state)}；质量门：{display(bundle.get('gate_status', 'unavailable'))}。",
        "通用单标的研究；风险偏好：" + display(scope.risk_profile) + "；持仓背景：" + display(scope.position_context) + "。交易执行可行性未知。"]
    if deep:
        header[0] = "# FinSight 深度研究报告"
        header[3] = f"判断期限：{scope.decision_horizon}；流程：深度研究报告；多空 {scope.debate_rounds} 轮；研究经理裁决后结束。"
        header[5] = "本报告为研究结论，未执行交易方案、风险团队或投资组合经理阶段。"
    header.insert(1, "执行模式：" + getattr(scope, "execution_mode", "历史研究流程") + "；流程版本：" + scope.workflow_version)
    if final:
        header.extend(["## 最终判断", render_artifact(final, labels)])
    else:
        header.extend(["## 最终判断未完成", "没有本模式最终裁决产物，不能用上游产物冒充完成。"])
    header.append("## 分析师研究与质量检查")
    for role in scope.analysts:
        header.append(f"### {CATALOG[role].title}：{display(bundle.get('coverage', {}).get(role, 'unavailable'))}")
        for a in expert:
            if a["analyst"] == role:
                header.append(a.get("report_markdown", a.get("summary", "")))
                header.extend("- " + x for x in [*a.get("missing", []), *a.get("limitations", [])])
    header += ["### 质量门及未决问题", display(bundle.get("issues", []), labels), display(bundle.get("excluded", []), labels)]
    grouped = {"debate": [a for a in artifacts if a.get("role") in ("bull", "bear")],
               "risk": [a for a in artifacts if a.get("role", "").startswith("risk_")]}
    for key, title in (("debate", "多空交替辩论"), ("research_manager", "研究经理裁决"), ("trader", "交易员研究方案"), ("risk", "三方风险讨论")):
        if deep and key in ("trader", "risk"):
            continue
        selected = grouped.get(key, [a for a in artifacts if a.get("role") == key])
        header.append("## " + title)
        header += [f"### {labels[(a['artifact_id'], '$')]}\n\n{render_artifact(a, labels)}" for a in selected]
        if not selected:
            header.append("本阶段未交付，原因见阶段状态。")
    header.append("## 阶段状态与限制")
    status_names = {"completed": "已交付", "failed": "失败", "skipped": "未执行", "cancelled": "已取消"}
    header += [f"- {ROLE_TITLES.get(s['role'], CATALOG[s['role']].title if s['role'] in CATALOG else '质量审阅')}：{status_names.get(s['status'], s['status'])}" + ("；" + s["reason"] if s["reason"] else "") for s in stages]
    header += ["- " + e for e in errors]
    header.append("## 来源、计算与主张索引")
    references = []
    for a in expert:
        for c in a.get("claims", []):
            header.append(f"- {labels[(a['artifact_id'], c['claim_id'])]}：{c['text']}")
            for r in c.get("evidence_refs", []):
                from ..data.evidence import EvidenceStore
                value = EvidenceStore(store.root).load_result(r["result_id"])
                references.append({**r, "claim": f"{a['artifact_id']}:{c['claim_id']}", "source": value.source, "source_url": value.source_url})
                header.append(f"  - {value.source}；{r['locator']}；{value.source_url or ''}")
            header += ["  - 确定性计算：" + x for x in c.get("calculation_refs", [])]
    header += ["## 实际预算", display(store.usage(run)), "尚未验证未来收益。程序检查通过不表示自然语言判断或预测已获证明。"]
    report = {"schema_version": "2.1", "research_id": run, "scope": scope.model_dump(mode="json"), "process_status": process,
              "content_status": content, "review_status": bundle.get("review_status", "unavailable"), "decision_status": state,
              "coverage": bundle.get("coverage", {}), "quality_gate": bundle, "portfolio_decision": None if deep else final, "research_decision": final if deep else None,
              "execution_mode": getattr(scope, "execution_mode", "legacy"), "workflow_version": scope.workflow_version,
              "artifacts": artifacts, "stages": stages, "errors": errors, "outcome_verified": False,
              "semantic_support": "requires_human_sampling", "budget": store.usage(run),
              "report_markdown": "\n\n".join(x for x in header if x) + "\n"}
    path = directory / "report.md"
    path.write_text(report["report_markdown"], encoding="utf-8")
    write_json(directory / "report.json", report)
    write_json(directory / "evidence_manifest.json", {"references": references, "grants": store.grants(run)})
    if not (directory / "research_bundle.json").exists():
        write_json(directory / "research_bundle.json", bundle)
    for key, selected in grouped.items():
        filename = "risk_discussion" if key == "risk" else key
        write_json(directory / (filename + ".json"), selected)
        (directory / (filename + ".md")).write_text("\n\n".join(render_artifact(a, labels) for a in selected), encoding="utf-8")
    for role, filename in (("quality_gate", "quality_gate"), ("research_manager", "research_plan"), ("trader", "trader_proposal"), ("portfolio_manager", "portfolio_decision")):
        selected = next((a for a in artifacts if a.get("role") == role), None)
        if selected:
            write_json(directory / (filename + ".json"), selected)
        elif (directory / (filename + ".json")).exists():
            history = directory / "exports_history"
            history.mkdir(exist_ok=True)
            (directory / (filename + ".json")).replace(history / (filename + "_" + uuid4().hex + ".json"))
    return report, path


def read_trace(store, run, stage="all"):
    store.get(run)
    accepted = []
    for t in store.tasks(run):
        if t["status"] != "completed":
            continue
        a = store.read_artifact(t)
        if not a or a.get("workflow_version") not in ("2.1", "2.2"):
            continue
        role = a.get("role", "")
        if stage == "all" or stage == "debate" and role in ("bull", "bear") or stage == "risk" and role.startswith("risk_") or stage == role:
            accepted.append(a)
    return {"research_id": run, "stage": stage, "artifacts": accepted, "stages": store.stages(run)}
