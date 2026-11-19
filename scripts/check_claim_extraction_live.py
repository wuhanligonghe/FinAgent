"""Live Qwen extraction against synthetic evidence; not an accuracy benchmark."""
import argparse
from datetime import datetime
import json
from pathlib import Path

from finagent.claim_extraction import QwenClaimExtractor, claim_number
from finagent.config import load_settings
from finagent.data.contracts import Observation
from finagent.data.evidence import EvidenceStore
from finagent.errors import AgentError
from finagent.fact_check import inspect_answer
from finagent.runtime import ExecutionContext, TZ
from finagent.security import SecurityGuard


CASES = [
    ("chinese_price", "贵州茅台股价十元。", [("price", "10", "fact")], False),
    ("wrong_price", "贵州茅台股价九十九元。", [("price", "99", "fact")], True),
    ("financial_fields", "贵州茅台2024年营业收入十亿元，归母净利润两亿元。",
     [("revenue", "10", "fact"), ("attributable_net_profit", "2", "fact")], False),
    ("hypothesis", "假设贵州茅台股价一百元，仅作示例。", [("price", "100", "hypothesis")], False),
    ("growth", "贵州茅台营业收入同比增长百分之十五。", [("calculation:growth", "15", "fact")], False),
    ("multi_company", "贵州茅台股价十元，五粮液股价二十元。", [("price", "10", "fact"), ("price", "20", "fact")], False),
    ("table", "|公司|2024年营业收入（亿元）|\n|---|---|\n|贵州茅台|十|", [("revenue", "10", "fact")], False),
    ("forecast", "预计贵州茅台明年营业收入十二亿元。", [("revenue", "12", "prediction")], False),
    ("attribution", "按你提供的信息，贵州茅台股价一百元。", [("price", "100", "user_statement")], False),
    ("code", '#include <stdio.h>\nint main(void) { puts("Hello World"); return 0; }', [], False),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--endpoint")
    parser.add_argument("--cases", nargs="+", choices=[case[0] for case in CASES])
    parser.add_argument("--output", default=".finagent/checks/claim_extraction_live.json")
    args = parser.parse_args()
    settings = load_settings(args.config)
    config = settings.fact_check.extraction.model_copy(deep=True)
    if args.endpoint:
        config.endpoint = args.endpoint
        config = type(config).model_validate(config.model_dump())
    output = Path(args.output)
    store = EvidenceStore(output.parent / (output.stem + "_synthetic_data"))
    ctx = ExecutionContext()
    ctx.as_of = datetime.now(TZ)
    ctx.symbol = "600519.SH"
    for tool, data in [
        ("get_quote", {"name": "贵州茅台", "price": 10}),
        ("get_financial_statements", {"reports": [{"report_period": "2024-12-31", "metrics": {
            "revenue": {"value": 1000000000}, "attributable_net_profit": {"value": 200000000}}}]}),
        ("execute_program", {"kind": "financial_calculation", "purpose": "growth", "value": "15", "unit": "%"}),
    ]:
        raw = store.save_raw(json.dumps(data).encode(), {"synthetic": True})
        observation = store.save_result(Observation(status="ok", source="synthetic_claim_extraction_smoke",
            symbol=ctx.symbol, snapshot_id=raw, currency="CNY", data=data))
        ctx.result_tools[observation.result_id] = tool
        ctx.evidence_ids.update({raw, observation.result_id})
    raw = store.save_raw(b"synthetic second company", {"synthetic": True})
    observation = store.save_result(Observation(status="ok", source="synthetic_claim_extraction_smoke",
        symbol="000858.SZ", snapshot_id=raw, currency="CNY", data={"name": "五粮液", "price": 20}))
    ctx.result_tools[observation.result_id] = "get_quote"
    ctx.evidence_ids.update({raw, observation.result_id})
    extractor = QwenClaimExtractor(config)
    guard = SecurityGuard(settings)
    results = []
    cases = [case for case in CASES if not args.cases or case[0] in args.cases]
    for name, answer, expected, conflict in cases:
        item = {"name": name, "answer": answer, "expected": expected, "expected_conflict": conflict}
        try:
            extracted = extractor.extract(answer, {"entities": [{"name": "贵州茅台", "symbol": ctx.symbol}]}, guard=guard)
            found = []
            for claim in extracted["claims"]:
                if claim["value"] is not None:
                    try:
                        found.append((claim["metric"], claim_number(claim["value"]), claim["kind"]))
                    except ValueError:
                        pass
            recognized = all((field, claim_number(value), kind) in found for field, value, kind in expected)
            if not expected:
                recognized = not extracted["claims"]
            state = {"user_updates": [{"text": "贵州茅台股价一百元"}]} if name == "attribution" else {}
            report = inspect_answer(answer, ctx, store, state, extracted["claims"])
            item.update(extraction=extracted, fact_check=report,
                        passed=recognized and bool(report["issues"]) == conflict)
        except AgentError as exc:
            item.update(passed=False, error=exc.code)
        results.append(item)
        print(json.dumps({k: item[k] for k in ("name", "passed", "error") if k in item}, ensure_ascii=False), flush=True)
        if item.get("error") in {"CLAIM_EXTRACTION_UNAVAILABLE", "CLAIM_EXTRACTION_TIMEOUT"}:
            break
    report = {"kind": "live_smoke_synthetic_evidence_not_accuracy_benchmark", "model": config.model,
              "executed": len(results), "total": len(cases), "passed": sum(r["passed"] for r in results), "cases": results}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if len(results) == len(cases) and all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
