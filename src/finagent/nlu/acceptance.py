"""Actual Qwen predictions + controlled identity replay; never executes research."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import copy

from .contracts import NLUResult
from .parser import prepare
from .qwen3 import Qwen3Parser
from .bundle import file_hash
from .metrics import exact, semantic_metrics, semantic_gates
from .semantic import VERSION, strict_json, validate_frame
from ..data.evidence import EvidenceStore, write_json
from ..errors import AgentError
from ..provenance import package_digest
from ..session import Conversation


def evaluate(settings, model_dir, test_path, oracle_path, output, *, mode_boundaries=None, parser=None):
    if Path(output).exists():
        raise ValueError("验收报告已存在，请使用新输出文件")
    settings = settings.model_copy(deep=True)
    settings.nlu.model_dir = Path(model_dir).resolve()
    predictor = parser or Qwen3Parser(settings, diagnostic=True)
    rows = [strict_json(s) for s in Path(test_path).read_text(encoding="utf-8-sig").splitlines() if s.strip()]
    oracles = [strict_json(s) for s in Path(oracle_path).read_text(encoding="utf-8-sig").splitlines() if s.strip()]
    if not rows or len({v["id"] for v in rows}) != len(rows) or len({v["id"] for v in oracles}) != len(oracles) or {v["id"] for v in oracles} != {r["id"] for r in rows}:
        raise ValueError("冻结测试数据与宿主oracle必须非空且按唯一ID一一对应")
    by_id = {v["id"]: v for v in oracles}
    golds, predictions, cases = [], [], []
    eligible = accepted = correctly_accepted = covered = independent = invalid_started = 0
    required = {"outcome", "symbol", "mode", "pipeline", "analysts", "identity_queries", "news_days", "policy_days", "as_of_date", "scope_constraints"}
    temp_root = settings.data_dir / "nlu-evaluation-tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if "messages" not in row or len(row["messages"]) != 3:
            raise ValueError("需要新SFT格式冻结数据；旧PLM数据须先迁移")
        user = strict_json(row["messages"][1]["content"])
        gold = validate_frame(strict_json(row["messages"][2]["content"]), user["text"])
        oracle = by_id[row["id"]]
        expected = oracle["expected"]
        if set(expected) != required or expected["outcome"] not in ("accept", "clarify", "reject"):
            raise ValueError("每条oracle必须完整定义宿主字段，包括无操作的null值")
        verified = (oracle.get("annotation_status") == "human_verified" and
                    row.get("metadata", {}).get("annotation_status") in ("approved", "human_verified") and
                    row.get("metadata", {}).get("synthetic") is False and
                    row.get("metadata", {}).get("purpose") == "independent_acceptance")
        independent += verified
        prediction = result = None
        error_code = None
        try:
            result = predictor.parse(user["text"], user.get("context", {}))
            prediction = validate_frame({k: getattr(result, k) if k != "entities" else [e.model_dump(exclude={"score"}) for e in result.entities]
                                         for k in ("schema_version", "task_type", "dialogue_act", "entity_binding", "dimension_state", "entities")}, user["text"])
        except AgentError as exc:
            error_code = exc.code
        golds.append(gold); predictions.append(prediction)
        queries = []
        actual = dict.fromkeys(required)
        if result is None:
            actual.update(outcome="reject", identity_queries=[])
        else:
            class FixedParser:
                def parse(self, text, context):
                    # Diagnostic parsing cannot execute. The override is confined to
                    # this offline oracle replay, which never invokes any coordinator.
                    return result.model_copy(update={"diagnostic_only": False, "admission": "accept"}, deep=True)
            class Replay:
                store = SimpleNamespace(save_result=lambda value: value)
                def resolve_entity(self, query, ctx):
                    queries.append(query); ctx.source_request()
                    return SimpleNamespace(data=oracle.get("identity_replay", {}).get(query, {"candidates": []}))
            with TemporaryDirectory(dir=temp_root) as temp:
                local = settings.model_copy(deep=True); local.data_dir = Path(temp)
                conversation = Conversation(EvidenceStore(local.data_dir), "acceptance")
                context = user.get("context", {})
                conversation.state.update(symbol=context.get("confirmed_symbol"), scope=copy.deepcopy(context.get("scope", {})),
                                          last_research_id=context.get("research_id"), history=[{"user": v} for v in context.get("history", [])])
                try:
                    prepared = prepare(user["text"], local, conversation, parser=FixedParser(), service=Replay(), as_of=oracle.get("clock"))
                    if prepared.result.task_type in ("research_status", "research_report", "research_resume"):
                        if not context.get("research_id") or prepared.result.task_type == "research_resume":
                            raise AgentError("NEEDS_CLARIFICATION", "需要明确研究ID或显式恢复")
                    actual.update(outcome="accept", symbol=prepared.ctx.symbol, mode=prepared.mode, pipeline=prepared.pipeline,
                                  analysts=prepared.roles, news_days=prepared.news_days, policy_days=prepared.policy_days,
                                  as_of_date=prepared.ctx.as_of.date().isoformat(), scope_constraints=prepared.ctx.scope_constraints)
                except AgentError as exc:
                    error_code = exc.code
                    actual.update(outcome="clarify" if exc.code in ("NEEDS_CLARIFICATION", "RESEARCH_REQUIRED") else "reject")
                actual["identity_queries"] = queries
        fields = required if actual["outcome"] == "accept" else {"outcome", "identity_queries"}
        matched = all(actual[k] == expected[k] for k in fields)
        eligible += expected["outcome"] == "accept"
        if actual["outcome"] == "accept":
            accepted += 1
            covered += expected["outcome"] == "accept"
            correctly_accepted += expected["outcome"] == "accept" and matched and exact(gold, prediction)
            invalid_started += prediction is None
        cases.append({"id": row["id"], "actual": actual, "expected": expected, "matched": matched,
                      "prediction": prediction, "full_frame_correct": exact(gold, prediction), "error_code": error_code})
    metrics = semantic_metrics(golds, predictions)
    boundary = strict_json(Path(mode_boundaries).read_text(encoding="utf-8")) if mode_boundaries else {}
    implementation = package_digest()
    boundary_ok = (boundary.get("schema_version") == "execution22.boundaries.1" and boundary.get("implementation") == implementation and
                   boundary.get("passed") is True and boundary.get("cases", 0) >= 14 and boundary.get("errors") == 0 and boundary.get("skipped") == 0)
    semantic_ok = semantic_gates(metrics)
    accuracy, coverage = correctly_accepted / accepted if accepted else None, covered / eligible if eligible else None
    gates = (independent == len(rows) and independent >= 300 and boundary_ok and semantic_ok and invalid_started == 0
             and accuracy is not None and accuracy >= .95 and coverage is not None and coverage >= .70)
    report = {"schema_version": "nlu_sft2.host_acceptance.1", "deployment_fingerprint": predictor.fingerprint,
              "implementation": implementation, "test_sha256": file_hash(test_path), "oracle_sha256": file_hash(oracle_path),
              "mode_boundary_sha256": file_hash(mode_boundaries) if mode_boundaries else None,
              "mode_boundary_passed": boundary_ok, "independent_human_samples": independent,
              "semantic_gates_passed": semantic_ok, "host_gates_passed": gates, "metrics": metrics,
              "nonambiguous": eligible, "accepted": accepted, "correct_accepted": correctly_accepted,
              "accepted_accuracy": accuracy, "coverage": coverage,
              "automatic_correct_fraction": correctly_accepted / eligible if eligible else None,
              "invalid_output_started": invalid_started, "cases": cases,
              "note": "真实模型预测+证券身份回放；不执行研究，不能替代实时证券可用性验收"}
    write_json(Path(output), report)
    return report
