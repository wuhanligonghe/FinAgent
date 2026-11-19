"""Synthetic reviewer/provenance/gate tests, not evidence of model accuracy."""
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

import httpx
import jsonschema

from finagent.answer_review import QwenAnswerReviewer, build_evidence_packet
from finagent.config import AnswerReviewSettings, load_settings
from finagent.data.contracts import Observation
from finagent.errors import AgentError
from finagent.harness.host import SessionHost
from finagent.models import ModelReply
from finagent.runtime import ExecutionContext, TZ
from tests.support import ServiceCase, ScriptedModel, call_reply


def verdict(name="pass", *, citations=None, issues=None, **kw):
    return {"complete": True, "verdict": name, "summary": "合成审核结果",
            "evidence_required": bool(citations),
            "citations": citations or [], "issues": [{"citations": [], **i} for i in issues or []], **kw}


def reviewer(decision=None, *, handler=None, config=None, **overrides):
    seen = []
    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        if handler:
            return handler(request)
        response = {"model": body["model"], "done": True, "done_reason": "stop",
                    "prompt_eval_count": 80, "eval_count": 40,
                    "message": {"role": "assistant", "content": json.dumps(decision or verdict(), ensure_ascii=False)}}
        response.update(overrides)
        return httpx.Response(200, json=response)
    return QwenAnswerReviewer(config or AnswerReviewSettings(), transport=httpx.MockTransport(respond)), seen


class ReviewerProtocolTests(unittest.TestCase):
    def test_wire_schema_pairs_exact_excerpt_with_its_own_evidence_id(self):
        packet = {"complete": True, "evidence": [
            {"evidence_id": "result_first", "body": {"metrics": {"amount": "120亿元"}}},
            {"evidence_id": "result_second", "body": {"metrics": {"amount": "240亿元"}}}]}
        decision = verdict(citations=[{"evidence_id": "result_first", "quote": '"amount": "120亿元"'}])
        model, seen = reviewer(decision)
        self.assertEqual(model.review("金额120亿元。", packet)["verdict"], "pass")
        schema = seen[0]["format"]
        jsonschema.validate(decision, schema)
        invalid = deepcopy(decision)
        invalid["citations"][0]["evidence_id"] = "result_second"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)
        invalid["citations"][0] = {"evidence_id": "result_first", "quote": "金额为120亿元"}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)
        payload = json.loads(seen[0]["messages"][-1]["content"])
        self.assertEqual(payload["packet"], packet)
        self.assertIn("citation_options", payload)
        issue_decision = deepcopy(decision)
        issue_decision.update(verdict="revise", issues=[{
            "answer_quote": "金额120亿元。", "kind": "unsupported", "reason": "合成问题",
            "suggestion": "核对范围", "citations": []}])
        jsonschema.validate(issue_decision, schema)
        issue_decision["issues"][0]["answer_quote"] = "金额为120亿元"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(issue_decision, schema)

    def packet(self):
        return {"complete": True, "evidence": [{"evidence_id": "result_test", "body": {"text": "销售回款增加\n采购付款减少"}}]}

    def test_complete_answer_and_evidence_in_one_request(self):
        model, seen = reviewer(verdict(citations=[{"evidence_id": "result_test", "quote": "销售回款增加\n采购付款减少"}]))
        before, usage = Mock(), Mock()
        result = model.review("回款增加，付款减少。", self.packet(), before_model=before, on_usage=usage)
        payload = json.loads(seen[0]["messages"][-1]["content"])
        self.assertEqual(payload["answer"], "回款增加，付款减少。")
        self.assertEqual(payload["packet"], self.packet())
        self.assertFalse(seen[0]["think"])
        self.assertFalse(seen[0]["stream"])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(len(seen), 1)
        before.assert_called_once()
        usage.assert_called_once_with({"prompt_tokens": 80, "completion_tokens": 40})

    def test_rejects_invented_quote_id_or_answer_and_inconsistent_verdicts(self):
        issue = {"answer_quote": "完全来自回款", "kind": "overstatement", "reason": "遗漏付款变化", "suggestion": "保留两个原因"}
        cases = [verdict(citations=[{"evidence_id": "unknown", "quote": "销售回款增加"}]),
                 verdict(citations=[{"evidence_id": "result_test", "quote": "虚构原文"}]),
                 verdict(evidence_required=True), verdict(complete=False), verdict(issues=[issue]), verdict("revise"),
                 verdict("revise", issues=[{**issue, "answer_quote": "不存在的草稿"}])]
        for decision in cases:
            with self.subTest(decision=decision):
                model, _ = reviewer(decision)
                with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_INVALID"):
                    model.review("完全来自回款", self.packet())

    def test_overstatement_is_returned_for_repair_and_unsupported_needs_no_citation(self):
        issue = {"answer_quote": "完全来自回款", "kind": "overstatement", "reason": "证据还提到采购付款", "suggestion": "删除完全"}
        model, _ = reviewer(verdict("revise", issues=[issue]))
        result = model.review("完全来自回款", self.packet())
        self.assertEqual(result["issues"][0]["kind"], "overstatement")

    def test_timeout_failure_truncation_and_wrong_model_are_not_pass(self):
        for overrides in ({"done": False}, {"model": "other"}, {"done_reason": "length"},
                          {"message": {"role": "assistant", "content": "{}", "thinking": "hidden"}}):
            model, _ = reviewer(**overrides)
            with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_INVALID"):
                model.review("你好", {"complete": True, "evidence": []})
        for handler, error in ((lambda r: httpx.Response(503), "UNAVAILABLE"),
                               (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("timeout")), "TIMEOUT")):
            model, _ = reviewer(handler=handler)
            with self.assertRaisesRegex(AgentError, error):
                model.review("你好", {"complete": True, "evidence": []})

    def test_no_truncation_or_network_when_packet_or_input_exceeds_budget(self):
        model, seen = reviewer(config=AnswerReviewSettings(max_input_chars=100, context_tokens=4096, output_tokens=512))
        for answer, packet in (("长" * 101, self.packet()), ("你好", {**self.packet(), "complete": False}),
                               ("你好", {"complete": True, "evidence": [{"body": "长" * 4000}]})):
            with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_INPUT"):
                model.review(answer, packet)
        self.assertFalse(seen)

    def test_concept_answer_can_pass_without_external_evidence(self):
        model, _ = reviewer()
        self.assertEqual(model.review("现金流反映现金收支。", {"complete": True, "evidence": []})["verdict"], "pass")
        self.assertEqual(model.review("你好", self.packet())["verdict"], "pass")

    def test_late_review_and_duplicate_json_never_pass(self):
        clock = [0.0]
        def late(request):
            clock[0] = 2.0
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(verdict())}})
        model, _ = reviewer(handler=late)
        with patch("finagent.answer_review.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_TIMEOUT"):
                model.review("你好", {"complete": True, "evidence": []}, timeout=1)
        model, _ = reviewer(message={"role": "assistant", "content": '{"complete":true,"complete":false}'})
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_INVALID"):
            model.review("你好", {"complete": True, "evidence": []})

    def test_protocol_repair_is_bounded_and_accounts_for_both_calls(self):
        calls, charged, usage = [], Mock(), Mock()
        def respond(request):
            payload = json.loads(json.loads(request.content)["messages"][-1]["content"])
            calls.append(payload)
            decision = {"complete": True, "verdict": "pass"} if len(calls) == 1 else verdict()
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "prompt_eval_count": 5, "eval_count": 3,
                "message": {"role": "assistant", "content": json.dumps(decision)}})
        model, _ = reviewer(handler=respond)
        result = model.review("你好", {"complete": True, "evidence": []}, before_model=charged, on_usage=usage)
        self.assertEqual(result["protocol_attempts"], 2)
        self.assertEqual(result["usage"], {"prompt_tokens": 10, "completion_tokens": 6})
        self.assertEqual(charged.call_count, 2)
        self.assertEqual(usage.call_count, 2)
        self.assertIn("protocol_feedback", calls[1])

    def test_missing_user_requirement_uses_null_quote_not_invented_text(self):
        decision = verdict("revise", issues=[{"answer_quote": None, "kind": "scope",
            "reason": "用户要求比较两家公司，草稿只讨论一家", "suggestion": "补充另一家公司或说明缺口"}])
        model, _ = reviewer(decision)
        self.assertEqual(model.review("茅台现金流分析。", {"complete": True, "evidence": []})["verdict"], "revise")


class EvidencePacketTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.ctx = ExecutionContext()
        self.ctx.as_of = datetime(2026, 11, 5, 12, tzinfo=TZ)
        self.ctx.symbol = "600519.SH"
        self.state = {"review_turn": {"id":"current_turn"},
            "todos": [{"content":"比较两家公司现金流，不看股价", "status":"in_progress"}],
            "user_constraints": [{"text":"不看股价", "status":"active"}],
            "user_updates": [{"text": "比较两家公司现金流，不看股价"}]}
        self.config = AnswerReviewSettings()

    def source(self, *, symbol="600519.SH", tool="get_financial_statements", turn_id="current_turn", **kw):
        raw = self.store.save_raw(b"synthetic original", {"synthetic": True, "fetched_at": "2026-11-05T10:00:00+08:00"})
        data = kw.pop("data", {"reports": [{"report_period": "2024-12-31", "available_at": "2025-04-01T00:00:00+08:00",
                       "metrics": {"operating_cashflow": {"value": "12340000000", "locator": "/cashflow"}}}]})
        result = self.store.save_result(Observation(status="partial", symbol=symbol, source="synthetic",
            snapshot_id=raw, data=data, warnings=["供应商累计口径"], **kw))
        self.ctx.result_tools[result.result_id] = tool
        self.ctx.evidence_ids.update({raw, result.result_id})
        self.state.setdefault("evidence_registry", {})[result.result_id] = {"turn_id": turn_id, "symbol": symbol, "snapshot_ids":[raw]}
        return result

    def packet(self):
        return build_evidence_packet(self.ctx, self.store, self.state, self.config)

    def test_compares_current_turn_entities_and_reuses_matching_old_evidence(self):
        self.ctx.scope_constraints["allowed_report_periods"] = ["2024-12-31"]
        first, second = self.source(), self.source(symbol="000858.SZ")
        self.source(symbol="000001.SZ",turn_id="old_turn")
        old = self.source(turn_id="old_turn")
        self.source(data={"reports": [{"report_period": "2023-12-31"}]})
        packet = self.packet()
        self.assertTrue(packet["complete"])
        self.assertEqual({e["evidence_id"] for e in packet["evidence"]}, {first.result_id, second.result_id, old.result_id})
        self.assertEqual({x["reason"] for x in packet["excluded"]}, {"other_entity", "other_report_period"})
        self.assertEqual(packet["request_context"]["user_constraints"][0]["text"], "不看股价")
        self.assertNotIn("task", packet)
        self.assertIn("metrics", packet["evidence"][0]["body"]["reports"][0])
        self.assertEqual(packet["evidence"][0]["warnings"], ["供应商累计口径"])
        self.assertIsNone(packet["evidence"][0]["fetched_at"])
        self.assertEqual(packet["evidence"][0]["snapshots"][0]["metadata"]["fetched_at"], "2026-11-05T10:00:00+08:00")

    def test_stale_invalid_future_or_unauthorized_evidence_cannot_support(self):
        self.source(stale=True)
        bad = self.source()
        self.ctx.invalidated_result_ids.add(bad.result_id)
        self.source(data_time="2027-01-01")
        valid = self.source()
        # Shared snapshot, remove authorization for all remaining sources.
        self.ctx.evidence_ids.remove(valid.snapshot_id)
        packet = self.packet()
        self.assertFalse(packet["evidence"])
        self.assertFalse(packet["complete"])

    def test_budget_overflow_is_explicit_and_blocks_review(self):
        self.config.max_evidence_results = 1
        self.source()
        self.source(symbol="000858.SZ")
        packet = self.packet()
        self.assertFalse(packet["complete"])
        self.assertEqual(packet["incomplete"][0]["reason"], "evidence_budget_exceeded")
        model, seen = reviewer()
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_INPUT"):
            model.review("比较现金流", packet)
        self.assertFalse(seen)

    def test_project_review_budget_accepts_two_complete_market_windows(self):
        self.config = load_settings(Path(__file__).resolve().parents[1] / "finagent.toml").fact_check.review
        # This regression exercises the explicit fixed-request compatibility mode.
        self.config.agentic = False
        self.config.backend = "ollama"
        bars = [{"date": f"session_{i}", "open": "100.00", "close": "101.00",
                 "high": "102.00", "low": "99.00", "volume": "12345678"}
                for i in range(200)]
        first = self.source(tool="get_market_data", data={"bars": bars, "window": "first"})
        second = self.source(tool="get_market_data", data={"bars": bars, "window": "second"})
        packet = self.packet()
        self.assertGreater(len(json.dumps(packet, ensure_ascii=False)), 40000)
        self.assertTrue(packet["complete"])
        self.assertEqual({e["evidence_id"] for e in packet["evidence"]}, {first.result_id, second.result_id})
        self.assertTrue(all(len(e["body"]["bars"]) == 200 for e in packet["evidence"]))
        model, seen = reviewer(config=self.config)
        model.review("走势只能作条件分析。", packet)
        self.assertEqual(len(seen), 1)

    def test_explicit_read_can_reuse_old_evidence_without_task_frame(self):
        value = self.source(symbol="000858.SZ",turn_id="old_turn")
        self.state["review_turn"] = {"read_ids": [value.snapshot_id]}
        self.assertEqual(self.packet()["evidence"][0]["evidence_id"], value.result_id)

    def test_missing_snapshot_and_result_block_complete_packet(self):
        value = self.source()
        (self.store.root / "evidence" / f"{value.snapshot_id}.bin").unlink()
        self.assertFalse(self.packet()["complete"])
        (self.store.root / "results" / f"{value.result_id}.json").unlink()
        self.assertEqual(self.packet()["incomplete"][0]["reason"], "result_unreadable")

    def test_unreadable_other_entity_history_does_not_block_current_turn(self):
        unrelated = self.source(symbol="000001.SZ",turn_id="old_turn")
        (self.store.root / "results" / f"{unrelated.result_id}.json").unlink()
        current = self.source()
        packet = self.packet()
        self.assertTrue(packet["complete"])
        self.assertEqual([e["evidence_id"] for e in packet["evidence"]], [current.result_id])

    def test_report_available_after_cutoff_blocks_packet(self):
        self.source(data={"reports": [{"report_period": "2024-12-31", "available_at": "2027-01-01T00:00:00+08:00"}]})
        packet = self.packet()
        self.assertFalse(packet["complete"])
        self.assertEqual(packet["incomplete"][0]["reason"], "source_metadata_or_time_invalid")

    def test_review_receives_news_dates_and_user_window_without_interpreting_todo_as_scope(self):
        self.state["user_updates"]=[{"text":"看2025年新闻"}]
        self.source(tool="get_stock_news", data={"articles": [
            {"snippet":"旧新闻", "published_at":"2024-01-01T00:00:00+08:00"},
            {"snippet":"相关摘要", "published_at":"2025-04-01T00:00:00+08:00"}]})
        packet=self.packet()
        self.assertTrue(packet["complete"])
        self.assertEqual(packet["user_request"],"看2025年新闻")
        self.assertEqual(len(packet["evidence"][0]["body"]["articles"]),2)

    def test_review_environment_overrides_are_independent_of_extraction(self):
        path = self.root / "finagent.toml"
        path.write_text('[fact_check.review]\nenabled=false\n')
        with patch.dict(os.environ, {"FINAGENT_ANSWER_REVIEW_ENABLED": "true",
                "FINAGENT_ANSWER_REVIEW_MODEL": "review-fixture", "FINAGENT_ANSWER_REVIEW_ENDPOINT": "http://localhost:1234"}, clear=True):
            settings = load_settings(path)
        self.assertTrue(settings.fact_check.review.enabled)
        self.assertEqual(settings.fact_check.review.model, "review-fixture")
        self.assertEqual(settings.fact_check.review.endpoint, "http://localhost:1234")
        self.assertFalse(settings.fact_check.extraction.enabled)


class ReviewGateTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.settings.harness.memory_enabled = False
        self.settings.fact_check.review.enabled = True
        self.hosts = []

    def tearDown(self):
        for host in self.hosts:
            host.close()
        super().tearDown()

    def host(self, model, review_model):
        host = SessionHost(self.settings, "review" + str(len(self.hosts)), model=model, service=self.service,
                           answer_reviewer=review_model, claim_extractor=Mock())
        self.hosts.append(host)
        return host

    def test_agentic_review_repairs_main_answer_and_persists_isolated_audit(self):
        from finagent.harness.reviewer import RUBRIC
        self.settings.fact_check.review.agentic = True
        seen = []
        def respond(request):
            body = json.loads(request.content)
            initial = json.loads(body["messages"][1]["content"])
            seen.append(initial)
            answer = initial["answer"]
            if answer == "现金流完全来自回款。":
                result = verdict("revise", issues=[{"answer_quote": answer, "kind": "unsupported",
                    "reason": "当前资料不支持归因", "suggestion": "删除无依据归因"}])
            else:
                result = verdict()
            result["checks"] = [{"dimension": k, "status": "pass" if result["verdict"] == "pass" else "unknown",
                                 "reason": "合成判定"} for k in RUBRIC]
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps({"action": "submit_review", "arguments": result})}})
        review_model, _ = reviewer(config=self.settings.fact_check.review, handler=respond)
        host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": "现金流完全来自回款。"}),
            ModelReply({"role": "assistant", "content": "我不知道，现有资料无法说明现金流变化原因。"})), review_model)
        result = host.submit("说明现金流变化原因")
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(host.review_budget.model_calls, 2)
        self.assertEqual(host.active_ledger.counts["model"], 4)
        self.assertEqual(len(seen), 2)
        self.assertNotIn("todos", seen[0]["user_requirements"])
        saved = json.loads((host.directory / host.state["fact_check"]["packet_ref"]).read_text())
        self.assertEqual(saved["audit"]["status"], "pass")
        self.assertEqual(saved["audit"]["budget_after"]["model_calls"], 2)
        self.assertTrue(saved["audit"]["events"])

    def test_agentic_repair_cannot_reset_its_review_budget(self):
        from finagent.harness.reviewer import RUBRIC
        self.settings.fact_check.review.agentic = True
        self.settings.fact_check.review.max_model_calls = 1
        def respond(request):
            result = verdict("revise", issues=[{"answer_quote": "草稿", "kind": "unsupported",
                "reason": "未核实", "suggestion": "说明缺口"}])
            result["checks"] = [{"dimension": k, "status": "unknown", "reason": "未核实"} for k in RUBRIC]
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps({"action": "submit_review", "arguments": result})}})
        review_model, _ = reviewer(config=self.settings.fact_check.review, handler=respond)
        host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": "草稿"})), review_model)
        result = host.submit("分析")
        self.assertEqual(result.status, "failed")
        self.assertIn("ANSWER_REVIEW_BUDGET", result.reason)
        self.assertNotEqual(result.answer, "草稿")

    def test_review_drives_repair_without_legacy_field_matching_or_extraction(self):
        def respond(request):
            payload = json.loads(request.content)
            data = json.loads(payload["messages"][-1]["content"])
            if data["answer"] == "现金流增长完全来自回款改善。":
                decision = verdict("revise", issues=[{"answer_quote": data["answer"], "kind": "unsupported",
                    "reason": "仅取得报价，没有现金流归因证据", "suggestion": "删除无依据断言并说明缺口"}])
            else:
                entry = data["packet"]["evidence"][0]
                decision = verdict(citations=[{"evidence_id": entry["evidence_id"], "quote": "合成测试"}])
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)}})
        review_model, seen = reviewer(handler=respond)
        main = ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519.SH"})),
            ModelReply({"role": "assistant", "content": "现金流增长完全来自回款改善。"}),
            ModelReply({"role": "assistant", "content": "现有报价无法说明现金流变化原因。"}))
        host = self.host(main, review_model)
        result = host.submit("分析茅台")
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(host.fact_check_attempts, 1)
        self.assertEqual(len(seen), 2)
        host.claim_extractor.extract.assert_not_called()
        self.assertEqual(host.state["fact_check"]["status"], "review_passed")
        self.assertEqual(host.active_ledger.counts["model"], 5)
        self.assertTrue(host.state["evidence_registry"])
        self.assertTrue((host.directory / host.state["fact_check"]["packet_ref"]).exists())
        self.assertIn("证据审核反馈", json.dumps(main.messages_seen[-1], ensure_ascii=False))

    def test_persistent_conflict_cannot_be_overridden_by_stop_hook(self):
        quote = "现金流完全来自回款"
        review_model, seen = reviewer(verdict("revise", issues=[{"answer_quote": quote, "kind": "unsupported",
            "reason": "无证据", "suggestion": "说明缺口"}]))
        host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": quote})), review_model)
        host.hooks.register("Stop", lambda **kw: {"status": "completed"})
        result = host.submit("分析茅台现金流")
        self.assertEqual(result.status, "failed")
        self.assertIn("FACT_CHECK_FAILED", result.reason)
        self.assertNotEqual(result.answer, quote)
        self.assertEqual(len(seen), 3)

    def test_failed_or_unable_review_never_delivers_draft(self):
        for review_model in (reviewer(verdict("unable_to_verify"))[0],
                             reviewer(handler=lambda r: httpx.Response(503))[0]):
            host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": "草稿"})), review_model)
            result = host.submit("分析")
            self.assertEqual(result.status, "failed")
            self.assertNotEqual(result.answer, "草稿")
            self.assertEqual(host.state["fact_check"]["status"], "not_verified")

    def test_shared_budget_and_cancellation_prevent_delivery(self):
        self.settings.limits.model_calls = 1
        review_model, seen = reviewer()
        host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": "草稿"})), review_model)
        result = host.submit("分析")
        self.assertIn("MODEL_BUDGET", result.reason)
        self.assertFalse(seen)

    def test_cancel_after_review_does_not_deliver_passed_draft(self):
        review_model = Mock()
        host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": "草稿"})), review_model)
        def cancel(*args, **kwargs):
            kwargs["before_model"]()
            host.ctx.cancelled.set()
            return {**verdict(), "engine": "test"}
        review_model.review.side_effect = cancel
        result = host.submit("分析")
        self.assertEqual(result.status, "cancelled")
        self.assertNotEqual(result.answer, "草稿")

    def test_evidence_provenance_survives_session_restore(self):
        def respond(request):
            data = json.loads(json.loads(request.content)["messages"][-1]["content"])
            decision = verdict(citations=[{"evidence_id": data["packet"]["evidence"][0]["evidence_id"], "quote": "合成测试"}])
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(decision)}})
        review_model, _ = reviewer(handler=respond)
        host = self.host(ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519.SH"})),
            ModelReply({"role": "assistant", "content": "已取得报价。"})), review_model)
        result = host.submit("查茅台报价")
        self.assertEqual(result.status, "completed", result.reason)
        old = deepcopy(host.state["evidence_registry"])
        session_id = host.session_id
        host.close()
        restored = SessionHost(self.settings, session_id, model=ScriptedModel(ModelReply({"role": "assistant", "content": "继续说明。"})),
                               service=self.service, answer_reviewer=review_model)
        self.hosts.append(restored)
        self.assertEqual(restored.state["evidence_registry"], old)
        self.assertEqual(restored.submit("继续").status, "completed")

    def test_tool_call_draft_is_withheld_until_review(self):
        review_model, _ = reviewer()
        host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": "完成"})), review_model)
        printed = []
        host.write = printed.append
        host.event("model_response", message={"tool_calls": [{"id": "x"}], "content": "无依据的定性结论"})
        self.assertNotIn("无依据的定性结论", printed)


if __name__ == "__main__":
    unittest.main()
