from __future__ import annotations

import json
from ..agent import AgentLoop, RunRequest
from ..config import StrictModel
from ..data.contracts import Observation
from ..errors import AgentError
from ..tools.registry import Tool
from ..analysts.decision import prompt
from .budget import BudgetedModel
from .profile import AgentProfile
from .runner import AnalystRunner
from .decision_contracts import SCHEMAS
from .workflow import access, ref, items, validate_output


class ReadHandoff(StrictModel):
    artifact_id: str


class DecisionRunner(AnalystRunner):
    def context(self, task, attempt, reserved=False, followup=False, grants=None):
        ctx = super().context(task, attempt, reserved, followup, grants)
        if task == "review":
            ctx.limits.model_calls = 4
        return ctx

    def model_settings(self, role):
        cfg = self.budget.config
        key = role if role != "quality_gate" or role in cfg.role_models else "review"
        name = cfg.role_models.get(key)
        if name:
            return cfg.model_profiles[name]
        value = super().model_settings(role)
        if role in ("review", "quality_gate", "research_manager", "portfolio_manager"):
            return value.model_copy(update={"max_output_tokens": cfg.manager_output_tokens})
        return value

    def run(self, role, task, attempt, *, question="", artifacts=None, bundle=None):
        if role not in SCHEMAS:
            return super().run(role, task, attempt, question=question, artifacts=artifacts)
        artifacts = artifacts or []
        allowed_results = set()
        for a in artifacts:
            for c in a.get("claims", []):
                allowed_results.update(r["result_id"] for r in c.get("evidence_refs", []))
                allowed_results.update(c.get("calculation_refs", []))
        # Calculation input lineage is explicitly granted as well.
        from ..data.evidence import EvidenceStore
        evidence = EvidenceStore(self.settings.data_dir)
        pending = list(allowed_results)
        while pending:
            result = evidence.load_result(pending.pop())
            for key in result.data.get("input_result_ids", []):
                if key not in allowed_results:
                    allowed_results.add(key)
                    pending.append(key)
        grants = [g for g in self.store.grants(self.budget.run) if g["result_id"] in allowed_results]
        ctx = self.context(task, attempt, access(role, self.scope), grants=grants)
        ctx.limits.model_calls = 4
        ctx.limits.context_chars = 250000
        ctx.submit_reserve = 1
        service = self.service_factory()
        model = None
        try:
            from ..tools.perception import build_registry
            registry = build_registry(service).view(["read_evidence"])
            by_id = {a["artifact_id"]: a for a in artifacts}

            def read(args, context):
                if args.artifact_id not in by_id:
                    raise AgentError("ARTIFACT_DENIED", "交接产物未获授权")
                return Observation(status="ok", source="host_handoff", data=by_id[args.artifact_id])

            def submit(args, context):
                context.submitted_analysis = validate_output(role, args.model_dump(mode="json"), artifacts, self.scope, task, bundle)
                return Observation(status="ok", source="host_submission", data={"accepted": True})

            registry.register(Tool("read_handoff", "按已授予ID读取固定交接产物，不接受路径", ReadHandoff, read, False))
            registry.register(Tool("submit_analysis", "按本角色合同提交；最多一次修正", SCHEMAS[role], submit, False))
            hints = [{"role": a.get("role"), "artifact_id": a["artifact_id"], "artifact_hash": a["artifact_hash"], "item_ids": list(items(a))} for a in artifacts]
            # These are bounded views of immutable originals; identity/hash
            # refer to the full accepted artifact read by read_handoff.
            hidden = {"input_artifact_refs", "evidence_tools", "host_checks", "created_at", "report_markdown", "role_prompt_version"}
            projected = [{k: v for k, v in a.items() if k not in hidden} for a in artifacts]
            fixed = {}
            if role in ("bull", "bear") or role.startswith("risk_"):
                risk = role.startswith("risk_")
                previous = [a for a in artifacts if a.get("role", "").startswith("risk_")] if risk else [a for a in artifacts if a.get("role") in ("bull", "bear")]
                fixed = {"round": int(task.rsplit("_", 1)[1]), "turn_index": len(previous) + 1, "perspective" if risk else "side": role}
            if role in ("trader", "portfolio_manager"):
                fixed["plan_ref"] = ref(next(a for a in artifacts if a["role"] == "research_manager"))
            if role.startswith("risk_") or role == "portfolio_manager":
                fixed["proposal_ref"] = ref(next(a for a in artifacts if a["role"] == "trader"))
            goal = {"scope": self.scope.model_dump(mode="json"), "task": task, "bundle": bundle,
                    "artifacts": projected, "ref_hints": hints, "projection_note": "视图省略重复元数据，哈希指向原始完整产物；可按ID回读",
                    "submission_contract": {"name": SCHEMAS[role].__name__, "required_fields": SCHEMAS[role].model_json_schema().get("required", []),
                                            "field_names": list(SCHEMAS[role].model_fields), "required_values": fixed}}
            model = self.model_factory(role)
            loop = AgentLoop(BudgetedModel(model, self.budget, task, access(role, self.scope)), registry, service.store,
                             profile=AgentProfile(role, prompt(role)))
            result = loop.run(RunRequest(json.dumps(goal, ensure_ascii=False), require_data=False), ctx)
            ctx.check()
            if ctx.submitted_analysis is None:
                result.raise_for_error()
                raise AgentError("SUBMISSION_MISSING", "决策角色未提交合法交接：" + result.reason)
            return ctx.submitted_analysis
        finally:
            if model is not None and hasattr(model, "close"):
                model.close()
            service.close()
