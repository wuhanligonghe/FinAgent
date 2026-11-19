"""Business scoring, experiment controls and source grounding regressions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from unittest.mock import patch

from finagent.data.evidence import EvidenceStore
from finagent.dialogue_evaluation import evaluation_clock
from finagent.errors import AgentError
from finagent.experiments.fixtures import digest, load_suite, public_pack, seed_fixture, truth_index, validate_suite
from finagent.experiments.reporting import aggregate, paired_comparison, resume_eligibility
from finagent.experiments.retrieval import retrieve
from finagent.experiments.runner import ExperimentBudget, _agent_context, analyze, closed_world, prepare, run
from finagent.experiments.scoring import expected_value, grade
from finagent.harness.host import SessionHost
from finagent.experiments.legacy_library import KnowledgeBase
from finagent.models import ModelReply
from finagent.runtime import ExecutionContext

from .support import ServiceCase, ScriptedModel, call_reply


class BusinessExperimentsTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.suite = load_suite()
        self.case = self.suite["dialogues"][0]
        self.clock = self.suite["clock"]
        with evaluation_clock(datetime.fromisoformat(self.clock)):
            self.documents = seed_fixture(self.store, self.case["fixtures"])

    def answer(self, spec=None, value=None, citations=True):
        gold = self.case["turns"][0]["gold"]
        expected = spec or gold["claims"][0]
        amount, facts, unit = expected_value(expected, truth_index(self.documents))
        claim = {**expected, "value": str(amount if value is None else value), "unit": unit, "currency": "CNY", "citations": [
            {"document_id": f["document_id"], "locator": f["locator"]} for f in facts] if citations else []}
        return {"action": "answer", "text": "合成资料金额见所附表格。", "request": gold["request"], "claims": [claim]}

    def score(self, answer, **kwargs):
        return grade(json.dumps(answer, ensure_ascii=False), self.case["turns"][0]["gold"], self.documents, cutoff=self.clock, **kwargs)

    def test_dataset_groups_entities_and_queries_do_not_cross_splits(self):
        result = validate_suite(self.suite)
        self.assertEqual(result["splits"], {"development": 30, "holdout": 18})
        self.assertEqual(result["turns"], 69)
        changed = deepcopy(self.suite)
        changed["dialogues"][-1]["group_id"] = changed["dialogues"][0]["group_id"]
        with self.assertRaises(ValueError):
            validate_suite(changed)

    def test_growth_labels_have_percentage_units(self):
        for case in self.suite["dialogues"]:
            for turn in case["turns"]:
                if any(c["metric"].endswith("_yoy") for c in turn["gold"]["claims"]):
                    self.assertEqual(turn["gold"]["request"]["unit"], "%")

    def test_inconsistent_candidate_labels_are_rejected_before_model_calls(self):
        suite = deepcopy(self.suite)
        case = next(c for c in suite["dialogues"] if c["category"] == "growth")
        case["turns"][0]["gold"]["request"]["unit"] = "元"
        with self.assertRaisesRegex(ValueError, "百分比"):
            validate_suite(suite)

    def test_fixtures_round_trip_through_original_document_validation(self):
        self.assertEqual(len(self.documents), 3)
        self.assertTrue(all(len(d["facts"]) == 7 and not d["pending"] for d in self.documents))
        self.assertNotIn("gold", json.dumps(public_pack(self.documents)))

    def test_correct_answer_requires_correct_number_and_source(self):
        result = self.score(self.answer())
        self.assertTrue(result["task_success"])
        self.assertEqual(result["citation_supported"], 1)
        self.assertEqual(result["unsupported_numeric_claims"], 0)

    def test_real_citation_with_wrong_number_is_not_support(self):
        result = self.score(self.answer(value="123"))
        self.assertFalse(result["task_success"])
        self.assertEqual(result["citation_supported"], 0)
        self.assertEqual(result["unsupported_numeric_claims"], 1)

    def test_wrong_period_and_wrong_company_do_not_get_numeric_credit(self):
        answer = self.answer()
        answer["claims"][0]["period_end"] = "2023-12-31"
        self.assertFalse(self.score(answer)["task_success"])
        answer = self.answer()
        answer["claims"][0]["symbol"] = "300750.SZ"
        self.assertEqual(self.score(answer)["claim_correct"], 0)

    def test_missing_and_fabricated_citations_are_rejected(self):
        self.assertFalse(self.score(self.answer(citations=False))["task_success"])
        answer = self.answer()
        answer["claims"][0]["citations"][0]["document_id"] = "doc_fabricated"
        self.assertEqual(self.score(answer)["citation_supported"], 0)

    def test_agent_cannot_cite_unseen_blocks_but_inline_source_can(self):
        answer = self.answer()
        self.assertTrue(self.score(answer, accessible=None)["task_success"])
        self.assertFalse(self.score(answer, accessible=set())["task_success"])
        citation = answer["claims"][0]["citations"][0]
        shown = {(citation["document_id"], citation["locator"].split("/row:")[0])}
        self.assertTrue(self.score(answer, accessible=shown)["task_success"])

    def test_currency_scope_and_duplicate_claims_are_checked(self):
        answer = self.answer()
        answer["claims"][0]["currency"] = "USD"
        self.assertFalse(self.score(answer)["task_success"])
        answer = self.answer()
        answer["claims"][0]["scope"] = "parent"
        self.assertFalse(self.score(answer)["task_success"])
        answer = self.answer()
        answer["claims"].append(deepcopy(answer["claims"][0]))
        self.assertFalse(self.score(answer)["task_success"])

    def test_yoy_needs_both_period_sources_and_percent_units(self):
        spec = {**self.case["turns"][0]["gold"]["claims"][0], "metric": "revenue_yoy", "period_end": "2025-12-31"}
        answer = self.answer(spec=spec)
        request = {**self.case["turns"][0]["gold"]["request"], "periods": ["2025-12-31"], "metrics": ["revenue_yoy"], "unit": "%"}
        answer["request"] = request
        gold = {**self.case["turns"][0]["gold"], "request": request, "claims": [spec]}
        result = grade(json.dumps(answer), gold, self.documents, cutoff=self.clock)
        self.assertTrue(result["task_success"])
        answer["claims"][0]["citations"].pop()
        self.assertFalse(grade(json.dumps(answer), gold, self.documents, cutoff=self.clock)["task_success"])

    def test_future_source_and_unexpected_extra_claim_are_rejected(self):
        documents = deepcopy(self.documents)
        for doc in documents:
            doc["available_at"] = "2027-01-01T00:00:00+08:00"
        answer = self.answer()
        self.assertFalse(grade(json.dumps(answer), self.case["turns"][0]["gold"], documents, cutoff=self.clock)["task_success"])
        answer["claims"].append({**answer["claims"][0], "metric": "price"})
        self.assertFalse(self.score(answer)["task_success"])

    def test_format_failure_counts_missing_slots_and_values(self):
        result = grade("随便回答", self.case["turns"][0]["gold"], self.documents, cutoff=self.clock)
        self.assertFalse(result["task_success"])
        self.assertEqual(result["slot_total"], 8)
        self.assertEqual(result["claim_required"], 1)
        self.assertEqual(result["slot_correct"], 0)

    def test_undeclared_text_amount_is_not_silently_accepted(self):
        answer = self.answer()
        answer["text"] = "这个数值为123456789.12元。"
        result = self.score(answer)
        self.assertFalse(result["task_success"])
        self.assertEqual(result["unsupported_text_amounts"], 1)

    def test_grounded_context_amount_and_undeclared_amount_are_distinct(self):
        answer = self.answer()
        another = next(f for d in self.documents for f in d["facts"] if f["metric"] == "net_profit")
        answer["text"] = "另外原文的净利润为" + another["normalized_value"] + "元。"
        result = self.score(answer)
        self.assertTrue(result["task_success"])
        self.assertFalse(result["contract_complete"])
        self.assertEqual(result["undeclared_text_amounts"], 1)
        self.assertEqual(result["unsupported_text_amounts"], 0)

    def test_unavailable_answer_cannot_hide_an_invented_amount_in_text(self):
        answer = {"action": "unavailable", "text": "没有资料，但可能是999元。", "request": self.case["turns"][0]["gold"]["request"], "claims": []}
        gold = {**self.case["turns"][0]["gold"], "claims": [], "action": "unavailable"}
        result = grade(json.dumps(answer), gold, [], cutoff=self.clock)
        self.assertFalse(result["task_success"])

    def test_clarification_is_a_question_not_a_keyword_match(self):
        gold = {"action": "clarify", "request": None, "claims": [], "clarification_field": "profit_basis"}
        self.assertTrue(grade("请明确利润口径：归母净利润还是全部净利润？", gold, [], cutoff=self.clock, runtime_status="needs_clarification")["task_success"])
        self.assertFalse(grade("归母净利润是一个财务概念。", gold, [], cutoff=self.clock)["task_success"])

    def test_agent_context_preserves_output_contract_without_frame_capabilities(self):
        self.settings.harness.memory_enabled = self.settings.harness.auto_memory = False
        self.settings.harness.workspace = self.root
        host = SessionHost(self.settings, "ablation", model=ScriptedModel(ModelReply({"role": "assistant", "content": "测试"})), service=self.service)
        try:
            original = host.model_messages([], host.ctx)
            _agent_context(host, "agent")
            changed = host.model_messages([], host.ctx)
            self.assertNotIn("任务理解由你负责", changed[0]["content"])
            self.assertIn("金融数值必须来自受控查询", changed[0]["content"])
            self.assertNotIn("task_understanding", json.loads(changed[1]["content"]))
        finally:
            host.close()

    def test_preparation_calls_no_model_and_generates_pending_review(self):
        with patch("finagent.experiments.runner.CompatibleChatModel", side_effect=AssertionError("prepare must not connect")):
            report = run(self.settings, self.root / "prepared", case_ids=[self.case["id"]])
        self.assertEqual(report["budget"]["model_calls"], 0)
        self.assertEqual(report["completed_jobs"], 0)
        self.assertFalse(report["resume"]["eligible_for_heldout_effect_claim"])
        self.assertTrue((self.root / "prepared/review_answers.json").exists())

    def test_manifest_freezes_data_before_runs_and_rejects_overwrite(self):
        output = self.root / "manifest"
        manifest, _ = prepare(self.settings, output, case_ids=[self.case["id"]])
        copied = {k: v for k, v in manifest.items() if k != "manifest_hash"}
        self.assertEqual(manifest["manifest_hash"], digest(copied))
        self.assertEqual(manifest["dataset_hash"], digest(self.suite))
        self.assertEqual(manifest["implementation_archive"]["sha256"], manifest["implementation_digest"])
        self.assertTrue((output / "implementation/finagent/experiments/runner.py").exists())
        with self.assertRaises(ValueError):
            prepare(self.settings, output)

    def test_source_archive_cannot_recursively_include_its_output(self):
        import finagent.experiments.runner as runner
        path = Path(runner.__file__).resolve().parent / "invalid_experiment_output"
        with self.assertRaisesRegex(ValueError, "源码目录"):
            prepare(self.settings, path, case_ids=[self.case["id"]])
        self.assertFalse(path.exists())

    def test_review_cannot_be_claimed_without_matching_data_and_reviewer(self):
        path = self.root / "review.json"
        path.write_text(json.dumps({"dataset_hash": digest(self.suite), "cases": [{"case_id": self.case["id"], "case_hash": digest(self.case), "status": "approved"}]}))
        with self.assertRaises(ValueError):
            prepare(self.settings, self.root / "reviewed", case_ids=[self.case["id"]], review_file=path)

    def test_unreviewed_holdout_is_not_run_or_inspected(self):
        report = run(self.settings, self.root / "holdout_prepare", split="holdout")
        self.assertEqual(report["retrieval"]["status"], "not_run_before_review")
        with patch("finagent.experiments.runner.CompatibleChatModel", side_effect=AssertionError("must not connect")):
            with self.assertRaisesRegex(ValueError, "人工标签审核"):
                run(self.settings, self.root / "holdout_live", live=True, split="holdout")

    def test_rejected_label_cannot_be_used_for_paid_run(self):
        path = self.root / "rejected.json"
        path.write_text(json.dumps({"dataset_hash": digest(self.suite), "cases": [{"case_id": self.case["id"], "status": "rejected"}]}))
        with self.assertRaisesRegex(ValueError, "退回"):
            prepare(self.settings, self.root / "rejected_run", case_ids=[self.case["id"]], review_file=path)

    def test_research_is_disabled_even_if_called_from_experiment_code(self):
        from finagent.harness.research import ResearchService
        with closed_world("keyword"):
            with self.assertRaisesRegex(AgentError, "禁止新增"):
                ResearchService(self.settings).start("deep_research", "600519.SH", "不要真的运行")
            with self.assertRaises(AgentError):
                ResearchService(self.settings).resume("old")

    def test_global_call_and_post_response_token_caps_are_shared(self):
        class Fake:
            calls = 0
            def complete(self, *args):
                self.calls += 1
                return ModelReply({"role": "assistant", "content": "测试"}, usage={"prompt_tokens": 8, "completion_tokens": 2})
        actual = Fake()
        budget = ExperimentBudget(10, 10, self.root / "calls.jsonl")
        a, b = budget.wrap(actual, "a"), budget.wrap(actual, "b")
        a.complete([], [], 1)
        with self.assertRaises(AgentError):
            b.complete([], [], 1)
        self.assertEqual(actual.calls, 1)
        captured = json.loads((self.root / "requests/call_00001.json").read_text())
        self.assertEqual(captured["messages"], [])

    def test_paired_bootstrap_groups_repeats_instead_of_counting_turns(self):
        rows = [{"case_id": "a", "repeat": r, "arm": arm, "dialogue_success": arm == "agent_full"} for r in range(3) for arm in ("direct", "agent_full")]
        result = paired_comparison(rows, "direct", "agent_full")
        self.assertEqual(result["paired_dialogues"], 1)
        self.assertEqual(result["paired_runs"], 3)
        self.assertEqual(result["delta_percentage_points"], 100)
        self.assertIsNone(result["ci95_percentage_points"])

    def test_failing_jobs_stay_in_success_denominator(self):
        gold = self.case["turns"][0]["gold"]
        g = grade("invalid", gold, self.documents, cutoff=self.clock)
        row = {"case_id": "a", "arm": "direct", "repeat": 0, "dialogue_success": False,
               "turns": [{"grade": g, "runtime_status": "failed", "usage": {}, "elapsed_seconds": 1, "model_calls": 1, "tool_calls": 0}]}
        result = aggregate([row])["direct"]
        self.assertEqual(result["dialogue_success_rate"], 0)
        self.assertEqual(result["numeric_recall"], 0)
        self.assertEqual(result["slot_accuracy"], 0)

    def test_fake_models_cannot_generate_live_resume_claims_or_leak_case_labels(self):
        seen, toolsets = [], []
        answer = json.dumps(self.answer(), ensure_ascii=False)
        def factory(job):
            class Model:
                def complete(self, messages, tools, timeout):
                    seen.append(json.dumps(messages, ensure_ascii=False))
                    toolsets.append((job["arm"], {t["function"]["name"] for t in tools}))
                    return ModelReply({"role": "assistant", "content": answer}, usage={"prompt_tokens": 5, "completion_tokens": 5})
            return Model()
        report = run(self.settings, self.root / "fake_run", live=True, case_ids=[self.case["id"]], arms=["direct", "agent"], model_factory=factory)
        self.assertEqual(report["mode"], "test_fixture")
        self.assertFalse(report["resume"]["eligible_for_heldout_effect_claim"])
        self.assertTrue(all(self.case["id"] not in messages and '"gold"' not in messages for messages in seen))
        for arm, names in toolsets:
            self.assertNotIn("update_task_frame", names)
            self.assertNotIn("read_task_frames", names)
            self.assertNotIn("start_deep_research", names)
            self.assertNotIn("task", names)

    def test_offline_reanalysis_reuses_frozen_runs_without_model_calls(self):
        answer = json.dumps(self.answer())
        def factory(job):
            return ScriptedModel(ModelReply({"role": "assistant", "content": answer}, usage={"prompt_tokens": 5, "completion_tokens": 5}))
        source = self.root / "source_run"
        run(self.settings, source, live=True, case_ids=[self.case["id"]], arms=["direct"], model_factory=factory)
        before = (source / "runs.jsonl").read_bytes()
        with patch("finagent.experiments.runner.CompatibleChatModel", side_effect=AssertionError("analysis must not connect")):
            report = analyze(source, self.root / "reanalysis")
        self.assertEqual(report["metrics"]["direct"]["dialogue_success_rate"], 1)
        self.assertEqual((source / "runs.jsonl").read_bytes(), before)
        audit = json.loads((self.root / "reanalysis/analysis_manifest.json").read_text())
        self.assertEqual(audit["additional_model_calls"], 0)

    def test_reanalysis_rejects_modified_frozen_dataset(self):
        answer = json.dumps(self.answer())
        source = self.root / "tampered_run"
        run(self.settings, source, live=True, case_ids=[self.case["id"]], arms=["direct"],
            model_factory=lambda job: ScriptedModel(ModelReply({"role": "assistant", "content": answer})))
        snapshot = json.loads((source / "dataset_snapshot.json").read_text())
        snapshot["version"] = "tampered"
        (source / "dataset_snapshot.json").write_text(json.dumps(snapshot))
        with self.assertRaisesRegex(ValueError, "摘要不匹配"):
            analyze(source, self.root / "bad_analysis")

    def test_reanalysis_can_run_from_another_working_directory(self):
        answer = json.dumps(self.answer())
        source = self.root / "portable_run"
        run(self.settings, source, live=True, case_ids=[self.case["id"]], arms=["direct"],
            model_factory=lambda job: ScriptedModel(ModelReply({"role": "assistant", "content": answer})))
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            report = analyze(source, self.root / "portable_analysis")
        finally:
            os.chdir(previous)
        self.assertEqual(report["metrics"]["direct"]["dialogue_success_rate"], 1)

    def test_both_retrievers_keep_company_period_and_scope_filters(self):
        library = KnowledgeBase(self.store)
        for method in ("keyword", "bm25_expansion"):
            ctx = ExecutionContext()
            ctx.bind_symbol("600519.SH")
            matches = retrieve(library, "营业收入", ctx, "2024-12-31", strategy=method).data["matches"]
            self.assertTrue(matches)
            self.assertTrue(all(library.load(m["document_id"])["symbol"] == "600519.SH" and library.load(m["document_id"])["report_period"] == "2024-12-31" for m in matches))
