"""Audit saved P0 evidence, without network or model calls."""
import argparse
import json
from pathlib import Path

from finagent.config import load_settings
from finagent.data.evidence import EvidenceStore, write_json
from finagent.experiments.legacy_library import KnowledgeBase
from finagent.provenance import package_digest
from finagent.runtime import now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True, help="最终源码的真实财务运行ID")
    args = parser.parse_args()
    import re
    if not re.fullmatch(r"[a-f0-9]{32}", args.run_id):
        raise ValueError("invalid run id")
    settings = load_settings()
    root = settings.data_dir
    store = EvidenceStore(root)
    def read(path):
        return json.loads((root / path).read_text(encoding="utf-8"))
    offline = read("checks/p0_offline_tests.json")
    matrix = read("checks/p0_development_matrix.json")
    package = read("checks/p0_package_smoke.json")
    latest = read("checks/p0_real_multiturn_latest.json")
    online = read("checks/p0_market_online.json")
    materials = read("checks/p0_public_materials.json")
    final = read(f"runs/{args.run_id}/result.json")
    digest = package_digest()
    key = settings.model.api_key.get_secret_value().encode()
    scanned, hits = 0, []
    if key:
        for path in (root / "runs").glob("*/*"):
            if path.is_file() and path.suffix in (".json", ".jsonl", ".md"):
                scanned += 1
                if key in path.read_bytes():
                    hits.append(str(path))  # never print the matched secret
    attempts = {}
    for path in (root / "checks").glob("p0_real_20*.json"):
        for item in json.loads(path.read_text(encoding="utf-8")).get("attempts", []):
            attempts[item["run_id"]] = item
    library = KnowledgeBase(store)
    checks = {
        "offline": offline["passed"] and offline["implementation_sha256"] == digest,
        "development_matrix": matrix["passed"] and matrix["total_cases"] == 70,
        "wheel_matches_source": package["passed"] and package["implementation_sha256"] == digest,
        "latest_multiturn": latest["passed"] and latest["passed_cases"] == 4,
        "final_source_model_run": final["status"] == "completed" and final["final_protocol_verified"] and final["implementation_sha256"] == digest,
        "public_reports": len(materials["samples"]) == 3 and all(s["status"] == "parsed" and s["facts"] == 6 and store.has_raw(s["snapshot_id"])
                              and library.load(s["document_id"])["synthetic"] is False for s in materials["samples"]),
        "benchmark_online": len(online["results"]) == 3 and online["results"][1]["symbol"] == "000300.SH" and store.has_raw(online["results"][1]["snapshot_id"]),
        "secrets_absent": bool(key) and not hits,
    }
    result = {"checked_at": now().isoformat(), "implementation_sha256": digest, "checks": checks,
              "functional_checks_passed": all(checks.values()), "offline_tests": offline["tests_run"],
              "development_scenarios": [matrix["passed_cases"], matrix["total_cases"]],
              "latest_real_multiturn": [latest["passed_cases"], latest["executed_cases"]],
              "historical_real_multiturn": [sum(a["passed"] for a in attempts.values()), len(attempts)],
              "final_run_id": args.run_id, "secret_scan": {"scanned_run_files": scanned, "matching_files": hits},
              "sft": "deferred_by_user", "product_acceptance": "not_fully_evaluated",
              "remaining_acceptance": ["冻结留出集与人工引用支持评分", "30次/类性能分位数", "SFT与B0/B1/B2对照及回滚实验"],
              "live_limitations": ["新浪证券及指数日线最终完整状态未知，完整复盘保留partial", "公司公告原文依赖已导入覆盖"],
              "cost": None}
    write_json(root / "checks/p0_acceptance_summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["functional_checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
