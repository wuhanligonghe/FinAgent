"""Synthetic protocol, provenance, and evidence-gate checks; not Qwen accuracy."""
from copy import deepcopy
from datetime import datetime
import json
from unittest.mock import Mock, patch
import unittest

import httpx

from finagent.claim_extraction import QwenClaimExtractor, claim_number
from finagent.config import ClaimExtractionSettings, Settings
from finagent.data.contracts import Observation
from finagent.errors import AgentError
from finagent.fact_check import inspect_answer
from finagent.harness.host import SessionHost
from finagent.models import ModelReply
from finagent.runtime import ExecutionContext, TZ
from finagent.security import SecurityGuard
from tests.support import ServiceCase, ScriptedModel, call_reply


def claim(quote="贵州茅台股价十元", *, value="十", subject="贵州茅台", metric="price", unit="元", period=None, kind="fact"):
    return {"quote": quote, "kind": kind, "subject": subject, "metric": metric,
            "period": period, "value": value, "unit": unit}


def extractor(payload=None, *, handler=None, config=None, **overrides):
    seen = []
    def respond(request):
        seen.append(json.loads(request.content))
        if handler:
            return handler(request)
        response = {"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                    "prompt_eval_count": 80, "eval_count": 40,
                    "message": {"role": "assistant", "content": json.dumps(
                        payload if payload is not None else {"complete": True, "claims": [claim()]}, ensure_ascii=False)}}
        response.update(overrides)
        return httpx.Response(200, json=response)
    return QwenClaimExtractor(config or ClaimExtractionSettings(), transport=httpx.MockTransport(respond)), seen


class ProtocolTests(unittest.TestCase):
    def test_non_thinking_schema_provenance_and_usage(self):
        model, seen = extractor()
        before, usage = Mock(), Mock()
        answer = "查询结果：贵州茅台股价十元。"
        result = model.extract(answer, before_model=before, on_usage=usage, guard=SecurityGuard(Settings()))
        self.assertEqual(result["claims"][0]["start"], answer.index("贵州茅台"))
        self.assertEqual(answer[result["claims"][0]["start"]:result["claims"][0]["end"]], claim()["quote"])
        self.assertFalse(seen[0]["think"])
        self.assertFalse(seen[0]["stream"])
        self.assertEqual(seen[0]["format"]["type"], "object")
        self.assertEqual(seen[0]["options"]["num_predict"], 4096)
        self.assertFalse(any(k.startswith("_") for m in seen[0]["messages"] for k in m))
        self.assertIn("untrusted_data", seen[0]["messages"][1]["content"])
        self.assertEqual(len(seen), 1)
        before.assert_called_once()
        usage.assert_called_once_with({"prompt_tokens": 80, "completion_tokens": 40})

    def test_rejects_incomplete_wrong_model_and_invalid_response(self):
        cases = [{"done_reason": "length"}, {"model": "different"}, {"done": False},
                 {"message": {"role": "assistant", "content": "{}", "thinking": "hidden"}},
                 {"message": {"role": "assistant", "content": '{"complete":true,"complete":false,"claims":[]}'}}]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                model, _ = extractor(**overrides)
                with self.assertRaisesRegex(AgentError, "CLAIM_EXTRACTION_INVALID"):
                    model.extract(claim()["quote"])

    def test_rejects_invented_quotes_values_entities_units_and_periods(self):
        for changes in ({"quote": "不存在的原文"}, {"value": "99"}, {"subject": "编造公司"},
                        {"period": "2025年"}, {"unit": "美元"}, {"metric": "arbitrary_metric"}):
            with self.subTest(changes=changes):
                model, _ = extractor({"complete": True, "claims": [{**claim(), **changes}]})
                with self.assertRaisesRegex(AgentError, "CLAIM_EXTRACTION_INVALID"):
                    model.extract(claim()["quote"])
        model, _ = extractor({"complete": False, "claims": [claim()]})
        with self.assertRaises(AgentError):
            model.extract(claim()["quote"])

    def test_timeout_http_failure_and_input_overflow_never_look_successful(self):
        for handler, code in ((lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("timeout")), "TIMEOUT"),
                              (lambda r: httpx.Response(503), "UNAVAILABLE")):
            model, _ = extractor(handler=handler)
            with self.assertRaisesRegex(AgentError, code):
                model.extract(claim()["quote"])
        model, seen = extractor(config=ClaimExtractionSettings(max_input_chars=100))
        with self.assertRaisesRegex(AgentError, "CLAIM_EXTRACTION_INPUT"):
            model.extract("长" * 101)
        self.assertFalse(seen)

    def test_long_answer_is_processed_in_full_with_original_offsets(self):
        def respond(request):
            body = json.loads(request.content)
            chunk = json.loads(body["messages"][-1]["content"])["answer"]
            payload = {"complete": True, "claims": [claim()] if claim()["quote"] in chunk else []}
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}})
        model, seen = extractor(handler=respond)
        answer = "这是合成背景。" * 800 + claim()["quote"]
        result = model.extract(answer)
        self.assertGreater(len(seen), 1)
        self.assertEqual(result["claims"][0]["start"], answer.index(claim()["quote"]))
        self.assertEqual("".join(json.loads(body["messages"][-1]["content"])["answer"] for body in seen), answer)

    def test_claim_ending_before_chunk_end_does_not_change_next_chunk_offset(self):
        answer = claim()["quote"] + "；" + "合成背景。" * 800
        def respond(request):
            chunk = json.loads(json.loads(request.content)["messages"][-1]["content"])["answer"]
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(
                    {"complete": True, "claims": [claim()] if claim()["quote"] in chunk else []}, ensure_ascii=False)}})
        model, seen = extractor(handler=respond)
        result = model.extract(answer)
        self.assertEqual(len(result["claims"]), 1)
        chunks = [json.loads(body["messages"][-1]["content"])["answer"] for body in seen]
        self.assertEqual("".join(chunks), answer)
        self.assertEqual(len(chunks), 2)

    def test_chunks_share_a_deadline_and_partial_results_are_not_returned(self):
        clock = [0.0]
        def respond(request):
            clock[0] = 10.0
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": '{"complete":true,"claims":[]}'}})
        model, seen = extractor(handler=respond)
        with patch("finagent.claim_extraction.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(AgentError, "CLAIM_EXTRACTION_TIMEOUT"):
                model.extract("合成背景。" * 1000, timeout=1)
        self.assertEqual(len(seen), 1)

    def test_single_late_response_is_not_published(self):
        clock = [0.0]
        def respond(request):
            clock[0] = 10.0
            return httpx.Response(200, json={"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": '{"complete":true,"claims":[]}'}})
        model, _ = extractor(handler=respond)
        with patch("finagent.claim_extraction.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(AgentError, "CLAIM_EXTRACTION_TIMEOUT"):
                model.extract("你好", timeout=1)

    def test_modality_is_grounded_in_draft_rather_than_model_confidence(self):
        model, _ = extractor({"complete": True, "claims": [claim(kind="uncertain")]})
        item = model.extract(claim()["quote"])["claims"][0]
        self.assertEqual(item["kind"], "fact")
        self.assertEqual(item["model_kind"], "uncertain")
        forecast = claim("预计贵州茅台明年营业收入十二亿元", metric="revenue", value="十二", unit="亿元", kind="hypothesis")
        model, _ = extractor({"complete": True, "claims": [forecast]})
        self.assertEqual(model.extract(forecast["quote"])["claims"][0]["kind"], "prediction")

    def test_chinese_numerals_preserve_numeric_meaning(self):
        for raw, expected in (("十", "10"), ("九十九", "99"), ("一亿两千万", "120000000"),
                              ("一点二亿", "120000000"), ("负十五", "-15"), ("1,234.56", "1234.56"),
                              ("一百零二", "102")):
            self.assertEqual(claim_number(raw), claim_number(expected))
        for raw in ("一百二", "NaN", "Infinity", "一千多", "约十", "二到三", "万", "亿", "十百", "一二十"):
            with self.assertRaises(ValueError):
                claim_number(raw)


class ExtractedEvidenceTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.ctx = ExecutionContext()
        self.ctx.as_of = datetime(2026, 11, 5, 12, tzinfo=TZ)
        self.ctx.symbol = "600519.SH"
        self.state = {}
        self.hosts = []

    def tearDown(self):
        for host in self.hosts:
            host.close()
        super().tearDown()

    def source(self, data=None, symbol="600519.SH", tool="get_quote", **kwargs):
        raw = self.store.save_raw(b"synthetic extraction evidence", {"synthetic": True})
        observation = self.store.save_result(Observation(status="ok", source="synthetic", symbol=symbol,
            currency="CNY", snapshot_id=raw, data=data or {"name": "贵州茅台", "price": 10}, **kwargs))
        self.ctx.result_tools[observation.result_id] = tool
        self.ctx.evidence_ids.update({raw, observation.result_id})
        return observation

    def check(self, item, answer=None):
        answer = answer or item["quote"]
        model, _ = extractor({"complete": True, "claims": [item]})
        extracted = model.extract(answer)["claims"]
        return inspect_answer(answer, self.ctx, self.store, self.state, extracted)

    def test_chinese_price_is_checked_beyond_regex_coverage(self):
        self.source()
        self.assertEqual(inspect_answer(claim()["quote"], self.ctx, self.store, self.state)["status"], "not_verified")
        report = self.check(claim())
        self.assertFalse(report["issues"])
        self.assertEqual(report["checked"][-1]["recognizer"], "qwen")
        self.assertTrue(self.check(claim("贵州茅台股价九十九元", value="九十九"))["issues"])

    def test_period_metric_unit_and_identity_cannot_be_changed_by_extractor(self):
        self.source({"reports": [{"report_period": "2024-12-31", "metrics": {"revenue": {"value": 1000000000}}}]},
                    tool="get_financial_statements")
        good = claim("贵州茅台2024年营业收入十亿元", metric="revenue", period="2024年", unit="亿元")
        self.assertFalse(self.check(good)["issues"])
        wrong_year = {**good, "quote": good["quote"].replace("2024", "2025"), "period": "2025年"}
        self.assertTrue(self.check(wrong_year)["issues"])
        self.assertTrue(self.check({**good, "metric": "net_profit"})["issues"])
        wrong_unit = {**good, "quote": good["quote"].replace("十亿元", "十万元")}
        self.assertTrue(self.check(wrong_unit, wrong_unit["quote"] + "；展示单位还有亿元")["issues"])
        self.assertTrue(self.check({**good, "subject": "000858.SZ"}, good["quote"] + "；另一个对象000858.SZ")["issues"])

    def test_quarterly_and_annual_reporting_periods_cannot_be_borrowed(self):
        self.source({"reports": [
            {"report_period": "2024-09-30", "metrics": {"revenue": {"value": 1000000000}}},
            {"report_period": "2024-12-31", "metrics": {"revenue": {"value": 2000000000}}},
        ]}, tool="get_financial_statements")
        item = claim("贵州茅台2024年前三季度营业收入十亿元", metric="revenue", period="2024年前三季度", unit="亿元")
        self.assertFalse(self.check(item)["issues"])
        annual = {**item, "quote": "贵州茅台2024年营业收入十亿元", "period": "2024年"}
        self.assertTrue(self.check(annual)["issues"])
        ambiguous = {**item, "quote": "贵州茅台2024年三季度营业收入十亿元", "period": "2024年三季度"}
        self.assertTrue(self.check(ambiguous)["issues"])

    def test_false_nonfactual_label_does_not_exempt_observed_numeric_assertion(self):
        self.source()
        bad = claim("贵州茅台股价九十九元", value="九十九", kind="hypothesis")
        self.assertTrue(self.check(bad)["issues"])
        hypothetical = claim("假设贵州茅台股价九十九元", value="九十九", kind="hypothesis")
        report = self.check(hypothetical)
        self.assertFalse(report["issues"])
        self.assertTrue(report["unverified"])
        mixed = claim("假设贵州茅台股价一百元，但是贵州茅台现价九十九元", value="九十九", kind="hypothesis")
        self.assertTrue(self.check(mixed)["issues"])

    def test_user_attribution_is_recovered_from_draft_even_if_quote_omits_it(self):
        self.source()
        self.state["user_updates"] = [{"text": "贵州茅台股价一百元"}]
        item = claim("贵州茅台股价一百元", value="一百", kind="fact")
        report = self.check(item, "按你提供的信息，" + item["quote"])
        self.assertFalse(report["issues"])
        self.assertTrue(report["unverified"])
        self.assertEqual(report["status"], "not_verified")

    def test_percentage_points_are_not_percentage_rates(self):
        self.source({"kind": "financial_calculation", "purpose": "growth", "value": "15", "unit": "%"}, tool="execute_program")
        item = claim("贵州茅台同比增长十五个百分点", metric="calculation:growth", value="十五", unit="个百分点")
        self.assertTrue(self.check(item)["issues"])

    def test_qualitative_claim_is_not_certified_by_extraction(self):
        item = claim("贵州茅台拥有很强的护城河", metric=None, value=None, unit=None)
        report = self.check(item)
        self.assertFalse(report["issues"])
        self.assertEqual(report["status"], "not_verified")
        self.assertEqual(report["unverified"][-1]["reason"], "qualitative_claim_no_semantic_verifier")

    def test_partial_numeric_span_is_not_a_valid_replacement(self):
        self.source()
        report = self.check(claim("贵州茅台股价一百一十元", value="十"))
        self.assertTrue(any(x["kind"] == "CLAIM_VALUE_MISMATCH" for x in report["issues"]))

    def test_invalidated_evidence_and_unexecuted_calculations_are_rejected(self):
        observation = self.source()
        self.ctx.invalidated_result_ids.add(observation.result_id)
        self.assertTrue(self.check(claim())["issues"])
        self.source({"kind": "financial_calculation", "purpose": "growth", "value": "15", "unit": "%"},
                    tool="generate_program")
        item = claim("贵州茅台同比增长百分之十五", metric="calculation:growth", value="十五", unit="百分之")
        self.assertTrue(self.check(item)["issues"])
        self.source({"kind": "financial_calculation", "purpose": "growth", "value": "15", "unit": "%"},
                    tool="execute_program")
        self.assertFalse(self.check(item)["issues"])

    def host(self, model, extraction_model):
        self.settings.harness.memory_enabled = False
        self.settings.fact_check.extraction.enabled = True
        host = SessionHost(self.settings, "claims_" + str(len(self.hosts)), model=model, service=self.service,
                           claim_extractor=extraction_model)
        self.hosts.append(host)
        return host

    def test_real_gate_uses_extractor_and_accounts_for_auxiliary_call(self):
        extraction_model, seen = extractor()
        model = ScriptedModel(call_reply(("quote", "get_quote", {"symbol": "600519.SH"})),
                              ModelReply({"role": "assistant", "content": claim()["quote"]}))
        host = self.host(model, extraction_model)
        result = host.submit("查询茅台股价")
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(len(seen), 1)
        self.assertEqual(host.state["fact_check"]["extraction"]["status"], "extracted")
        self.assertEqual(host.state["last_ledger"]["operations"]["model"], 3)
        self.assertEqual(host.state["usage"]["claim_extraction"]["prompt_tokens"], 80)

    def test_regex_conflicts_cannot_be_overridden_by_extraction(self):
        extraction_model = Mock()
        model = ScriptedModel(ModelReply({"role": "assistant", "content": "贵州茅台股价99元"}))
        host = self.host(model, extraction_model)
        result = host.submit("查询茅台股价")
        self.assertEqual(result.status, "failed")
        self.assertNotIn("99", result.answer)
        extraction_model.extract.assert_not_called()

    def test_empty_extraction_and_service_failure_block_delivery(self):
        for extraction_model in (extractor({"complete": True, "claims": []})[0],
                                 extractor(handler=lambda r: httpx.Response(503))[0]):
            host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": claim()["quote"]})), extraction_model)
            result = host.submit("查询茅台股价")
            self.assertEqual(result.status, "failed")
            self.assertIn("CLAIM_EXTRACTION_FAILED", result.reason)
            self.assertNotEqual(result.answer, claim()["quote"])
            self.assertEqual(host.state["fact_validation"], "not_verified")

    def test_general_code_can_have_no_financial_claims_without_verified_status(self):
        extraction_model, _ = extractor({"complete": True, "claims": []})
        code = '#include <stdio.h>\nint main(void) { puts("Hello World"); return 0; }'
        host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": code})), extraction_model)
        result = host.submit("写一个C语言Hello World")
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(host.state["fact_validation"], "not_verified")
        self.assertEqual(host.state["fact_check"]["extraction"]["status"], "empty")

    def test_budget_exhaustion_cannot_publish_unextracted_draft(self):
        extraction_model, seen = extractor()
        self.settings.limits.model_calls = 1
        host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": claim()["quote"]})), extraction_model)
        result = host.submit("查询茅台股价")
        self.assertEqual(result.status, "failed")
        self.assertIn("MODEL_BUDGET", result.reason)
        self.assertFalse(seen)

    def test_cancel_during_extraction_does_not_deliver_draft(self):
        extraction_model = Mock()
        host = self.host(ScriptedModel(ModelReply({"role": "assistant", "content": claim()["quote"]})), extraction_model)
        def cancel(*args, **kwargs):
            kwargs["before_model"]()
            host.ctx.cancelled.set()
            return {"claims": []}
        extraction_model.extract.side_effect = cancel
        result = host.submit("查询茅台股价")
        self.assertEqual(result.status, "cancelled")
        self.assertNotEqual(result.answer, claim()["quote"])


if __name__ == "__main__":
    unittest.main()
