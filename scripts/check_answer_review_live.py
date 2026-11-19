#!/usr/bin/env python3
"""Live reviewer smoke with synthetic evidence only; not an accuracy benchmark."""
import argparse
from datetime import datetime
import json
from pathlib import Path

import httpx

from finagent.answer_review import QwenAnswerReviewer, build_evidence_packet
from finagent.config import load_settings
from finagent.data.contracts import Observation
from finagent.data.evidence import EvidenceStore
from finagent.errors import AgentError
from finagent.runtime import ExecutionContext, TZ
from finagent.security import SecurityGuard


CASES = [
    ("supported", "合成公司2024年经营现金流为120元，同比增长20%。增加主要由于销售回款增加，同时采购付款减少。", "pass"),
    ("wrong_number", "合成公司2024年经营现金流为999元。", "revise"),
    ("overstatement", "合成公司2024年现金流增长完全来自销售回款增加。", "revise"),
    ("missing_evidence", "合成公司2024年现金流增长是因为海外并购。", "revise"),
    ("concept", "经营现金流反映企业经营活动产生的现金流入和流出。", "pass"),
    ("required_retrieval", "按本次提供的合成资料，合成公司2024年经营现金流为120元。", "pass"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--endpoint")
    parser.add_argument("--cases", nargs="+", choices=[c[0] for c in CASES])
    parser.add_argument("--output", default=".finagent/checks/answer_review_live.json")
    args = parser.parse_args()
    settings = load_settings(args.config)
    config = settings.fact_check.review.model_copy(deep=True)
    if args.endpoint:
        config.backend = "ollama"
        config.endpoint = args.endpoint
        config = type(config).model_validate(config.model_dump())
    output = Path(args.output)
    store = EvidenceStore(output.parent / (output.stem + "_synthetic_data"))
    ctx = ExecutionContext()
    ctx.as_of = datetime(2026, 11, 5, 12, tzinfo=TZ)
    ctx.symbol = "600519.SH"
    ctx.scope_constraints["allowed_report_periods"] = ["2024-12-31", "2023-12-31"]
    state = {"user_updates": [{"text": "分析合成公司2024年经营现金流及变化原因"}]}
    for tool, data in [
        ("get_financial_statements", {"reports": [{"report_period": "2024-12-31", "currency": "CNY",
            "metrics": {"operating_cashflow": {"value": "120", "unit": "元"}}},
            {"report_period": "2023-12-31", "currency": "CNY",
            "metrics": {"operating_cashflow": {"value": "100", "unit": "元"}}}]}),
        ("execute_program", {"kind": "financial_calculation", "purpose": "growth", "value": "20", "unit": "%",
            "period_end": "2024-12-31", "comparison_period_end": "2023-12-31",
            "formula": "(120-100)/100*100", "operands": ["120", "100"]}),
        ("get_stock_news", {"articles": [{"title": "合成现金流说明", "snippet":
            "合成公司2024年经营现金流增加，主要由于销售回款增加，同时采购付款减少。",
            "published_at": "2025-04-01T12:00:00+08:00"}]}),
    ]:
        raw = store.save_raw(json.dumps(data, ensure_ascii=False).encode(), {"synthetic": True})
        result = store.save_result(Observation(status="ok", source="synthetic_answer_review_smoke",
            symbol=ctx.symbol, snapshot_id=raw, currency="CNY", data=data))
        ctx.result_tools[result.result_id] = tool
        ctx.evidence_ids.update({raw, result.result_id})
    packet = build_evidence_packet(ctx, store, state, config)
    responses = []
    def record(request):
        # Only this script's synthetic cases; retain invalid outputs for diagnosis.
        with httpx.HTTPTransport(proxy=settings.model.proxy_url if config.backend == "main_model" else None) as transport:
            response = transport.handle_request(request)
            content = response.read()
            responses.append(json.loads(content))
            # read() has already decoded Content-Encoding; do not attach the
            # compressed response headers to the decoded body a second time.
            return httpx.Response(response.status_code, json=json.loads(content))
    model = QwenAnswerReviewer(config, model_settings=settings.model, transport=httpx.MockTransport(record))
    guard = SecurityGuard(settings)
    cases = [c for c in CASES if not args.cases or c[0] in args.cases]
    results = []
    for name, answer, expected in cases:
        item = {"name": name, "answer": answer, "expected": expected}
        responses.clear()
        try:
            current_packet = {**packet, "user_request": "解释经营现金流的含义"} if name == "concept" else packet
            if name == "concept":
                current_packet["request_context"] = {"recent_user_requests": [{"text": "解释经营现金流的含义"}],
                                                     "user_constraints": []}
            if name == "required_retrieval":
                import copy
                read_ctx = copy.copy(ctx)
                read_ctx.result_tools, read_ctx.evidence_ids = {}, set()
                body = {"text": "".join(f"附录章节{i:04d}：记录合成报告的目录结构、编制口径与资料说明。\n" for i in range(800))
                        + "数据表：合成公司2024年经营现金流为120元。"}
                raw = store.save_raw(json.dumps(body, ensure_ascii=False).encode(), {"synthetic": True})
                evidence = store.save_result(Observation(status="ok", source="synthetic_retrieval",
                    symbol=ctx.symbol, snapshot_id=raw, data=body))
                read_ctx.result_tools[evidence.result_id] = "get_stock_news"
                read_ctx.evidence_ids.update({raw, evidence.result_id})
                current_packet = build_evidence_packet(read_ctx, store, {
                    "user_updates": [{"text": "依据本次提供的合成资料，核对合成公司2024年经营现金流是否为120元"}]}, config)
            audit = {}
            result = model.review(answer, current_packet, guard=guard, audit=audit)
            item.update(review=result, passed=result["verdict"] == expected)
            item["audit"] = audit
            if name == "required_retrieval":
                item["retrieval_observed"] = any(e.get("kind") == "tool_result" and e.get("payload", {}).get("name")
                    in {"read_review_evidence", "search_review_evidence"} for e in audit.get("events", []))
                item["passed"] = item["passed"] and item["retrieval_observed"]
        except AgentError as exc:
            item.update(error=exc.code, validation_detail=str(exc.__cause__) if exc.__cause__ else None,
                        audit=audit, passed=False)
        item["raw_review"] = responses[-1] if responses else None
        results.append(item)
        print(json.dumps({k: item[k] for k in ("name", "passed", "error") if k in item}, ensure_ascii=False), flush=True)
        if item.get("error") in {"ANSWER_REVIEW_UNAVAILABLE", "ANSWER_REVIEW_TIMEOUT"}:
            break
    report = {"kind": "live_reviewer_synthetic_smoke_not_accuracy_benchmark", "model": model.config.model,
              "backend": config.backend,
              "total": len(cases), "executed": len(results), "passed": sum(r["passed"] for r in results), "cases": results}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if len(results) == len(cases) and all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
