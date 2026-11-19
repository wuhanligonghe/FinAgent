"""Explicit SYNTHETIC fixtures for v2.1; no network or market validation."""
import json
from threading import Lock

from finagent.data.contracts import Observation
from finagent.data.evidence import EvidenceStore
from finagent.errors import AgentError
from finagent.research.workflow import ref

TEXT = "合成测试主张"


def synthetic_grants(evidence, database, budget, task, role, cutoff):
    facts = [{"fact_id": f"f{n}_{p}", "verification": "provider_field_mapping", "metric": m, "period_end": p}
             for n, m in enumerate(("revenue", "net_profit", "operating_cashflow", "total_assets", "total_liabilities"))
             for p in ("2023-12-31", "2024-12-31")]
    tools = {"technical": ["get_market_data", "get_benchmark_data", "calculate_closing_review"],
             "fundamentals": ["get_financial_statements", "execute_program"],
             "news": ["get_stock_news", "get_macro_news"],
             "sentiment": ["get_fund_flow", "get_market_attention", "get_market_data", "calculate_news_tone"],
             "policy": ["read_policy_document"]}[role]
    values = []
    for tool in tools:
        raw = evidence.save_raw(json.dumps({"role": role, "tool": tool, "value": TEXT}).encode(), {"source": "SYNTHETIC", "fetched_at": cutoff.isoformat()})
        value = evidence.save_result(Observation(status="ok", source="SYNTHETIC", symbol="600519.SH", snapshot_id=raw,
            data={"value": TEXT, "available_at": cutoff.isoformat(), "bars": [{"close": "12"}] * 60, "facts": facts, "as_of_status": "effective"}))
        database.grant(budget.run, budget.owner, task, value.result_id, tool)
        values.append((tool, value))
    return values


def expert_body(role, value, scope, status="complete"):
    return {"analyst": role, "scope_hash": scope.scope_hash, "content_status": status, "summary": TEXT, "report_markdown": TEXT,
        "claims": [{"claim_id": "c", "kind": "fact", "text": TEXT, "evidence_refs": [
            {"result_id": value.result_id, "snapshot_id": value.snapshot_id, "locator": "/data/value"}], "calculation_refs": []}],
        "missing": [] if status == "complete" else ["SYNTHETIC缺口"], "limitations": []}


def decision_body(role, task, artifacts, scope, bundle):
    claim_a = next(a for a in artifacts if a.get("claims"))
    cr = ref(claim_a, claim_a["claims"][0]["claim_id"])
    args = lambda: [{"argument_id": "a", "text": TEXT, "claim_refs": [cr], "horizon": scope.decision_horizon,
                     "assumptions": ["合成条件"], "invalidation_conditions": ["原始依据失效"]}]
    if role == "quality_gate":
        return {"summary": "SYNTHETIC收口", "resolutions": [{"issue_id": i["issue_id"], "state": "resolved", "reason": TEXT, "claim_refs": [cr]} for i in bundle["issues"]]}
    if role in ("bull", "bear") or role.startswith("risk_"):
        risk = role.startswith("risk_")
        previous = [a for a in artifacts if a.get("role", "").startswith("risk_")] if risk else [a for a in artifacts if a.get("role") in ("bull", "bear")]
        opponents = [a for a in previous if a["role"] != role]
        responses = []
        key = "risk_arguments" if risk else "arguments"
        if opponents:
            peer = opponents[-1]
            responses = [{"target": ref(peer, peer[key][0]["argument_id"]), "stance": "qualify", "reason": TEXT, "claim_refs": [cr]}]
        common = {"round": int(task.rsplit("_", 1)[-1]), "turn_index": len(previous) + 1, "responses": responses}
        if not risk:
            return {**common, "side": role, "arguments": args()}
        trader = next(a for a in artifacts if a["role"] == "trader")
        return {**common, "perspective": role, "proposal_ref": ref(trader), "assessment": "modify", "reason": TEXT, "risk_arguments": args()}
    plan = next((a for a in artifacts if a["role"] == "research_manager"), None)
    trader = next((a for a in artifacts if a["role"] == "trader"), None)
    decided = bundle["gate_status"] == "pass" and bundle["debate_complete"]
    if role == "trader":
        decided &= plan["decision_status"] == "decided"
    if role == "portfolio_manager":
        decided &= bundle["risk_complete"] and bundle["decision_eligible"] and plan["decision_status"] == "decided" and trader["decision_status"] == "decided"
    common = {"decision_status": "decided" if decided else "abstained", "reason": TEXT, "horizon": scope.decision_horizon,
        "conditions": ["合成条件成立"], "invalidation_conditions": ["原始依据失效"], "review_triggers": ["新的公开披露"], "claim_refs": [cr]}
    debates = [a for a in artifacts if a["role"] in ("bull", "bear")]
    rulings = [{"target": ref(a, c["argument_id"]), "verdict": "conditional", "reason": TEXT, "claim_refs": [cr]} for a in debates for c in a.get("arguments", [])]
    if role == "research_manager":
        return {**common, "rating": "Hold" if decided else None, "issue_rulings": rulings}
    if role == "trader":
        return {**common, "action": "Hold" if decided else None, "plan_ref": ref(plan), "preconditions": ["合成前提"], "action_triggers": ["条件满足"]}
    risks = [a for a in artifacts if a["role"].startswith("risk_")]
    risk_rulings = [{"target": ref(a, c["argument_id"]), "verdict": "conditional", "reason": TEXT, "claim_refs": [cr]}
                   for a in risks for c in a.get("risk_arguments", [])]
    return {**common, "rating": "Hold" if decided else None, "action": "Hold" if decided else None, "plan_ref": ref(plan),
        "proposal_ref": ref(trader), "risk_refs": [ref(a) for a in risks], "accepted_rejected_views": rulings + risk_rulings or [
            {"target": cr, "verdict": "unresolved", "reason": TEXT, "claim_refs": [cr]}]}


class SyntheticRunner:
    def __init__(self, tracker, settings, store, budget, scope):
        self.tracker, self.store, self.budget, self.scope = tracker, store, budget, scope

    def run(self, role, task, attempt, *, artifacts=None, bundle=None, **kwargs):
        with self.tracker["lock"]:
            self.tracker["calls"].append(task)
        if task in self.tracker.get("fail", set()) or role in self.tracker.get("fail", set()):
            raise AgentError("SYNTHETIC_FAILURE", "离线故障注入")
        if role in self.scope.analysts:
            evidence = EvidenceStore(self.store.root)
            values = synthetic_grants(evidence, self.store, self.budget, task, role, self.scope.as_of)
            return expert_body(role, values[0][1], self.scope, "partial" if role in self.tracker.get("partial", set()) else "complete")
        if role == "review":
            return {"summary": "SYNTHETIC审阅", "issues": self.tracker.get("issues", [])}
        return decision_body(role, task, artifacts, self.scope, bundle)


def tracker(**kwargs):
    return {"calls": [], "lock": Lock(), **kwargs}


def native_factory(settings, partial_roles=()):
    """Real AgentLoop with scripted local models and explicitly injected data."""
    import httpx
    from finagent.research.decision_runner import DecisionRunner
    from finagent.research.decision_contracts import DecisionScope
    from finagent.data.service import DataService
    from finagent.data.http import SourceHTTP
    from tests.support import call_reply
    contexts = {}
    class Native(DecisionRunner):
        def context(self, task, attempt, reserved=False, followup=False, grants=None):
            ctx = super().context(task, attempt, reserved, followup, grants)
            if task in self.scope.analysts:
                values = synthetic_grants(EvidenceStore(settings.data_dir), self.store, self.budget, task, task, self.scope.as_of)
                for tool, v in values:
                    ctx.result_tools[v.result_id] = tool
                    ctx.evidence_ids.update([v.result_id, v.snapshot_id])
                contexts[task] = (self.scope, values[0][1])
            return ctx
    class Model:
        def __init__(self, role):
            self.role = role
        def complete(self, messages, tools, timeout):
            # Runtime metadata is intentionally a user-role data message too.
            # Select the actual task envelope, never infer trust from ordering.
            goal = next(value for m in messages if m["role"] == "user"
                        for value in [json.loads(m["content"])] if "task" in value and "scope" in value)
            if self.role in contexts:
                scope, value = contexts[self.role]
                body = expert_body(self.role, value, scope, "partial" if self.role in partial_roles else "complete")
                body = {k: v for k, v in body.items() if k not in ("analyst", "scope_hash")}
            elif self.role == "review":
                body = {"summary": "SYNTHETIC审阅", "issues": []}
            else:
                from finagent.research.selection import SelectedScope
                cls = SelectedScope if goal["scope"].get("workflow_version") == "2.2" else DecisionScope
                body = decision_body(self.role, goal["task"], goal["artifacts"], cls.model_validate(goal["scope"]), goal["bundle"])
            return call_reply(("submit", "submit_analysis", body))
    def service():
        def denied(request):
            raise AssertionError("Synthetic demo must not access a network")
        return DataService(settings, SourceHTTP(EvidenceStore(settings.data_dir), retries=0, client=httpx.Client(transport=httpx.MockTransport(denied))))
    return lambda *args: Native(*args, model_factory=Model, service_factory=service)
