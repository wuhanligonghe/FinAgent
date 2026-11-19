"""A failed review is never equivalent to an empty successful review."""
from .contracts import digest


def issue_list(review):
    return [{**i, "issue_id": f"issue_{n}", "state": "open"} for n, i in enumerate((review or {}).get("issues", []))]


def research_bundle(scope, artifacts, review, closing=None, *, closing_required=False, excluded=None):
    issues = issue_list(review)
    resolutions = {r["issue_id"]: r for r in (closing or {}).get("resolutions", [])}
    for i in issues:
        if i["issue_id"] in resolutions:
            i.update(resolutions[i["issue_id"]])
    coverage = {r: "unavailable" for r in scope.analysts}
    for a in artifacts:
        if a.get("analyst") in coverage and a.get("claims"):
            # Supplemental followups never conceal missing first-wave contracts.
            if a["task_id"] == a["analyst"]:
                coverage[a["analyst"]] = a["content_status"]
    has_claims = any(a.get("claims") for a in artifacts)
    review_done = review is not None and (not closing_required or closing is not None)
    open_issues = [i for i in issues if i["state"] == "open"]
    blocked = not has_claims or any(i["severity"] == "critical" for i in open_issues)
    complete = all(v == "complete" for v in coverage.values())
    gate = "blocked" if blocked else "pass" if complete and review_done and not excluded and not any(i["severity"] == "major" for i in open_issues) else "limited"
    eligible = gate == "pass" and {"technical", "fundamentals"} <= set(scope.analysts) and all(
        any(a.get("analyst") == r and a.get("claims") and a.get("content_status") == "complete" for a in artifacts) for r in ("technical", "fundamentals"))
    return {"gate_status": gate, "coverage": coverage, "issues": issues, "excluded": excluded or [],
            "review_status": "unavailable" if not review_done else "issues_found" if open_issues else "passed",
            "decision_eligible": eligible, "debate_complete": False, "risk_complete": False,
            "decision_constraints": ["仅研究，无订单或执行价格参数", "交易执行可行性未确认"],
            "source_snapshots": sorted({r["snapshot_id"] for a in artifacts for c in a.get("claims", []) for r in c.get("evidence_refs", [])}),
            "claim_index": [{"task_id": a["task_id"], "claim_id": c["claim_id"], "text": c["text"]} for a in artifacts for c in a.get("claims", [])],
            "review_input_hash": digest(review) if review else None}
