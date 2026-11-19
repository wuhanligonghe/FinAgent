"""Development matrix and local scale check; never a model/held-out score."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from finagent.config import Limits, load_settings
from finagent.data.evidence import EvidenceStore, write_json
from finagent.experiments.legacy_library import DocumentImport, KnowledgeBase
from finagent.market import resolve_market_day
from finagent.runtime import ExecutionContext, TZ, now
from finagent.session import Conversation
from datetime import date, datetime


def main():
    settings = load_settings()
    directory = settings.data_dir / "evaluation"
    directory.mkdir(parents=True, exist_ok=True)
    cases = json.loads(Path("doc/p0_scenarios.json").read_text(encoding="utf-8"))
    results = []
    with TemporaryDirectory(dir=directory) as temporary:
        store = EvidenceStore(Path(temporary))
        session = Conversation(store, "synthetic-matrix")
        for case in cases["chinese_multiturn"]:
            session.state = {"symbol": case["prior_symbol"], "scope": {"report_period": case["prior_period"]}, "preferences": {}}
            ctx = session.context(case["question"], Limits())
            actual = {"symbol": ctx.symbol, "report_period": ctx.scope.get("report_period"), "evidence_count": len(ctx.evidence_ids)}
            results.append({"id": case["id"], "synthetic": True, "category": "chinese_scope", "actual": actual, "expected": case["expected"], "passed": actual == case["expected"]})
        for case in cases["market_calendar"]:
            ctx = ExecutionContext(as_of=datetime.fromisoformat(case["as_of"]))
            scope = resolve_market_day(date.fromisoformat(case["requested"]), ctx)
            actual = {"trade_date": scope["trade_date"], "session_ended": scope["session_ended"]}
            results.append({"id": case["id"], "synthetic": True, "category": "market_calendar", "actual": actual, "expected": case["expected"], "passed": actual == case["expected"]})
        # This tests storage/search capacity using explicitly synthetic materials.
        # It does not expand the set of verified real company disclosures.
        started = perf_counter()
        original = Path(temporary) / "synthetic.txt"
        original.write_text("SYNTHETIC: 本规模测试材料不是公开公司财报。营业收入待核验。", encoding="utf-8")
        library = KnowledgeBase(store)
        entities = json.loads(Path("src/finagent/resources/securities.json").read_text(encoding="utf-8"))
        for i in range(100):
            company = entities[i % 10]
            library.ingest(original, DocumentImport(symbol=company["symbol"], title=f"合成规模资料{i}", kind="annual_report",
                source_url=f"https://example.test/synthetic/{i}", publisher="SYNTHETIC", published_at="2024-01-01", publication_trusted=True,
                version=f"synthetic-{i}", report_period="2023-12-31", synthetic=True))
        ctx = ExecutionContext(symbol=entities[0]["symbol"])
        retrieved = library.search("营业收入", ctx, limit=5)
        scale = {"synthetic": True, "entities": len(entities), "materials": len(list(library.documents())),
                 "top_k": len(retrieved.data["matches"]), "elapsed_seconds": round(perf_counter()-started, 3)}
        scale["passed"] = scale["entities"] >= 10 and scale["materials"] >= 100 and scale["top_k"] == 5
    report = {"mode": "synthetic_development_regression", "checked_at": now().isoformat(),
              "notice": "开发回归，不是冻结留出集、真实模型正确率或产品质量达标证明。市场异常的零分母/注入/滞后等另见test_p0。",
              "passed_cases": sum(r["passed"] for r in results), "total_cases": len(results), "cases": results, "scale": scale,
              "passed": all(r["passed"] for r in results) and scale["passed"]}
    write_json(settings.data_dir / "checks/p0_development_matrix.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
