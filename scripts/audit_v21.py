"""Build an UNLABELLED human audit queue from actual validation artifacts."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from finagent.data.evidence import write_json
from finagent.research.contracts import digest


def audit(paths):
    runs, facts, responses, decisions = [], {}, [], []
    for path in paths:
        validation = json.loads(Path(path).read_text(encoding="utf-8"))
        for state in validation.get("results", []):
            if not state.get("report"):
                continue
            report = json.loads(Path(state["report"]).with_suffix(".json").read_text(encoding="utf-8"))
            runs.append({"research_id": state["id"], "symbol": state["scope"]["symbol"], "report": state["report"],
                "process_status": report["process_status"], "content_status": report["content_status"], "decision_status": report["decision_status"],
                "usage": state["budget"], "errors": report["errors"]})
            for a in report["artifacts"]:
                identity = {"research_id": state["id"], "artifact_id": a["artifact_id"], "role": a["role"]}
                for c in a.get("claims", []):
                    key = digest({k: c.get(k) for k in ("kind", "text", "evidence_refs", "calculation_refs")})
                    if key not in facts:
                        facts[key] = {"audit_id": key, "claim": c, "appearances": [], "human_support": None, "critical_error": None, "note": ""}
                    facts[key]["appearances"].append(identity)
                for response in a.get("responses", []):
                    responses.append({**identity, "response": response, "human_on_target": None, "note": ""})
                if a["role"] in ("research_manager", "portfolio_manager"):
                    decisions.append({**identity, "decision": a, "human_consistent": None, "critical_error": None, "note": ""})
    return {"schema_version": "2.1", "runs": runs, "fact_annotations": list(facts.values()), "response_annotations": responses,
        "decision_annotations": decisions, "quality_status": "pending_human_review", "support_rate": None,
        "counts": {"distinct_claim_records": len(facts), "responses": len(responses), "decisions": len(decisions)},
        "note": "重复记录已按完整主张去重，语义重复仍需人工合并。未标注不算正确，不是已完成80/20/12抽查。"}


def score(labels):
    facts = labels["fact_annotations"]
    complete = [x for x in facts if x.get("human_support") in ("supported", "unsupported", "unable_to_judge") and isinstance(x.get("critical_error"), bool)]
    responses = [x for x in labels["response_annotations"] if isinstance(x.get("human_on_target"), bool)]
    decisions = [x for x in labels["decision_annotations"] if isinstance(x.get("human_consistent"), bool) and isinstance(x.get("critical_error"), bool)]
    support = sum(x["human_support"] == "supported" for x in complete) / len(complete) if complete else None
    on_target = sum(x["human_on_target"] for x in responses) / len(responses) if responses else None
    critical = sum(x["critical_error"] for x in complete + decisions)
    enough = len(complete) >= 80 and len(responses) >= 20 and len(decisions) >= 12
    return {"labelled_facts": len(complete), "labelled_responses": len(responses), "labelled_decisions": len(decisions),
        "support_rate": support, "response_on_target_rate": on_target, "critical_errors": critical,
        "passed": enough and support >= .95 and on_target >= .9 and critical == 0 and all(x["human_consistent"] for x in decisions)}


def main():
    parser = argparse.ArgumentParser(description="离线生成真实产物人工标注队列，或核算已有人工标注；不调用模型")
    parser.add_argument("--validation", nargs="+")
    parser.add_argument("--annotations")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.annotations:
        result = score(json.loads(Path(args.annotations).read_text(encoding="utf-8")))
    else:
        if not args.validation:
            parser.error("需要 --validation 或 --annotations")
        result = audit(args.validation)
    write_json(Path(args.output), result)
    print(json.dumps({k: v for k, v in result.items() if k not in ("runs", "fact_annotations", "response_annotations", "decision_annotations")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
