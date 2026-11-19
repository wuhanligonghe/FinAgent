"""Paired conversation-level statistics and conservative claim generation."""
from __future__ import annotations

from collections import defaultdict
import csv
import json
from math import ceil, log2
from pathlib import Path
import random
import statistics

from ..provenance import package_digest


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def quantile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def aggregate(records):
    by_arm = defaultdict(list)
    for record in records:
        by_arm[record["arm"]].append(record)
    result = {}
    for arm, rows in by_arm.items():
        turns = [turn for row in rows for turn in row["turns"]]
        executed = [turn for turn in turns if turn["runtime_status"] != "not_run"]
        grades = [turn["grade"] for turn in turns]
        total = lambda key: sum(g.get(key, 0) for g in grades)
        successes = sum(row["dialogue_success"] for row in rows)
        prompts = sum(t["usage"].get("prompt_tokens", 0) for t in turns)
        completions = sum(t["usage"].get("completion_tokens", 0) for t in turns)
        result[arm] = dict(dialogue_runs=len(rows), unique_dialogues=len({r["case_id"] for r in rows}),
            dialogue_successes=successes, dialogue_success_rate=ratio(successes, len(rows)),
            turns=len(turns), executed_turns=len(executed), turn_success_rate=ratio(sum(g["task_success"] for g in grades), len(turns)),
            output_contract_rate=ratio(sum(g.get("contract_complete", g["format_valid"]) for g in grades), len(turns)),
            json_parse_rate=ratio(sum(g["format_valid"] for g in grades), len(turns)),
            slot_accuracy=ratio(total("slot_correct"), total("slot_total")), slot_total=total("slot_total"),
            numeric_recall=ratio(total("claim_correct"), total("claim_required")), claim_required=total("claim_required"),
            citation_support_rate=ratio(total("citation_supported"), total("citation_claims")), citation_claims=total("citation_claims"),
            unsupported_numeric_claim_rate=ratio(total("unsupported_numeric_claims"), total("claim_emitted")), claim_emitted=total("claim_emitted"),
            undeclared_text_amounts=total("undeclared_text_amounts"), text_amount_count=total("text_amount_count"),
            unsupported_text_amounts=total("unsupported_text_amounts"),
            unnecessary_clarifications=sum(g["unnecessary_clarification"] for g in grades),
            latency_p50_seconds=quantile([t["elapsed_seconds"] for t in executed], .5),
            latency_p95_seconds=quantile([t["elapsed_seconds"] for t in executed], .95),
            prompt_tokens=prompts, completion_tokens=completions,
            tokens_per_turn=ratio(prompts + completions, len(executed)),
            model_calls=sum(t["model_calls"] for t in turns), tool_calls=sum(t["tool_calls"] for t in turns))
    return result


def paired_comparison(records, baseline, treatment, seed=17, samples=2000):
    indexed = {(r["case_id"], r["repeat"], r["arm"]): r for r in records}
    by_case = defaultdict(list)
    for case_id, repeat, arm in indexed:
        if arm != baseline:
            continue
        other = indexed.get((case_id, repeat, treatment))
        if other is not None:
            by_case[case_id].append(float(other["dialogue_success"]) - float(indexed[(case_id, repeat, baseline)]["dialogue_success"]))
    differences = [statistics.mean(v) for _, v in sorted(by_case.items())]
    if not differences:
        return {"baseline": baseline, "treatment": treatment, "paired_dialogues": 0, "delta_percentage_points": None, "ci95_percentage_points": None}
    rng = random.Random(seed)
    boot = [statistics.mean(rng.choices(differences, k=len(differences))) * 100 for _ in range(samples)] if len(differences) >= 2 else []
    return {"baseline": baseline, "treatment": treatment, "paired_dialogues": len(differences),
            "paired_runs": sum(len(v) for v in by_case.values()), "delta_percentage_points": statistics.mean(differences) * 100,
            "ci95_percentage_points": [quantile(boot, .025), quantile(boot, .975)] if boot else None,
            "descriptive_only": len(differences) < 10 or len(set(differences)) == 1,
            "interpretation": "小样本或退化bootstrap区间不能证明等效或无差异" if len(differences) < 10 or len(set(differences)) == 1 else "固定语料上的描述性配对区间",
            "method": "paired bootstrap over dialogue IDs; average repeats within each ID; fixed finite corpus"}


def resume_eligibility(manifest, records):
    completed = {(r["case_id"], r["repeat"], r["arm"]) for r in records}
    expected = {(j["case_id"], j["repeat"], j["arm"]) for j in manifest["jobs"]}
    reasons = []
    if manifest["split"] != "holdout":
        reasons.append("当前为开发集，不能宣称独立留出效果")
    holdout_size = manifest["dataset_validation"]["splits"]["holdout"]
    if len(manifest["case_ids"]) != holdout_size:
        reasons.append(f"未覆盖完整{holdout_size}组留出对话")
    if manifest["repeats"] < 3:
        reasons.append("本项目预注册要求至少3次重复；这不是通用统计充分性保证")
    if expected != completed:
        reasons.append("运行矩阵未完成，不能只挑成功样例")
    if not manifest["review"]["all_approved"]:
        reasons.append("所选样本尚未全部完成运行前人工标签审核")
    required = {"direct", "agent"} if manifest.get("schema_version", 1) >= 2 else {"direct", "agent_no_task", "agent_full"}
    if set(manifest["arms"]) != required:
        reasons.append("对照方案未齐全")
    if manifest["mode"] != "live":
        reasons.append("未运行真实模型")
    if not manifest.get("implementation_archive"):
        reasons.append("首次试跑只有源码摘要，未保存推理时源码快照")
    return {"eligible_for_heldout_effect_claim": not reasons, "reasons": reasons,
            "engineering_claim": f"构建覆盖{manifest['dataset_validation']['dialogues']}组、{manifest['dataset_validation']['turns']}轮候选对话的金融Agent评测框架，支持{len(manifest['arms'])}种方案对照、数值与证据核验、检索对照及token/延迟追踪。"}


def write_report(output, manifest, records, budget, retrieval=None, stopped_reason=None):
    output = Path(output)
    metrics = aggregate(records)
    pairs = ([paired_comparison(records, "direct", "agent", manifest["seed"])]
             if manifest.get("schema_version", 1) >= 2 else
             [paired_comparison(records, "direct", "agent_full", manifest["seed"]),
              paired_comparison(records, "agent_no_task", "agent_full", manifest["seed"])])
    eligibility = resume_eligibility(manifest, records)
    eligibility["positive_improvement_supported"] = [p["baseline"] for p in pairs if
        eligibility["eligible_for_heldout_effect_claim"] and not p.get("descriptive_only", True)
        and p.get("ci95_percentage_points") and p["ci95_percentage_points"][0] > 0]
    report = {"schema_version": 1, "experiment_id": manifest["experiment_id"], "manifest_hash": manifest["manifest_hash"],
              "analysis_implementation_digest": package_digest(),
              "implementation_digest": manifest["implementation_digest"], "mode": manifest["mode"], "split": manifest["split"],
              "synthetic_sources": True, "review": manifest["review"], "planned_jobs": len(manifest["jobs"]),
              "completed_jobs": len(records), "stopped_reason": stopped_reason, "metrics": metrics, "paired_comparisons": pairs,
              "budget": budget, "retrieval": retrieval, "resume": eligibility,
              "by_category": {category: aggregate([r for r in records if r.get("category", "unspecified") == category])
                              for category in sorted({r.get("category", "unspecified") for r in records})},
              "limitations": ["数据为开发者编写的合成财报和候选标签，不能推断真实金融数据表现。",
                  "数值和引用评分覆盖结构化claims，并检查text内带金额/百分比单位的数值；无单位数字、语义归属仍需人工审阅。",
                  "置信区间以固定语料的对话ID重采样，不能证明对所有用户或公司的泛化。",
                  "直接模型内联读取相同来源包，Agent通过工具检索；新版不再进行任务表示消融，旧报告按其冻结协议解释。",
                  "token来自服务商usage，缺失usage会明确记录，不推算实际账单。"]}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if metrics:
        with (output / "metrics.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, ["arm", *next(iter(metrics.values())).keys()])
            writer.writeheader()
            writer.writerows({"arm": arm, **values} for arm, values in metrics.items())
    fmt = lambda value: "N/A" if value is None else f"{value * 100:.1f}%"
    lines = ["# 金融Agent对照实验", "", f"模式：{manifest['mode']}；集合：{manifest['split']}；完成 {len(records)}/{len(manifest['jobs'])} 个对话运行。", "",
        "全部金融资料为冻结合成原文。独立研究新增启动次数：0。", "",
        "| 方案 | 完成对话 | 对话成功率 | 字段准确率 | 数值召回 | 引用支持率 | 无依据结构化数字率 | P50/P95秒 | 总token |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for arm, m in metrics.items():
        latency = "N/A" if m["latency_p50_seconds"] is None else f"{m['latency_p50_seconds']:.2f}/{m['latency_p95_seconds']:.2f}"
        lines.append(f"| {arm} | {m['dialogue_runs']} | {fmt(m['dialogue_success_rate'])} | {fmt(m['slot_accuracy'])} | {fmt(m['numeric_recall'])} | {fmt(m['citation_support_rate'])} | {fmt(m['unsupported_numeric_claim_rate'])} | {latency} | {m['prompt_tokens']+m['completion_tokens']} |")
    lines.extend(["", "对话成功要求：每一轮动作、继承字段、所需数值、原文引用和运行状态均通过。格式失败、运行失败和缺失运行分别记录。",
                  "正文出现但未列入claims的金额单列为声明完整性缺陷；若可在已见原文找到，不自动算成编造。正文语义归属仍需人工审阅。", ""])
    for arm, m in metrics.items():
        lines.append(f"- {arm}：JSON解析率 {fmt(m['json_parse_rate'])}；输出合同完整率 {fmt(m['output_contract_rate'])}；正文未声明金额 {m['undeclared_text_amounts']}；正文无可见依据金额 {m['unsupported_text_amounts']}。")
    for pair in pairs:
        lines.append(f"- {pair['treatment']} − {pair['baseline']}：{pair['delta_percentage_points']} 个百分点；配对对话 {pair['paired_dialogues']}；95%区间 {pair['ci95_percentage_points']}。")
        if pair.get("interpretation"):
            lines.append("  " + pair["interpretation"] + "。")
    lines.extend(["", "效果型简历表述资格：" + ("满足预注册门槛，仍须如实标明数据范围。" if not eligibility["reasons"] else "未满足。"), *["- " + reason for reason in eligibility["reasons"]], "", eligibility["engineering_claim"], "", *report["limitations"]])
    if stopped_reason:
        lines.extend(["", "提前停止：" + stopped_reason])
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "RESUME.md").write_text(eligibility["engineering_claim"] + "\n\n" +
        ("仅可按report.json的实测范围填写效果；不能把百分点写成相对提升。" if not eligibility["reasons"] else "目前不生成独立留出准确率或提升百分比的简历声明。\n" + "\n".join(eligibility["reasons"])) + "\n", encoding="utf-8")
    (output / "prediction_review_template.json").write_text(json.dumps({"manifest_hash": manifest["manifest_hash"], "records": [
        {"case_id": r["case_id"], "repeat": r["repeat"], "arm": r["arm"], "turn": t["turn"],
         "answer": t["answer"], "automatic_task_success": t["grade"]["task_success"],
         "status": "pending", "reviewer": None, "notes": "核对正文语义归属、未带单位的数字和遗漏要求；不改变原始标签。"}
        for r in records for t in r["turns"]]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
