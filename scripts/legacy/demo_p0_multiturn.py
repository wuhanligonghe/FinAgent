"""Real configured-model verification on the manually reviewed public PDFs.

This consumes model usage. It is a development demonstration, not a held-out
model accuracy score. Every attempt and its report are retained.
"""
import json
from datetime import datetime

from finagent.agent import AgentLoop, RunRequest
from finagent.config import load_settings
from finagent.data.evidence import write_json
from finagent.data.service import DataService
from finagent.experiments.legacy_library import KnowledgeBase, numeric
from finagent.models import CompatibleChatModel
from finagent.provenance import package_digest
from finagent.runtime import now
from finagent.session import Conversation
from finagent.tools.perception import build_registry


def main():
    settings = load_settings()
    service = DataService(settings)
    model = CompatibleChatModel(settings.model)
    conversation = Conversation(service.store, "p0_real_" + now().strftime("%Y%m%d_%H%M%S"))
    loop = AgentLoop(model, build_registry(service), service.store)
    cutoff = datetime.fromisoformat("2025-05-01T23:59:59+08:00")
    cases = [
        ("查询贵州茅台2024年报的营业收入和归母净利润，从完整原文取证并给出facts，不需要计算。", "600519.SH", "2024-12-31", {"revenue": "170899152276.34", "attributable_net_profit": "86228146421.62"}),
        ("那上一年呢？沿用刚才两个指标和口径，给出原文事实。", "600519.SH", "2023-12-31", {"revenue": "147693604994.14", "attributable_net_profit": "74734071550.75"}),
        ("换成宁德时代，仍看2023年报的营业收入及归母净利润，注意单位。", "300750.SZ", "2023-12-31", {"revenue": "400917044900", "attributable_net_profit": "44121248300"}),
        ("那归母净利润率呢？请继续使用当前公司和期间，并用受限程序执行。", "300750.SZ", "2023-12-31", {}),
    ]
    attempts = []
    implementation = package_digest()
    try:
        for index, (question, company, period, expected) in enumerate(cases, 1):
            ctx = conversation.context(question, settings.limits, as_of=cutoff)
            result = loop.run(RunRequest(question), ctx)
            conversation.remember(result)
            body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
            facts = {}
            for claim in (body.get("final_answer") or {}).get("facts", []):
                if str(claim.get("reference", "")).startswith("doc_"):
                    fact = KnowledgeBase(service.store).fact(claim["reference"], ctx)
                    if fact["period_end"] == period:
                        facts[fact["metric"]] = fact["normalized_value"]
            checks = {"completed": result.status == "completed", "final_protocol_verified": body["final_protocol_verified"],
                      "company": body["symbol"] == company, "period": body["scope"].get("report_period") == period,
                      "gold_original_cells": all(key in facts and numeric(facts[key]) == numeric(value) for key, value in expected.items())}
            if index == 4:
                calculations = [service.store.load_result(key).data for key in result.calculation_ids]
                checks["evidence_bound_margin"] = any(c.get("purpose") == "net_margin" and [f["metric"] for f in c["operands"]] == ["attributable_net_profit", "revenue"]
                                                       and all(f["period_end"] == period for f in c["operands"]) for c in calculations)
            item = {"case": index, "question": question, "mode": "real_model_local_public_pdf", "run_id": result.run_id,
                    "status": result.status, "run_dir": str(result.run_dir), "checks": checks, "passed": all(checks.values()),
                    "model_calls": result.model_calls, "tool_calls": result.tool_calls, "elapsed_seconds": result.elapsed_seconds,
                    "token_usage": body["token_usage"], "cost": body["cost"]}
            attempts.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            report = {"checked_at": now().isoformat(), "mode": "real_model_development_demo", "model": settings.model.name,
                      "implementation_sha256": implementation, "passed_cases": sum(a["passed"] for a in attempts),
                      "executed_cases": len(attempts), "planned_cases": len(cases), "attempts": attempts,
                      "notice": "人工核对的3份公开PDF，4轮开发演示；不是冻结留出集或SFT结果。费用未知保留null。"}
            report["passed"] = len(attempts) == len(cases) and all(a["passed"] for a in attempts)
            write_json(settings.data_dir / "checks" / (conversation.path.stem + ".json"), report)
            write_json(settings.data_dir / "checks/p0_real_multiturn_latest.json", report)
    finally:
        model.close()
        service.close()
    return 0 if len(attempts) == len(cases) and all(a["passed"] for a in attempts) else 2


if __name__ == "__main__":
    raise SystemExit(main())
