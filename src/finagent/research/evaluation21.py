"""v2.1 frozen four-way comparisons. Preparing inputs never calls a model."""
import json
import re
from pathlib import Path
from uuid import uuid4

import httpx

from ..config import ResearchSettings
from ..data.evidence import EvidenceStore, write_json
from ..data.http import SourceHTTP
from ..data.service import DataService
from ..errors import AgentError
from .contracts import ResearchRequest, digest
from .decision_contracts import DecisionRequest
from .decision_runner import DecisionRunner
from .coordinator import ResearchCoordinator
from .evaluation import FrozenPack, freeze_packs, run_variant, summarize_runs
from .token_envelope import TokenEnvelope, TokenEnvelopeModel

VARIANTS21 = ("single", "experts_review", "decision_one_round", "decision_two_rounds")


def freeze_matrix(settings, sources, destination):
    from .store import ResearchStore
    destination = Path(destination).resolve()
    if destination.exists():
        raise AgentError("EVALUATION_EXISTS", "冻结目录已存在，不能覆盖")
    if len(sources) != 2:
        raise AgentError("EVALUATION_SOURCES", "需要两家不同公司的来源研究ID")
    symbols = [json.loads(ResearchStore.from_settings(settings).get(s)["scope"])["symbol"] for s in sources]
    if len(set(symbols)) != 2:
        raise AgentError("EVALUATION_SOURCES", "来源研究必须对应两家公司")
    destination.mkdir(parents=True)
    packs = [freeze_packs(settings, source, destination / f"company_{n}") for n, source in enumerate(sources)]
    cases = []
    for n in range(2):
        cases.append({"pack_root": f"company_{n}", "case": packs[n]["packs"][0], "name": "normal_" + symbols[n]})
    for name, base, excluded in (("cross_dimension_candidate", "cross_dimension", []),
                                 ("missing_fundamentals", "normal", ["get_financial_statements"]),
                                 ("historical", "historical", []), ("unknown_execution_rules", "normal", [])):
        original = next(c for c in packs[0]["packs"] if c["name"] == base)
        case = {**original, "name": name, "excluded_tools": excluded}
        cases.append({"pack_root": "company_0", "case": case, "name": name})
    manifest = {"schema_version": "2.1", "sources": sources, "companies": symbols, "cases": cases,
                "pack_hashes": {f"company_{n}": p["input_hash"] for n, p in enumerate(packs)}, "variants": VARIANTS21,
                "planned_runs": 24, "executed_runs": 0, "cross_dimension_conflict_confirmed": False,
                "note": "跨维度任务尚未证明存在真实观点冲突；规则未知是能力边界，不伪造交易限制。冻结不等于执行或验收。"}
    manifest["input_hash"] = digest(manifest)
    write_json(destination / "matrix.json", manifest)
    return manifest


def load_matrix(root):
    root = Path(root).resolve()
    value = json.loads((root / "matrix.json").read_text(encoding="utf-8"))
    if value["input_hash"] != digest({k: v for k, v in value.items() if k != "input_hash"}):
        raise AgentError("EVALUATION_INPUT_CHANGED", "对照清单哈希不匹配")
    for name, checksum in value["pack_hashes"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or FrozenPack(path).manifest["input_hash"] != checksum:
            raise AgentError("EVALUATION_INPUT_CHANGED", "对照输入已改变")
    for entry in value["cases"]:
        if entry["pack_root"] not in value["pack_hashes"] or not re.fullmatch(r"[A-Za-z0-9_.-]+", entry["name"]) or ".." in entry["name"]:
            raise AgentError("EVALUATION_INPUT_CHANGED", "对照案例路径或名称无效")
    return value


def frozen_service(settings):
    def denied(request):
        raise AgentError("FROZEN_NETWORK_DENIED", "冻结对照不允许任何在线数据回退")
    store = EvidenceStore(settings.data_dir)
    return DataService(settings, SourceHTTP(store, retries=0, client=httpx.Client(transport=httpx.MockTransport(denied))))


def execute_matrix(settings, root, *, model_factory=None, token_limit=2000000):
    """Explicit paid operation. Callers must obtain model/data authority first."""
    root = Path(root).resolve()
    manifest = load_matrix(root)
    records = []
    token_records = []
    output = settings.data_dir / "v21_evaluation" / uuid4().hex[:12]
    for case_index, entry in enumerate(manifest["cases"]):
        pack = FrozenPack(root / entry["pack_root"])
        case = entry["case"]
        raw = {k: v for k, v in case["request"].items() if k in ResearchRequest.model_fields}
        for variant_index, variant in enumerate(VARIANTS21):
            target = output / f"c{case_index}_v{variant_index}"
            pack.prepare(target)
            # Same overall limits and output cap for every comparison arm.
            old = settings.research.model_dump()
            for key in ("model_calls", "tool_calls", "source_requests", "wall_seconds", "request_seconds"):
                old[key] = getattr(settings.decision, key)
            old.update(reserve_model=8, reserve_tool=12, reserve_seconds=120, reserve_source=0, role_models={}, model_profiles={})
            # Fix provider, per-call output ceiling and context envelope for all
            # arms. Role-specific model differences are not a debate benefit.
            common_model = settings.model.model_copy(update={"max_output_tokens": settings.decision.manager_output_tokens})
            common_decision = settings.decision.model_copy(update={"role_models": {}, "model_profiles": {}})
            scoped = settings.model_copy(update={"data_dir": target, "model": common_model, "decision": common_decision,
                                                 "research": ResearchSettings.model_validate(old)})
            envelope = TokenEnvelope(token_limit)
            def bounded_model(role):
                from ..models import CompatibleChatModel
                model = model_factory(role) if model_factory else CompatibleChatModel(common_model)
                return TokenEnvelopeModel(model, envelope, common_model)
            def factory(s, store, budget, scope):
                class EvaluationRunner(DecisionRunner):
                    def context(self, *args, **kwargs):
                        ctx = super().context(*args, **kwargs)
                        ctx.limits.context_chars = 250000
                        return ctx
                runner = EvaluationRunner(s, store, budget, scope, model_factory=bounded_model, service_factory=lambda: frozen_service(s))
                runner.registry_transform = lambda registry: pack.registry(registry, case)
                return runner
            if variant == "single":
                # Reuse baseline executor, with network disabled at the HTTP
                # layer as well as exact-argument tool replay.
                state = run_variant(scoped, pack, {**case, "request": raw}, "single", runner_factory=factory)
            elif variant == "experts_review":
                state = ResearchCoordinator(scoped, runner_factory=factory).run(ResearchRequest.model_validate(raw))
            else:
                rounds = 1 if variant == "decision_one_round" else 2
                state = ResearchCoordinator(scoped, runner_factory=factory).run(DecisionRequest(**raw, debate_rounds=rounds, risk_rounds=rounds))
            records.append((variant, state))
            token_records.append({"variant": variant, "research_id": state["id"], **envelope.snapshot()})
            summary = summarize_runs(records)
            summary["argument_annotations"], summary["decision_annotations"] = [], []
            for label, saved in records:
                if not saved["report"]:
                    continue
                report = json.loads(Path(saved["report"]).with_suffix(".json").read_text(encoding="utf-8"))
                for a in report.get("artifacts", []):
                    for r in a.get("responses", []):
                        summary["argument_annotations"].append({"research_id": saved["id"], "variant": label, "response": r, "human_on_target": None})
                    if a.get("role") in ("research_manager", "portfolio_manager"):
                        summary["decision_annotations"].append({"research_id": saved["id"], "variant": label, "artifact": a, "human_consistent": None, "critical_error": None})
            write_json(output / "comparison.json", {"input_hash": manifest["input_hash"], "comparison_limits": {
                "model_calls": settings.decision.model_calls, "per_call_output_tokens": common_model.max_output_tokens,
                "context_chars": 250000, "role_model_overrides": False, "total_token_envelope": token_limit,
                "note": "同服务商和保守token总额度；未知用量不退款，供应商超出估计则记录偏差，不能记为可比预算通过"},
                "token_envelopes": token_records, **summary})
    return output / "comparison.json"
