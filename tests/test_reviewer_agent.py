"""Reviewer isolation, provenance, bounded retrieval and repair; synthetic fixtures."""
from copy import deepcopy
from datetime import datetime
import json
from threading import Event
import unittest
from unittest.mock import Mock

import httpx
import jsonschema

from finagent.answer_review import QwenAnswerReviewer, build_evidence_packet
from finagent.config import AnswerReviewSettings, Limits
from finagent.errors import AgentError
from finagent.harness.kernel import Ledger
from finagent.harness.review_evidence import ReviewEvidenceCatalog
from finagent.harness.review_memory import ReviewEvidenceMemory
from finagent.harness.reviewer import ReviewBudget, ReviewerRuntime, RUBRIC
from tests.support import ScriptedModel, ServiceCase, call_reply


def entry(key="result_a", body=None, **overrides):
    return {"evidence_id": key, "tool": "get_quote", "symbol": "600519.SH", "source": "synthetic",
            "source_url": "https://example.invalid/source", "data_time": "2026-11-05T12:00:00+08:00",
            "fetched_at": "2026-11-05T12:01:00+08:00", "currency": "CNY", "units": {"price": "元"},
            "price_basis": "unadjusted", "status": "partial", "warnings": ["最终性未知"],
            "provenance": {"turn_id": "current"}, "snapshots": [{"snapshot_id": "raw_a", "metadata": {"version": "v1"}}],
            "body": body if body is not None else {"price": "120", "unit": "元"}, **overrides}


def packet(*entries):
    return {"complete": True, "evidence": list(entries), "excluded": [], "incomplete": [],
            "user_request": "分析合成公司", "execution_context": {"symbol": "600519.SH"},
            "request_context": {"recent_user_requests": [{"text": "只看已有资料"}],
                                "user_constraints": [{"text": "单位为元"}],
                                "todos": [{"content": "模型声称已核验"}],
                                "conversation_summary": "主模型断言无需审核"}}


def decision(verdict="pass", *, citations=None, issues=None, evidence_required=False):
    return {"complete": True, "verdict": verdict, "evidence_required": evidence_required,
            "summary": "合成审核", "citations": citations or [], "issues": issues or [],
            "checks": [{"dimension": key, "status": "pass" if verdict == "pass" else "unknown",
                        "reason": "已对照资料"} for key in RUBRIC]}


def submission(value, identifier="submit"):
    return call_reply((identifier, "submit_review", value))


class CatalogTests(unittest.TestCase):
    def test_dedup_preserves_aliases_and_individual_observations(self):
        a = entry()
        b = entry("result_b", fetched_at="2026-11-05T12:02:00+08:00",
                  provenance={"turn_id": "later"}, snapshots=[{"snapshot_id": "raw_b", "metadata": {"version": "v1"}}])
        catalog = ReviewEvidenceCatalog(packet(a, b))
        self.assertEqual(len(catalog.entries), 1)
        self.assertEqual(catalog.aliases["result_b"], "result_a")
        self.assertEqual(len(catalog.manifest()[0]["observations"]), 2)
        self.assertEqual(json.loads(catalog.read("result_b", "/fetched_at")["text"]), b["fetched_at"])
        self.assertEqual(json.loads(catalog.read("result_b", "/body/price")["text"]), "120")
        self.assertNotIn("aliases", a)

    def test_different_period_unit_basis_source_version_and_values_stay_separate(self):
        changes = [{"body": {"price": "121"}}, {"units": {"price": "美元"}},
                   {"price_basis": "adjusted"}, {"data_time": "2026-11-04T12:00:00+08:00"},
                   {"source": "other"}, {"snapshots": [{"snapshot_id": "raw_b", "metadata": {"version": "v2"}}]}]
        for change in changes:
            with self.subTest(change=change):
                catalog = ReviewEvidenceCatalog(packet(entry(), entry("result_b", **change)))
                self.assertEqual(len(catalog.entries), 2)
                self.assertEqual(catalog.manifest()[0]["related_evidence_ids"], ["result_b"])

    def test_projection_is_exact_and_large_arrays_remain_readable(self):
        body = {"kind": "market_series", "bars": [{"date": f"day_{i}", "close": i} for i in range(500)],
                "complete": False, "finality": "unknown"}
        catalog = ReviewEvidenceCatalog(packet(entry(body=body)))
        view = catalog.initial_view(16000)[0]
        self.assertFalse(view["full_body_loaded"])
        self.assertLess(len(json.dumps(view)), 3000)
        self.assertEqual(catalog.manifest()[0]["collections"]["/body/bars"], 500)
        self.assertEqual(json.loads(catalog.read("result_a", "/body/bars/300")["text"]), body["bars"][300])

    def test_search_offsets_and_json_pointer_escaping(self):
        body = {"a/b": {"~name": "前文🙂目标后文"}}
        catalog = ReviewEvidenceCatalog(packet(entry(body=body)))
        result = catalog.search("result_a", "目标", "/body/a~1b/~0name")
        hit = result["hits"][0]
        read = catalog.read("result_a", result["path"], hit["snippet_offset"], len(hit["text"]))
        self.assertEqual(read["text"], hit["text"])
        for key, path in (("result_other_user", "/body"), ("result_a", "/body/missing"),
                          ("result_a", "../../.env")):
            with self.assertRaises(AgentError):
                catalog.read(key, path)

    def test_search_multiple_keywords_is_not_order_sensitive(self):
        catalog = ReviewEvidenceCatalog(packet(entry(body={"text": "合成公司2024年经营现金流为120元。"})))
        result = catalog.search("result_a", "经营现金流 2024")
        self.assertTrue(result["hits"])
        self.assertIn("120元", result["hits"][0]["text"])
        self.assertEqual(catalog.search("result_a", "经营现金流 不存在")["hits"], [])

    def test_alias_specific_metadata_cannot_be_cited_under_wrong_id(self):
        catalog = ReviewEvidenceCatalog(packet(entry(), entry("result_b", fetched_at="2026-11-05T12:02:00+08:00")))
        self.assertFalse(catalog.contains_quote("result_a", "2026-11-05T12:02:00+08:00"))
        self.assertTrue(catalog.contains_quote("result_b", "2026-11-05T12:02:00+08:00"))
        self.assertTrue(catalog.search("result_b", "12:02", "/fetched_at")["hits"])


class EvidenceMemoryTests(unittest.TestCase):
    def test_disjoint_ranges_do_not_hide_gap_or_make_cross_gap_quote(self):
        memory = ReviewEvidenceMemory()
        memory.add("a", "/body/text", 0, '"ABC', 12)
        memory.add("a", "/body/text", 8, 'XYZ"', 12)
        self.assertFalse(memory.complete("a", "/body/text"))
        view = memory.view()[0]
        self.assertEqual(view["next_offset"], 4)
        self.assertEqual(len(view["segments"]), 2)
        self.assertFalse(any("ABCXYZ" in part for part in memory.fragments({"a"})))
        memory.add("b", "/body/text", 4, "DEF", 12)
        self.assertFalse(memory.complete("a", "/body/text"))

    def test_parent_value_subsumes_child_without_losing_json_pointer_support(self):
        memory = ReviewEvidenceMemory()
        memory.add_value("a", "/body/a~1b", {"~name": "值"})
        memory.add_value("a", "/body", {"a/b": {"~name": "值"}})
        self.assertEqual(len(memory.view()), 1)
        self.assertTrue(memory.complete("a", "/body/a~1b/~0name"))
        self.assertEqual(memory.add_value("a", "/body/a~1b/~0name", "值"), 0)

    def test_overlapping_fragments_must_agree(self):
        memory = ReviewEvidenceMemory()
        memory.add("a", "/body", 0, '"ABC', 8)
        with self.assertRaises(ValueError):
            memory.add("a", "/body", 2, "XX", 8)


class ReviewerRuntimeTests(unittest.TestCase):
    def runtime(self, evidence=None, **kwargs):
        return ReviewerRuntime(AnswerReviewSettings(agentic=True), "合成公司价格120元。",
                               evidence or packet(entry()), **kwargs)

    def test_kernel_completes_isolated_structured_review(self):
        runtime = self.runtime()
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a", "quote": '"price": "120"'}])
        model = ScriptedModel(submission(value))
        result = runtime.run(model)
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(runtime.budget.model_calls, 1)
        payload = json.loads(model.messages_seen[0][1]["content"])
        self.assertNotIn("todos", payload["user_requirements"])
        self.assertNotIn("conversation_summary", payload["user_requirements"])
        self.assertNotIn("主模型断言", json.dumps(model.messages_seen[0], ensure_ascii=False))
        self.assertEqual(set(payload["rubric"]), set(RUBRIC))
        self.assertTrue(payload["evidence_catalog"][0]["full_body_loaded"])
        self.assertTrue(runtime.audit["events"])

    def test_read_then_submit_and_shared_ledger_accounting(self):
        evidence = packet(entry(body={"hidden": "X" * 10000}))
        ledger = Ledger(Limits(model_calls=10, tool_calls=20))
        runtime = self.runtime(evidence, shared_ledger=ledger)
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a", "quote": "X" * 100}])
        model = ScriptedModel(call_reply(("read", "read_review_evidence", {
            "evidence_id": "result_a", "path": "/body/hidden", "offset": 2000, "limit": 100})), submission(value))
        self.assertEqual(runtime.run(model)["verdict"], "pass")
        self.assertEqual(ledger.counts["model"], 2)
        self.assertEqual(ledger.counts["tool"], 2)
        self.assertEqual(runtime.audit["loaded_ranges"][-1]["offset"], 2000)

    def test_unread_content_cannot_be_cited_even_when_it_exists(self):
        runtime = self.runtime(packet(entry(body={"hidden": "X" * 10000})))
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a", "quote": "X" * 20}])
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_INVALID"):
            runtime.run(ScriptedModel(submission(value, "s1"), submission(value, "s2")))
        self.assertIsNone(runtime.decision)

    def test_failed_read_or_forbidden_tool_prevents_pass(self):
        for name, args in (("read_review_evidence", {"evidence_id": "other_user"}), ("bash", {"command": "cat .env"})):
            with self.subTest(name=name):
                runtime = self.runtime()
                with self.assertRaises(AgentError):
                    runtime.run(ScriptedModel(call_reply(("bad", name, args)),
                        submission(decision(), "s1"), submission(decision(), "s2")))
                self.assertIsNone(runtime.decision)

    def test_retrieval_cache_avoids_repeated_read_volume(self):
        budget = ReviewBudget()
        args = {"evidence_id": "result_a", "path": "/body/hidden", "offset": 2000, "limit": 100}
        runtime = self.runtime(packet(entry(body={"hidden": "X" * 10000})), budget=budget)
        result = runtime.run(ScriptedModel(call_reply(("r1", "read_review_evidence", args)),
            call_reply(("r2", "read_review_evidence", args)), submission(decision())))
        self.assertEqual(result["verdict"], "pass")
        tool_results = [e["payload"]["result"] for e in runtime.trace if e["kind"] == "tool_result"]
        self.assertTrue(tool_results[1]["cache_hit"])
        self.assertLess(budget.read_chars, 1000)

    def test_budget_is_shared_across_separate_repair_reviews(self):
        config = AnswerReviewSettings(agentic=True, max_model_calls=1)
        budget = ReviewBudget()
        first = ReviewerRuntime(config, "你好", packet(), budget=budget)
        self.assertEqual(first.run(ScriptedModel(submission(decision())))["verdict"], "pass")
        second = ReviewerRuntime(config, "修正版", packet(), budget=budget)
        model = ScriptedModel(submission(decision()))
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_BUDGET"):
            second.run(model)
        self.assertEqual(model.index, 0)

    def test_cancellation_parent_budget_and_read_budget_never_release(self):
        cancel = Event(); cancel.set()
        runtime = self.runtime(cancelled=cancel)
        with self.assertRaisesRegex(AgentError, "CANCELLED"):
            runtime.run(ScriptedModel(submission(decision())))
        shared = Ledger(Limits(model_calls=1)); shared.charge("model")
        with self.assertRaisesRegex(AgentError, "MODEL_BUDGET"):
            self.runtime(shared_ledger=shared).run(ScriptedModel(submission(decision())))
        config = AnswerReviewSettings(agentic=True, max_read_chars=1000)
        runtime = ReviewerRuntime(config, "草稿", packet(entry(body={"x": "X" * 10000})))
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_BUDGET"):
            runtime.run(ScriptedModel(call_reply(("r", "read_review_evidence", {
                "evidence_id": "result_a", "path": "/body/x", "limit": 2000}))))

    def test_missing_rubric_and_unknown_dimension_cannot_pass(self):
        for mutate in (lambda d: d["checks"].pop(), lambda d: d["checks"][0].update(status="unknown")):
            value = decision(); mutate(value)
            runtime = self.runtime()
            with self.assertRaises(AgentError):
                runtime.run(ScriptedModel(submission(value, "s1"), submission(value, "s2"), submission(value, "s3")))
            self.assertIsNone(runtime.decision)

    def test_prefix_read_is_not_proof_of_absence_and_prompts_search(self):
        runtime = self.runtime(packet(entry(body={"text": "背景" * 6000 + "目标120元"})))
        value = decision("revise", issues=[{"answer_quote": "合成公司价格120元。", "kind": "unsupported",
            "reason": "未找到", "suggestion": "说明未知", "citations": []}])
        offered = []
        class ObservedModel(ScriptedModel):
            def complete(self, messages, tools, timeout):
                offered.append({tool["function"]["name"] for tool in tools})
                return super().complete(messages, tools, timeout)
        model = ObservedModel(call_reply(("r", "read_review_evidence", {"evidence_id": "result_a", "limit": 100})),
            submission(value, "s1"), call_reply(("q", "search_review_evidence", {"evidence_id": "result_a", "query": "目标"})),
            submission(value, "s2"))
        self.assertEqual(runtime.run(model)["verdict"], "revise")
        results = [e["payload"]["result"] for e in runtime.trace if e["kind"] == "tool_result"]
        self.assertEqual(results[1]["error"], "REVIEW_NEEDS_EVIDENCE")
        self.assertEqual(results[1]["required_reads"], [{"evidence_id": "result_a", "path": "/body",
                                                       "action": "search_review_evidence"}])
        self.assertEqual(["submit_review" in names for names in offered], [True, True, False, True])
        self.assertIn("120元", results[2]["hits"][0]["text"])

    def test_cache_is_not_reused_after_selected_source_changes(self):
        budget = ReviewBudget()
        args = {"evidence_id": "result_a", "path": "/body/price"}
        first = self.runtime(budget=budget)
        first.run(ScriptedModel(call_reply(("r1", "read_review_evidence", args)), submission(decision())))
        second = self.runtime(packet(entry(body={"price": "999"})), budget=budget)
        second.run(ScriptedModel(call_reply(("r2", "read_review_evidence", args)), submission(decision())))
        result = next(e["payload"]["result"] for e in second.trace if e["kind"] == "tool_result")
        self.assertEqual(json.loads(result["text"]), "999")
        self.assertNotIn("cache_hit", result)

    def test_unable_to_verify_may_explicitly_report_incomplete_review(self):
        value = decision("unable_to_verify"); value["complete"] = False
        runtime = self.runtime(packet())
        result = runtime.run(ScriptedModel(submission(value)))
        self.assertEqual(result["verdict"], "unable_to_verify")
        self.assertFalse(result["complete"])

    def test_merged_ranges_support_cross_page_citation_without_replaying_history(self):
        text = "".join(f"编号{i:04d}；" for i in range(1500))
        runtime = self.runtime(packet(entry(body={"text": text})))
        serialized = json.dumps(text, ensure_ascii=False)
        quote = serialized[4075:4200]
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a", "quote": quote}])
        model = ScriptedModel(call_reply(("r1", "read_review_evidence", {
            "evidence_id": "result_a", "path": "/body/text", "offset": 4000, "limit": 150})),
            call_reply(("r2", "read_review_evidence", {
                "evidence_id": "result_a", "path": "/body/text", "offset": 4100, "limit": 150})),
            submission(value))
        self.assertEqual(runtime.run(model)["verdict"], "pass")
        latest = json.loads(model.messages_seen[-1][1]["content"])
        segments = next(v for v in latest["evidence_memory"] if v["path"] == "/body/text")["segments"]
        self.assertEqual(segments, [{"offset": 4000, "text": serialized[4000:4250]}])
        self.assertEqual([len(m) for m in model.messages_seen], [2, 2, 2])
        self.assertNotIn("citation_options", latest)
        self.assertNotIn("answer_quote_options", latest)
        self.assertEqual(sum(e["kind"] == "message" for e in runtime.trace), 6)

    def test_long_quotes_are_not_repeated_in_schema(self):
        phrase = "唯一的证据原文标记" * 15
        runtime = self.runtime(packet(entry(body={"text": phrase})))
        wire_schema = json.dumps(runtime.schemas(None), ensure_ascii=False)
        prompt = runtime.model_messages([], None)[1]["content"]
        self.assertNotIn(phrase, wire_schema)
        self.assertEqual(prompt.count(phrase), 1)

    def test_json_string_quote_is_decoded_only_when_exact_loaded_source_matches(self):
        runtime = self.runtime(packet(entry(body={"text": "完整原文中有这段内容，并有后文。"})))
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a",
            "quote": json.dumps("有这段内容", ensure_ascii=False)}])
        result = runtime.run(ScriptedModel(submission(value)))
        self.assertEqual(result["citations"][0]["quote"], "有这段内容")
        missing = self.runtime(packet(entry(body={"hidden": "X" * 10000 + "未读内容"})))
        value["citations"][0]["quote"] = json.dumps("未读内容", ensure_ascii=False)
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_INVALID"):
            missing.run(ScriptedModel(submission(value, "s1"), submission(value, "s2")))

    def test_spliced_quote_is_rejected_with_only_exact_loaded_repair_hints(self):
        runtime = self.runtime(packet(entry(body={"formula": "(120-100)/100*100", "value": "20"})))
        quote = '"formula": "(120-100)/100*100", ... "value": "20"'
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a", "quote": quote}])
        with self.assertRaisesRegex(ValueError, "不能拼接") as error:
            runtime.submit(value)
        self.assertIn('(120-100)/100*100', str(error.exception))
        self.assertIsNone(runtime.decision)

    def test_field_reference_extracts_exact_numeric_fields_from_original_failure(self):
        runtime = self.runtime(packet(entry(body={"current_price": 393.0, "previous_close": 408.97})))
        value = decision(evidence_required=True, citations=[
            {"evidence_id": "result_a", "path": "/body/current_price"},
            {"evidence_id": "result_a", "path": "/body/previous_close"}])
        result = runtime.run(ScriptedModel(submission(value)))
        self.assertEqual([c["quote"] for c in result["citations"]],
                         ['"current_price": 393.0', '"previous_close": 408.97'])
        self.assertEqual(result["citation_locations"][0]["path"], "/body/current_price")
        self.assertEqual(runtime.audit["resolved_citations"], result["citation_locations"])

    def test_model_schema_offers_locations_and_rejects_freehand_quotes(self):
        runtime = self.runtime()
        schema = next(t["function"]["parameters"] for t in runtime.schemas(None)
                      if t["function"]["name"] == "submit_review")
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a", "path": "/body/price"}])
        jsonschema.validate(value, schema)
        value["citations"][0] = {"evidence_id": "result_a", "quote": "price: 120"}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(value, schema)

    def test_wire_paths_are_paired_with_source_and_cannot_use_guessed_array_indexes(self):
        runtime = self.runtime(packet(entry(body={"signals": [{"status": "candidate"}]}),
                                     entry("result_b", body={"different_field": 123})))
        schema = next(t["function"]["parameters"] for t in runtime.schemas(None)
                      if t["function"]["name"] == "submit_review")
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a", "path": "/body/signals/0/status"}])
        jsonschema.validate(value, schema)
        for key, path in [("result_a", "/body/signals/-1/status"), ("result_a", "/body/signals/1"),
                          ("result_a", "/body/different_field"), ("result_b", "/body/signals/0/status")]:
            value["citations"][0] = {"evidence_id": key, "path": path}
            with self.subTest(key=key, path=path), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(value, schema)

    def test_unread_field_is_not_in_wire_choices_but_appears_after_reading(self):
        runtime = self.runtime(packet(entry(body={"hidden": "X" * 10000})))
        self.assertNotIn("/body/hidden", json.dumps(runtime.schemas(None), ensure_ascii=False))
        runtime.remember_result("read_review_evidence", runtime.catalog.read("result_a", "/body/hidden", 100, 20))
        self.assertIn("/body/hidden", json.dumps(runtime.schemas(None), ensure_ascii=False))

    def test_unread_reference_requires_its_exact_range_before_resubmission(self):
        runtime = self.runtime(packet(entry(body={"hidden": "X" * 10000, "other": "Y" * 10000})))
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a",
            "path": "/body/hidden", "offset": 3500, "length": 20}])
        offered = []
        class ObservedModel(ScriptedModel):
            def complete(self, messages, tools, timeout):
                offered.append({tool["function"]["name"] for tool in tools})
                return super().complete(messages, tools, timeout)
        model = ObservedModel(submission(value, "s1"),
            call_reply(("wrong", "read_review_evidence", {"evidence_id": "result_a", "path": "/body/other", "limit": 100})),
            call_reply(("right", "read_review_evidence", {"evidence_id": "result_a", "path": "/body/hidden",
                                                       "offset": 3500, "limit": 20})), submission(value, "s2"))
        result = runtime.run(model)
        self.assertEqual(result["citations"][0]["quote"], "X" * 20)
        self.assertEqual(["submit_review" in names for names in offered], [True, False, False, True])
        self.assertEqual(runtime.invalid_submissions, 0)

    def test_reference_cannot_cross_unread_gap_or_escape_its_source(self):
        runtime = self.runtime(packet(entry(body={"hidden": "X" * 10000})))
        serialized = json.dumps("X" * 10000)
        runtime.memory.add("result_a", "/body/hidden", 0, serialized[:100], len(serialized))
        runtime.memory.add("result_a", "/body/hidden", 200, serialized[200:300], len(serialized))
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a",
            "path": "/body/hidden", "offset": 50, "length": 200}])
        with self.assertRaisesRegex(AgentError, "REVIEW_NEEDS_EVIDENCE"):
            runtime.submit(value)
        value["citations"][0] = {"evidence_id": "another_user", "path": "/body"}
        with self.assertRaises(ValueError):
            runtime.submit(value)
        self.assertIsNone(runtime.decision)

    def test_nested_escaped_paths_and_issue_references_are_resolved(self):
        runtime = self.runtime(packet(entry(body={"signals": [{"status": "candidate"}],
                                                  "a/b": {"~name": "原文"}})))
        value = decision("revise", citations=[{"evidence_id": "result_a", "path": "/body/a~1b/~0name"}],
            issues=[{"answer_quote": "合成公司价格120元。", "kind": "contradicted", "reason": "合成问题",
                     "suggestion": "修正", "citations": [{"evidence_id": "result_a", "path": "/body/signals/0/status"}]}])
        result = runtime.run(ScriptedModel(submission(value)))
        self.assertEqual(result["issues"][0]["citations"][0]["quote"], '"status": "candidate"')
        self.assertEqual(result["citation_locations"][1]["issue_index"], 0)

    def test_deduplicated_body_reference_does_not_share_alias_metadata(self):
        runtime = self.runtime(packet(entry(), entry("result_b", fetched_at="2026-11-05T12:02:00+08:00")))
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_b", "path": "/body/price"}])
        self.assertEqual(runtime.run(ScriptedModel(submission(value)))["citations"][0]["quote"], '"price": "120"')
        runtime = self.runtime(packet(entry(), entry("result_b", fetched_at="2026-11-05T12:02:00+08:00")))
        value["citations"][0]["path"] = "/fetched_at"
        with self.assertRaisesRegex(AgentError, "REVIEW_NEEDS_EVIDENCE"):
            runtime.submit(value)

    def test_field_reference_cannot_restore_text_hidden_by_security_guard(self):
        guard = Mock()
        guard.redact.return_value = ("MASKED", [])
        runtime = self.runtime(packet(entry(body={"private": "敏感内容"})), guard=guard)
        value = decision(evidence_required=True, citations=[{"evidence_id": "result_a", "path": "/body/private"}])
        with self.assertRaisesRegex(ValueError, "不能回传的敏感内容") as error:
            runtime.submit(value)
        self.assertNotIn('"private":', str(error.exception))
        self.assertIsNone(runtime.decision)

    def test_repeated_reads_stop_early_instead_of_spending_all_model_calls(self):
        runtime = self.runtime(packet(entry(body={"hidden": "X" * 10000})))
        args = {"evidence_id": "result_a", "path": "/body/hidden", "offset": 2000, "limit": 100}
        model = ScriptedModel(*(call_reply((f"r{i}", "read_review_evidence", args)) for i in range(4)))
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_NO_PROGRESS"):
            runtime.run(model)
        self.assertEqual(model.index, 3)
        self.assertEqual(runtime.budget.retrieval_calls, 3)
        self.assertIsNone(runtime.decision)

    def test_retrieval_limit_counts_different_empty_searches(self):
        runtime = ReviewerRuntime(AnswerReviewSettings(agentic=True, max_retrieval_calls=2),
                                  "草稿", packet(entry()))
        model = ScriptedModel(*(call_reply((f"q{i}", "search_review_evidence", {
            "evidence_id": "result_a", "query": f"不存在{i}"})) for i in range(3)))
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_BUDGET"):
            runtime.run(model)
        self.assertEqual(runtime.budget.retrieval_calls, 2)
        self.assertIsNone(runtime.decision)

    def test_retrieval_limit_is_shared_across_main_model_repairs(self):
        config = AnswerReviewSettings(agentic=True, max_retrieval_calls=1)
        budget = ReviewBudget()
        evidence = packet(entry(body={"hidden": "X" * 10000}))
        args = {"evidence_id": "result_a", "path": "/body/hidden", "offset": 2000, "limit": 100}
        first = ReviewerRuntime(config, "草稿", evidence, budget=budget)
        first.run(ScriptedModel(call_reply(("r1", "read_review_evidence", args)), submission(decision())))
        second = ReviewerRuntime(config, "修正版", evidence, budget=budget)
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_BUDGET"):
            second.run(ScriptedModel(call_reply(("r2", "read_review_evidence", args))))
        self.assertEqual(budget.retrieval_calls, 1)

    def test_adjacent_pages_finish_coverage_without_forcing_search(self):
        runtime = self.runtime(packet(entry(body={"text": "文" * 9000})))
        value = decision("revise", issues=[{"answer_quote": "合成公司价格120元。", "kind": "unsupported",
            "reason": "全文无数值支持", "suggestion": "删除数值断言", "citations": []}])
        reads = [call_reply((f"r{i}", "read_review_evidence", {
            "evidence_id": "result_a", "path": "/body/text", "offset": offset, "limit": size}))
            for i, (offset, size) in enumerate([(0, 3500), (3500, 3500), (7000, 2500)])]
        self.assertEqual(runtime.run(ScriptedModel(*reads, submission(value)))["verdict"], "revise")
        self.assertTrue(runtime.memory.complete("result_a", "/body/text"))
        self.assertFalse(runtime.incomplete_reads)

    def test_ollama_action_protocol_uses_no_external_tools_or_main_history(self):
        requests = []
        def respond(request):
            body = json.loads(request.content); requests.append(body)
            action = {"action": "submit_review", "arguments": decision()}
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(action)}, "eval_count": 20})
        config = AnswerReviewSettings(agentic=True)
        audit = {}
        result = QwenAnswerReviewer(config, transport=httpx.MockTransport(respond)).review("你好", packet(), audit=audit)
        self.assertEqual(result["engine"], "evidence_reviewer_agent_v2")
        actions = {c["properties"]["action"]["enum"][0] for c in requests[0]["format"]["oneOf"]}
        self.assertEqual(actions, {"list_review_evidence", "read_review_evidence", "search_review_evidence", "submit_review"})
        self.assertEqual(audit["status"], "pass")


class AgenticPacketTests(ServiceCase):
    def test_large_valid_evidence_uses_catalog_instead_of_becoming_incomplete(self):
        from finagent.data.contracts import Observation
        from finagent.runtime import ExecutionContext, TZ
        ctx = ExecutionContext(); ctx.as_of = datetime(2026, 11, 5, 12, tzinfo=TZ)
        raw = self.store.save_raw(b"synthetic", {})
        result = self.store.save_result(Observation(status="ok", source="synthetic", snapshot_id=raw,
            data={"text": "X" * 150000}))
        ctx.result_tools[result.result_id] = "get_stock_news"; ctx.evidence_ids.add(raw)
        config = AnswerReviewSettings(agentic=True, max_evidence_chars=1000)
        value = build_evidence_packet(ctx, self.store, {}, config)
        self.assertTrue(value["catalog_complete"])
        self.assertEqual(value["incomplete"], [])
        self.assertEqual(len(value["evidence"][0]["body"]["text"]), 150000)


if __name__ == "__main__":
    unittest.main()
