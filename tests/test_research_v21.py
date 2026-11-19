"""v2.1 offline contracts and real-executor fault tests; not financial evaluation."""
from datetime import datetime
import json
from pathlib import Path
from threading import Event
from unittest.mock import patch
import unittest
import subprocess
import sys

from pydantic import ValidationError
from finagent.config import DecisionSettings, Settings
from finagent.errors import AgentError
from finagent.research.contracts import ResearchRequest, ResearchScope
from finagent.research.decision_contracts import DecisionRequest, DecisionScope
from finagent.research.coordinator import ResearchCoordinator
from finagent.research.decision_coordinator import DecisionCoordinator
from finagent.research.decision_runner import DecisionRunner
from finagent.research.decision_reporting import read_trace
from finagent.research.entry import request_from_goal
from finagent.research.routing import research_route
from finagent.research.quality import research_bundle
from finagent.research.store import ResearchStore
from finagent.research.budget import ResearchBudget
from finagent.research.workflow import plan, seal, ref, validate_output, validate_config
from finagent.runtime import TZ
from finagent.data.service import DataService
from tests.support import ServiceCase, call_reply
from tests.research21_support import tracker, SyntheticRunner, decision_body, expert_body, synthetic_grants

CUTOFF = datetime(2025, 5, 8, 23, 59, 59, tzinfo=TZ)


class V21Tests(ServiceCase):
    def request(self, **kwargs):
        return DecisionRequest(goal="SYNTHETIC", symbol="600519.SH", as_of=CUTOFF, **kwargs)

    def coordinator(self, t):
        return ResearchCoordinator(self.settings, runner_factory=lambda *args: SyntheticRunner(t, *args))

    def report(self, state):
        return json.loads(Path(state["report"]).with_suffix(".json").read_text(encoding="utf-8"))

    def test_legacy_scope_hash_and_versioned_scope(self):
        old = ResearchScope(goal="x", symbol="600519.SH", as_of=CUTOFF)
        self.assertNotIn("pipeline", old.model_dump())
        self.assertEqual(old, ResearchScope.model_validate_json(old.model_dump_json()))
        scope = DecisionScope(**self.request().model_dump())
        with self.assertRaises(ValidationError):
            scope.debate_rounds = 3
        with self.assertRaises(ValidationError):
            DecisionScope(**{**scope.model_dump(), "scope_hash": "bad"})

    def test_modes_validate_before_online_resolution(self):
        from finagent.research.selection import select, SelectedRequest
        with self.assertRaises(AgentError):
            request_from_goal("深入分析贵州茅台", self.settings)
        req = select(self.settings, mode="full_decision", symbol="600519.SH", source="cli_flag", as_of=CUTOFF,
                     risk_profile="conservative", position_context="not_held")
        self.assertIsInstance(req, SelectedRequest)
        self.assertEqual(req.debate_rounds, 2)
        self.assertEqual(req.position_context, "not_held")
        self.assertEqual(req.risk_profile, "conservative")
        with self.assertRaises(AgentError):
            request_from_goal("未知公司", self.settings, depth="specialist", pipeline="trading")
        self.assertEqual(research_route("什么是多空辩论")[0], "direct")

    def test_round_budget_admission_and_default_arithmetic(self):
        scope = DecisionScope(**self.request().model_dump())
        self.assertEqual(len(plan(scope)), 20)
        validate_config(scope, self.settings.decision)
        with self.assertRaises(AgentError):
            validate_config(DecisionScope(**self.request(debate_rounds=3, risk_rounds=3).model_dump()), self.settings.decision)
        with self.assertRaises(ValidationError):
            DecisionSettings(reserve_model=1)
        self.assertEqual(self.settings.research.model_calls, 48)
        self.assertEqual(self.settings.decision.model_calls, 128)

    def test_longform_cap_is_explicit_and_role_profile_has_priority(self):
        from finagent.config import ModelSettings
        scope = DecisionScope(**self.request().model_dump())
        database = ResearchStore(self.root)
        run = database.create(scope, self.settings.decision, "x")
        budget = ResearchBudget(database, run, database.acquire(run, "x"), self.settings.decision, Event())
        runner = DecisionRunner(self.settings, database, budget, scope)
        self.assertEqual(runner.model_settings("research_manager").max_output_tokens, 12000)
        self.assertEqual(runner.model_settings("quality_gate").max_output_tokens, 12000)
        self.assertEqual(runner.model_settings("bull").max_output_tokens, self.settings.model.max_output_tokens)
        budget.config = DecisionSettings(model_profiles={"explicit": ModelSettings(max_output_tokens=4096)}, role_models={"research_manager": "explicit"})
        self.assertEqual(runner.model_settings("research_manager").max_output_tokens, 4096)

    def test_full_chain_sequence_and_complete_report(self):
        t = tracker()
        state = self.coordinator(t).run(self.request())
        report = self.report(state)
        self.assertEqual(state["content"], "complete", report["errors"])
        self.assertEqual(report["process_status"], "full")
        self.assertEqual(report["decision_status"], "decided")
        downstream = [x for x in t["calls"] if x.startswith(("bull_", "bear_", "risk_"))]
        self.assertEqual(downstream, ["bull_1", "bear_1", "bull_2", "bear_2", "risk_aggressive_1", "risk_conservative_1", "risk_neutral_1", "risk_aggressive_2", "risk_conservative_2", "risk_neutral_2"])
        trace = read_trace(ResearchStore(self.root), state["id"], "debate")
        self.assertEqual(len(trace["artifacts"]), 4)
        self.assertFalse(report["outcome_verified"])

    def test_initialization_failure_releases_owner_and_can_resume(self):
        coordinator = self.coordinator(tracker())
        with patch("finagent.research.decision_coordinator.plan", side_effect=RuntimeError("initialization failed")):
            with self.assertRaises(RuntimeError):
                coordinator.run(self.request())
        with coordinator.store.connect() as db:
            run = db.rows("research_runs")[0]["id"]
        row = coordinator.store.get(run)
        self.assertIsNone(row["owner"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["reason"], "INTERNAL_ERROR")
        self.assertEqual(self.report(coordinator.store.snapshot(run))["decision_status"], "unavailable")
        self.assertEqual(coordinator.resume(run)["content"], "complete")

    def test_runner_initialization_failure_exports_stages_without_calls(self):
        coordinator = DecisionCoordinator(self.settings, runner_factory=lambda *args: (_ for _ in ()).throw(RuntimeError("runner failed")))
        with self.assertRaises(RuntimeError):
            coordinator.run(self.request())
        with coordinator.store.connect() as db:
            run = db.rows("research_runs")[0]["id"]
        row = coordinator.store.get(run)
        self.assertIsNone(row["owner"])
        report = self.report(coordinator.store.snapshot(run))
        self.assertEqual(report["process_status"], "unavailable")
        self.assertEqual(report["budget"]["calls"].get("model", 0), 0)
        self.assertEqual(len(report["stages"]), 20)
        self.assertTrue(all(s["status"] == "skipped" for s in report["stages"]))

    def test_failed_review_is_limited_not_empty_pass(self):
        t = tracker(fail={"review"})
        report = self.report(self.coordinator(t).run(self.request()))
        self.assertEqual(report["quality_gate"]["gate_status"], "limited")
        self.assertEqual(report["decision_status"], "abstained")
        self.assertTrue(all(a["decision_status"] == "abstained" for a in report["artifacts"] if "decision_status" in a))

    def test_major_issues_resolve_only_with_closing_and_no_new_followups(self):
        t = tracker(issues=[{"task_id": "news", "claim_id": "c", "severity": "major", "description": "合成争点", "question": "核对原文", "success_condition": "存在原文依据"}])
        report = self.report(self.coordinator(t).run(self.request()))
        self.assertEqual(t["calls"].count("review"), 1)
        self.assertEqual(t["calls"].count("quality_gate"), 1)
        self.assertEqual(sum(x.startswith("followup_") for x in t["calls"]), 1)
        self.assertEqual(report["review_status"], "passed", report["errors"])
        self.assertEqual(report["quality_gate"]["issues"][0]["state"], "resolved")

    def test_critical_issue_blocks_downstream(self):
        t = tracker(issues=[{"task_id": "news", "claim_id": "c", "severity": "critical", "description": "合成关键错误"}], fail={"quality_gate"})
        report = self.report(self.coordinator(t).run(self.request()))
        self.assertEqual(report["quality_gate"]["gate_status"], "blocked")
        self.assertFalse(any(x.startswith("bull_") for x in t["calls"]))
        self.assertEqual(report["decision_status"], "unavailable")

    def test_partial_scope_abstains_without_fake_five_dimensions(self):
        report = self.report(self.coordinator(tracker()).run(self.request(analysts=["news", "policy"])))
        self.assertEqual(report["content_status"], "complete", report["errors"])
        self.assertEqual(report["process_status"], "full")
        self.assertEqual(report["decision_status"], "abstained")
        self.assertEqual(set(report["coverage"]), {"news", "policy"})

    def test_missing_experts_block_no_fabricated_decision(self):
        t = tracker(fail=set(self.request().analysts))
        report = self.report(self.coordinator(t).run(self.request()))
        self.assertEqual(report["content_status"], "unavailable")
        self.assertIsNone(report["portfolio_decision"])
        self.assertTrue(all(s["status"] == "skipped" for s in report["stages"] if s["role"] == "portfolio_manager"))

    def test_failed_bear_stops_later_debate_but_retains_limited_judgment(self):
        t = tracker(fail={"bear_1"})
        report = self.report(self.coordinator(t).run(self.request()))
        self.assertNotIn("bull_2", t["calls"])
        self.assertEqual(report["decision_status"], "abstained", report["errors"])
        self.assertEqual(report["process_status"], "partial")

    def test_manager_failure_skips_trader_risk_final(self):
        t = tracker(fail={"research_manager"})
        report = self.report(self.coordinator(t).run(self.request()))
        self.assertNotIn("trader", t["calls"])
        self.assertNotIn("portfolio_manager", t["calls"])
        self.assertEqual(report["decision_status"], "unavailable")

    def test_risk_failure_stops_later_turns_and_final_abstains(self):
        t = tracker(fail={"risk_conservative_1"})
        report = self.report(self.coordinator(t).run(self.request()))
        self.assertNotIn("risk_neutral_1", t["calls"])
        self.assertEqual(report["decision_status"], "abstained", report["errors"])

    def test_final_failure_does_not_promote_trader(self):
        report = self.report(self.coordinator(tracker(fail={"portfolio_manager"})).run(self.request()))
        self.assertEqual(report["decision_status"], "unavailable")
        self.assertIsNone(report["portfolio_decision"])

    def test_resume_reuses_turns_and_resumes_first_failed_task(self):
        t = tracker(fail={"portfolio_manager"})
        coordinator = self.coordinator(t)
        first = coordinator.run(self.request())
        before = list(t["calls"])
        # Both failed attempts consumed; no third attempt or fresh budget.
        t["fail"].clear()
        second = coordinator.resume(first["id"])
        self.assertEqual(t["calls"], before)
        self.assertEqual(self.report(second)["decision_status"], "unavailable")

    def test_cancel_does_not_open_summary_model(self):
        stop = Event()
        stop.set()
        t = tracker()
        state = self.coordinator(t).run(self.request(), cancelled=stop)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(t["calls"], [])
        self.assertIsNotNone(state["report"])

    def test_nested_reserve_keeps_final_pool(self):
        scope = DecisionScope(**self.request().model_dump())
        database = ResearchStore(self.root)
        run = database.create(scope, self.settings.decision, "x")
        owner = database.acquire(run, "x")
        budget = ResearchBudget(database, run, owner, self.settings.decision, Event())
        for _ in range(self.settings.decision.model_calls - self.settings.decision.reserve_model):
            budget.reserve("model", "expert")
        with self.assertRaises(AgentError):
            budget.reserve("model", "expert")
        for _ in range(self.settings.decision.reserve_model - self.settings.decision.final_model):
            budget.reserve("model", "risk", "downstream")
        with self.assertRaises(AgentError):
            budget.reserve("model", "risk", "downstream")
        for _ in range(4):
            budget.reserve("model", "portfolio_manager", "final")
        with self.assertRaises(AgentError):
            budget.reserve("model", "portfolio_manager", "final")

    def test_frozen_evaluation_http_layer_has_no_network_fallback(self):
        from finagent.research.evaluation21 import frozen_service
        from finagent.runtime import ExecutionContext
        service = frozen_service(self.settings)
        try:
            with self.assertRaisesRegex(AgentError, "FROZEN_NETWORK_DENIED"):
                service.http.get("tencent", "https://qt.gtimg.cn/q=sh600519", ExecutionContext(as_of=CUTOFF))
        finally:
            service.close()

    def test_four_way_matrix_runs_with_scripted_models_and_frozen_synthetic_inputs(self):
        from finagent.research.evaluation21 import freeze_matrix, execute_matrix
        database = ResearchStore(self.root)
        self.store.save_raw(b'SYNTHETIC_FROZEN_INPUT', {"source": "SYNTHETIC"})
        ids = [database.create(ResearchScope(goal="SYNTHETIC", symbol=s, as_of=CUTOFF), self.settings.research, "x")
               for s in ("600519.SH", "300750.SZ")]
        pack = self.root / "synthetic_matrix"
        freeze_matrix(self.settings, ids, pack)
        class Model:
            def __init__(self, role):
                self.role = role
            def complete(self, messages, tools, timeout):
                if self.role == "synthesis":
                    raise AgentError("SYNTHETIC_NO_CLAIMS", "合成无来源")
                body = {"summary": "SYNTHETIC", "issues": []} if self.role == "review" else {
                    "content_status": "unavailable", "summary": "SYNTHETIC", "report_markdown": "SYNTHETIC", "claims": [], "missing": ["合成无来源"]}
                reply = call_reply(("submit", "submit_analysis", body))
                reply.usage = {"prompt_tokens": 100, "completion_tokens": 20}
                return reply
        result = json.loads(execute_matrix(self.settings, pack, model_factory=Model).read_text(encoding="utf-8"))
        self.assertEqual(len(result["runs"]), 24)
        self.assertEqual(len(result["token_envelopes"]), 24)
        self.assertTrue(all(v["limit"] == 2000000 and v["pending"] == 0 for v in result["token_envelopes"]))
        self.assertFalse(any(v["provider_estimate_overrun"] for v in result["token_envelopes"]))
        self.assertIsNone(result["support_rate"])
        self.assertEqual(self.requests, [])

    def test_public_validation_copy_excludes_private_corpus_and_environment(self):
        from scripts.validate_v21 import prepare_public
        from finagent.data.evidence import write_json
        raw = self.store.save_raw(b'SYNTHETIC', {"source": "SYNTHETIC"})
        write_json(self.root / "library/private.json", {"source_url": "https://private.example.test/report", "snapshot_id": raw})
        (self.root / ".env").write_text("SYNTHETIC=not_a_real_key", encoding="utf-8")
        target = self.root / "public_only"
        self.assertEqual(prepare_public(self.settings, target), [])
        self.assertFalse((target / ".env").exists())
        self.assertFalse((target / "library/private.json").exists())

    def test_cli_explicit_research_and_trace_are_wired_without_paid_calls(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from finagent.cli import main
        t = tracker()
        real = self.coordinator(t)
        with patch("finagent.cli.load_settings", return_value=self.settings), patch("finagent.research.coordinator.ResearchCoordinator", return_value=real), redirect_stdout(StringIO()) as output:
            code = main(["research", "--symbol", "600519.SH", "--goal", "SYNTHETIC", "--mode", "full_decision"])
        self.assertEqual(code, 0)
        state = json.loads(output.getvalue())
        with patch("finagent.cli.load_settings", return_value=self.settings), redirect_stdout(StringIO()) as trace:
            code = main(["research-trace", state["id"], "--stage", "risk"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(trace.getvalue())["artifacts"]), 6)

    def test_mysql_namespace_is_reused_without_creating_sqlite(self):
        first = ResearchStore(self.root)
        second = ResearchStore(self.root)
        self.assertEqual(first.scope.id, second.scope.id)
        self.assertFalse((self.root / "research.sqlite3").exists())
        first.database.check()

    def test_changed_upstream_invalidates_all_old_downstream_even_if_budget_exhausts(self):
        t = tracker(partial={"policy"})
        coordinator = self.coordinator(t)
        first = coordinator.run(self.request())
        self.assertEqual(self.report(first)["decision_status"], "abstained")
        old_final = self.report(first)["portfolio_decision"]["artifact_id"]
        t["partial"].clear()
        second = coordinator.resume(first["id"])
        all_current = read_trace(ResearchStore(self.root), first["id"])["artifacts"]
        self.assertNotIn(old_final, [a["artifact_id"] for a in all_current])
        self.assertTrue(any(t["status"] == "invalidated" for t in second["tasks"] if t["id"] == "portfolio_manager"))
        self.assertEqual(self.report(second)["decision_status"], "unavailable")

    def test_dependency_refresh_can_finish_when_frozen_attempt_budget_allows(self):
        self.settings.decision = DecisionSettings(max_attempts=40)
        t = tracker(partial={"policy"})
        coordinator = self.coordinator(t)
        first = coordinator.run(self.request())
        old_final = self.report(first)["portfolio_decision"]["artifact_id"]
        t["partial"].clear()
        second = coordinator.resume(first["id"])
        report = self.report(second)
        self.assertEqual(report["content_status"], "complete", report["errors"])
        self.assertNotEqual(report["portfolio_decision"]["artifact_id"], old_final)
        self.assertEqual(t["calls"].count("technical"), 1)
        self.assertEqual(t["calls"].count("policy"), 2)
        self.assertEqual(t["calls"].count("bull_1"), 2)

    def test_real_process_exit_mid_debate_resumes_without_repeating_prior_turns(self):
        code = '''
import os,sys
from pathlib import Path
from finagent.config import Settings
from finagent.research.coordinator import ResearchCoordinator
from finagent.research.decision_contracts import DecisionRequest
from tests.research21_support import tracker,SyntheticRunner
from tests.test_research_v21 import CUTOFF
class Crash(SyntheticRunner):
 def run(self,role,task,attempt,**kwargs):
  if task=='bull_2': os._exit(23)
  return super().run(role,task,attempt,**kwargs)
s=Settings(data_dir=Path(sys.argv[1]),source_retries=0)
t=tracker()
ResearchCoordinator(s,runner_factory=lambda *a:Crash(t,*a)).run(DecisionRequest(goal='SYNTHETIC',symbol='600519.SH',as_of=CUTOFF))
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 23, result.stderr.decode(errors="replace"))
        database = ResearchStore(self.root)
        with database.connect() as db:
            run = db.rows("research_runs")[0]["id"]
        t = tracker()
        state = self.coordinator(t).resume(run)
        self.assertEqual(self.report(state)["process_status"], "full", self.report(state)["errors"])
        self.assertNotIn("bull_1", t["calls"])
        self.assertNotIn("bear_1", t["calls"])
        self.assertIn("bull_2", t["calls"])
        self.assertGreater(state["active_used"], 0)

    def test_native_agentloop_executes_all_roles_offline(self):
        self._assert_native_agentloop_roles()

    def test_native_roles_preserve_artifact_ids_with_phone_like_digits(self):
        from itertools import count
        from types import SimpleNamespace
        sequence = count()
        def identifier():
            return SimpleNamespace(hex=f'a{next(sequence):09x}' + '13800138000' + 'b' * 11)
        with patch('finagent.research.workflow.uuid4', side_effect=identifier):
            self._assert_native_agentloop_roles()

    def _assert_native_agentloop_roles(self):
        settings = self.settings
        contexts = {}
        class NativeRunner(DecisionRunner):
            def context(inner, task, attempt, reserved=False, followup=False, grants=None):
                ctx = super().context(task, attempt, reserved, followup, grants)
                if task in inner.scope.analysts:
                    values = synthetic_grants(self.store, inner.store, inner.budget, task, task, CUTOFF)
                    for tool, value in values:
                        ctx.result_tools[value.result_id] = tool
                        ctx.evidence_ids.update([value.result_id, value.snapshot_id])
                    contexts[task] = (inner.scope, values[0][1])
                return ctx
        class Model:
            def __init__(inner, role):
                inner.role = role
            def complete(inner, messages, tools, timeout):
                goal = next(value for m in messages if m["role"] == "user"
                            for value in [json.loads(m["content"])] if "task" in value and "scope" in value)
                if inner.role in self.request().analysts:
                    scope, value = contexts[inner.role]
                    body = expert_body(inner.role, value, scope)
                    body = {k: v for k, v in body.items() if k not in ("analyst", "scope_hash")}
                elif inner.role == "review":
                    body = {"summary": "SYNTHETIC审阅", "issues": []}
                else:
                    scope = DecisionScope.model_validate(goal["scope"])
                    body = decision_body(inner.role, goal["task"], goal["artifacts"], scope, goal["bundle"])
                return call_reply(("submit", "submit_analysis", body))
        factory = lambda *args: NativeRunner(*args, model_factory=Model, service_factory=lambda: DataService(settings, self.http))
        state = ResearchCoordinator(settings, runner_factory=factory).run(self.request())
        report = self.report(state)
        self.assertEqual(report["process_status"], "full", report["errors"])
        self.assertEqual(report["content_status"], "complete", report["errors"])
        self.assertEqual(state["budget"]["calls"]["model"], 19)
        self.assertEqual(self.requests, [])


class WorkflowBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.scope = DecisionScope(goal="SYNTHETIC", symbol="600519.SH", as_of=CUTOFF)
        self.bundle = {"gate_status": "pass", "decision_eligible": True, "debate_complete": True, "risk_complete": True, "issues": []}
        self.artifacts = [seal({"analyst": "news", "claims": [{"claim_id": "c", "text": "合成测试主张"}], "content_status": "complete"},
                               self.scope, "synthetic", "news", "news", 1, [])]

    def append(self, role, task):
        body = decision_body(role, task, self.artifacts, self.scope, self.bundle)
        body = validate_output(role, body, self.artifacts, self.scope, task, self.bundle)
        a = seal(body, self.scope, "synthetic", task, role, 1, list(self.artifacts))
        self.artifacts.append(a)
        return a

    def debate(self):
        for r in range(1, 3):
            for role in ("bull", "bear"):
                self.append(role, f"{role}_{r}")

    def risk(self):
        self.debate()
        self.append("research_manager", "research_manager")
        self.append("trader", "trader")
        for r in range(1, 3):
            for role in ("risk_aggressive", "risk_conservative", "risk_neutral"):
                self.append(role, f"{role}_{r}")

    def test_future_or_forged_hash_reference_rejected(self):
        body = decision_body("bull", "bull_1", self.artifacts, self.scope, self.bundle)
        body["arguments"][0]["claim_refs"][0]["artifact_hash"] = "0" * 64
        with self.assertRaisesRegex(AgentError, "INVALID_ARTIFACT_REFERENCE"):
            validate_output("bull", body, self.artifacts, self.scope, "bull_1", self.bundle)

    def test_tampered_handoff_rejected_even_when_reference_hash_copied(self):
        body = decision_body("bull", "bull_1", self.artifacts, self.scope, self.bundle)
        self.artifacts[0]["claims"][0]["text"] = "篡改"
        with self.assertRaisesRegex(AgentError, "ARTIFACT_CHANGED"):
            validate_output("bull", body, self.artifacts, self.scope, "bull_1", self.bundle)

    def test_wrong_turn_order_rejected(self):
        body = decision_body("bear", "bear_1", self.artifacts, self.scope, self.bundle)
        with self.assertRaisesRegex(AgentError, "TURN_ORDER"):
            validate_output("bear", body, self.artifacts, self.scope, "bear_1", self.bundle)

    def test_same_side_response_is_not_a_rebuttal(self):
        bull = self.append("bull", "bull_1")
        self.append("bear", "bear_1")
        body = decision_body("bull", "bull_2", self.artifacts, self.scope, self.bundle)
        body["responses"][0]["target"] = ref(bull, "a")
        with self.assertRaisesRegex(AgentError, "INVALID_RESPONSE_TARGET"):
            validate_output("bull", body, self.artifacts, self.scope, "bull_2", self.bundle)

    def test_non_initial_turn_must_respond(self):
        self.append("bull", "bull_1")
        body = decision_body("bear", "bear_1", self.artifacts, self.scope, self.bundle)
        body["responses"] = []
        with self.assertRaisesRegex(AgentError, "RESPONSE_REQUIRED"):
            validate_output("bear", body, self.artifacts, self.scope, "bear_1", self.bundle)

    def test_abstaining_opponent_allows_explicit_no_response(self):
        body = {"side": "bull", "round": 1, "turn_index": 1, "arguments": [], "abstention_reason": "没有依据"}
        body = validate_output("bull", body, self.artifacts, self.scope, "bull_1", self.bundle)
        bull = seal(body, self.scope, "synthetic", "bull_1", "bull", 1, self.artifacts)
        self.artifacts.append(bull)
        body = {"side": "bear", "round": 1, "turn_index": 2, "arguments": [], "abstention_reason": "没有依据",
                "no_response_reason": "对方无论点", "no_response_refs": [ref(bull)]}
        self.assertTrue(validate_output("bear", body, self.artifacts, self.scope, "bear_1", self.bundle))

    def test_unregistered_number_and_execution_parameters_rejected(self):
        body = decision_body("bull", "bull_1", self.artifacts, self.scope, self.bundle)
        body["arguments"][0]["text"] = "利润999亿元"
        with self.assertRaisesRegex(AgentError, "UNSUPPORTED_DECISION_NUMBER"):
            validate_output("bull", body, self.artifacts, self.scope, "bull_1", self.bundle)
        body["arguments"][0]["text"] = "目标价120元"
        with self.assertRaisesRegex(AgentError, "EXECUTION_PARAMETERS_DENIED"):
            validate_output("bull", body, self.artifacts, self.scope, "bull_1", self.bundle)

    def test_manager_cannot_skip_core_disputes(self):
        self.debate()
        body = decision_body("research_manager", "research_manager", self.artifacts, self.scope, self.bundle)
        body["issue_rulings"] = []
        with self.assertRaisesRegex(AgentError, "RULING_MISSING"):
            validate_output("research_manager", body, self.artifacts, self.scope, "research_manager", self.bundle)

    def test_manager_ruling_inherits_target_evidence_without_duplicate_refs(self):
        self.debate()
        body = decision_body("research_manager", "research_manager", self.artifacts, self.scope, self.bundle)
        for r in body["issue_rulings"]:
            r["claim_refs"] = []
        self.assertTrue(validate_output("research_manager", body, self.artifacts, self.scope, "research_manager", self.bundle))

    def test_limited_gate_cannot_produce_intermediate_buy(self):
        self.debate()
        body = decision_body("research_manager", "research_manager", self.artifacts, self.scope, self.bundle)
        self.bundle["gate_status"] = "limited"
        with self.assertRaisesRegex(AgentError, "DECISION_REQUIRES_ABSTENTION"):
            validate_output("research_manager", body, self.artifacts, self.scope, "research_manager", self.bundle)

    def test_risk_cannot_switch_proposal(self):
        self.debate()
        manager = self.append("research_manager", "research_manager")
        self.append("trader", "trader")
        body = decision_body("risk_aggressive", "risk_aggressive_1", self.artifacts, self.scope, self.bundle)
        body["proposal_ref"] = ref(manager)
        with self.assertRaisesRegex(AgentError, "PROPOSAL_MISMATCH"):
            validate_output("risk_aggressive", body, self.artifacts, self.scope, "risk_aggressive_1", self.bundle)

    def test_final_must_consume_all_risk_views(self):
        self.risk()
        body = decision_body("portfolio_manager", "portfolio_manager", self.artifacts, self.scope, self.bundle)
        body["risk_refs"] = body["risk_refs"][:1]
        with self.assertRaisesRegex(AgentError, "RISK_COVERAGE_MISSING"):
            validate_output("portfolio_manager", body, self.artifacts, self.scope, "portfolio_manager", self.bundle)

    def test_final_cannot_omit_opposition(self):
        self.risk()
        body = decision_body("portfolio_manager", "portfolio_manager", self.artifacts, self.scope, self.bundle)
        body["accepted_rejected_views"] = [body["accepted_rejected_views"][0]]
        with self.assertRaisesRegex(AgentError, "OPPOSITION_MISSING"):
            validate_output("portfolio_manager", body, self.artifacts, self.scope, "portfolio_manager", self.bundle)

    def test_final_cannot_decide_with_incomplete_risk(self):
        self.risk()
        body = decision_body("portfolio_manager", "portfolio_manager", self.artifacts, self.scope, self.bundle)
        self.bundle["risk_complete"] = False
        with self.assertRaisesRegex(AgentError, "DECISION_REQUIRES_ABSTENTION"):
            validate_output("portfolio_manager", body, self.artifacts, self.scope, "portfolio_manager", self.bundle)

    def test_horizon_and_rating_status_must_match(self):
        self.debate()
        body = decision_body("research_manager", "research_manager", self.artifacts, self.scope, self.bundle)
        body["horizon"] = "另一个期限"
        with self.assertRaisesRegex(AgentError, "HORIZON_MISMATCH"):
            validate_output("research_manager", body, self.artifacts, self.scope, "research_manager", self.bundle)
        body["decision_status"] = "abstained"
        with self.assertRaises(ValidationError):
            validate_output("research_manager", body, self.artifacts, self.scope, "research_manager", self.bundle)

    def test_discussion_optional_horizon_is_frozen(self):
        body = decision_body("bull", "bull_1", self.artifacts, self.scope, self.bundle)
        body["horizon"] = self.scope.decision_horizon
        self.assertTrue(validate_output("bull", body, self.artifacts, self.scope, "bull_1", self.bundle))
        body["horizon"] = "另一期限"
        with self.assertRaisesRegex(AgentError, "HORIZON_MISMATCH"):
            validate_output("bull", body, self.artifacts, self.scope, "bull_1", self.bundle)

    def test_abstained_narrative_cannot_recommend_buy(self):
        self.debate()
        self.bundle["gate_status"] = "limited"
        body = decision_body("research_manager", "research_manager", self.artifacts, self.scope, self.bundle)
        body["reason"] = "建议立即买入"
        with self.assertRaisesRegex(AgentError, "NARRATIVE_DECISION_CONFLICT"):
            validate_output("research_manager", body, self.artifacts, self.scope, "research_manager", self.bundle)

    def test_execution_assurance_is_not_a_research_fact(self):
        self.debate()
        body = decision_body("research_manager", "research_manager", self.artifacts, self.scope, self.bundle)
        body["reason"] = "已确认本方案可以执行"
        with self.assertRaisesRegex(AgentError, "EXECUTION_FEASIBILITY_UNKNOWN"):
            validate_output("research_manager", body, self.artifacts, self.scope, "research_manager", self.bundle)


if __name__ == "__main__":
    unittest.main()
