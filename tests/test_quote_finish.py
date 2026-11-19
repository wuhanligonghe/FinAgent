"""Synthetic regressions for quote facts and visible final-answer repair."""
from datetime import timedelta
import json

import httpx

from finagent.agent import AgentLoop, RunRequest
from finagent.finish import FinalAnswer, QUOTE_FACT_UNITS, finish_errors
from finagent.models import ModelReply
from finagent.runtime import ExecutionContext

from .support import ServiceCase, ScriptedModel, call_reply, final_reply, tencent_body


class QuoteFinishTests(ServiceCase):
    def quote(self):
        ctx = ExecutionContext()
        self.handler = lambda _: httpx.Response(200, content=tencent_body(stamp=ctx.as_of.strftime("%Y%m%d%H%M%S")))
        value = self.store.save_result(self.registry.execute("get_quote", {"symbol": "600519"}, ctx))
        ctx.result_tools[value.result_id] = "get_quote"
        ctx.perception_results.add(value.result_id)
        ctx.evidence_ids.add(value.result_id)
        return ctx, value

    def answer(self, quote, **overrides):
        fields = {"status": "completed", "answer": "合成行情事实，不是真实市场数据",
                  "evidence_ids": [quote.snapshot_id], "facts": [
                      {"reference": quote.result_id, "field": field, "value": str(quote.data[field]),
                       "unit": unit, "statement": "合成测试事实"}
                      for field, unit in QUOTE_FACT_UNITS.items() if quote.data.get(field) is not None]}
        return FinalAnswer(**(fields | overrides))

    def test_price_volume_amount_and_ratios_can_reference_current_quote(self):
        ctx, quote = self.quote()
        final = self.answer(quote)
        self.assertTrue({"price", "volume_shares", "amount_cny", "pb"}.issubset({f["field"] for f in final.facts}))
        self.assertEqual(finish_errors(final, ctx, self.store), [])

    def test_wrong_values_units_missing_fields_and_derived_metrics_are_rejected(self):
        ctx, quote = self.quote()
        base = {"reference": quote.result_id, "field": "price", "value": "10", "unit": "CNY/share"}
        for change in ({"value": "99"}, {"unit": "CNY"}, {"field": "pe_ttm"},
                       {"field": "change_percent"}, {"reference": "result_" + "0" * 32}):
            with self.subTest(change=change):
                errors = finish_errors(self.answer(quote, facts=[base | change]), ctx, self.store)
                self.assertTrue(errors)
                self.assertTrue(any("facts[0]" in error for error in errors))

    def test_partial_status_does_not_bypass_quote_fact_check(self):
        ctx, quote = self.quote()
        claim = {"reference": quote.result_id, "field": "price", "value": "99", "unit": "CNY/share"}
        self.assertTrue(finish_errors(self.answer(quote, status="partial", facts=[claim]), ctx, self.store))

    def test_quote_requires_original_evidence_and_current_result(self):
        ctx, quote = self.quote()
        self.assertTrue(finish_errors(self.answer(quote, evidence_ids=[]), ctx, self.store))
        ctx.invalidated_result_ids.add(quote.result_id)
        self.assertTrue(finish_errors(self.answer(quote), ctx, self.store))
        ctx.invalidated_result_ids.clear()
        ctx.result_tools.clear()
        self.assertTrue(finish_errors(self.answer(quote), ctx, self.store))

    def test_cross_company_and_after_cutoff_quotes_are_rejected(self):
        ctx, quote = self.quote()
        final = self.answer(quote)
        ctx.symbol = "300750.SZ"
        self.assertTrue(any("证券" in error for error in finish_errors(final, ctx, self.store)))
        ctx.symbol = "600519.SH"
        ctx.as_of -= timedelta(days=1)
        self.assertTrue(any("数据时间" in error for error in finish_errors(final, ctx, self.store)))

    def test_quote_and_calculated_facts_complete_together_without_repairs(self):
        ctx = ExecutionContext()
        self.handler = lambda _: httpx.Response(200, content=tencent_body(stamp=ctx.as_of.strftime("%Y%m%d%H%M%S")))

        def calculate(messages):
            quote = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
            return call_reply(("c", "calculate_market_metrics", {"result_id": quote["result_id"]}))

        def finish(messages):
            quote, calculation = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            facts = [{"reference": quote["result_id"], "field": field, "value": str(quote["data"][field]),
                      "unit": QUOTE_FACT_UNITS[field], "statement": "合成快照事实"}
                     for field in ("price", "volume_shares", "amount_cny")]
            facts.append({"reference": calculation["result_id"], "field": "change_percent",
                          "value": str(calculation["data"]["change_percent"]), "unit": "%", "statement": "合成涨跌幅"})
            final = FinalAnswer(status="completed", answer="合成行情快照及程序涨跌幅，最终收盘状态未知。",
                                evidence_ids=[quote["snapshot_id"]], calculation_ids=[calculation["result_id"]], facts=facts)
            return ModelReply({"role": "assistant", "content": final.model_dump_json()})

        model = ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519"})), calculate, finish)
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("查询合成行情"), ctx)
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual(result.model_calls, 3)
        state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["corrections"], 0)

    def test_optional_legacy_fact_fields_do_not_trigger_final_retries(self):
        def finish(messages):
            quote = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))
            feedback = [m["content"] for m in messages if m["role"] == "system" and m["content"].startswith("宿主结束校验未通过")]
            if feedback:
                self.assertIn("facts[0]", feedback[-1])
                self.assertIn("price", feedback[-1])
            final = FinalAnswer(status="completed", answer="合成快照，不是真实市场数据",
                                evidence_ids=[quote["snapshot_id"]], facts=[{
                                    "reference": quote["result_id"], "field": "price", "unit": "CNY/share",
                                    "value": str(quote["data"]["price"]) if feedback else "999"}])
            return ModelReply({"role": "assistant", "content": final.model_dump_json()})

        model = ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519"})), finish)
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("查询合成行情"))
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual((result.model_calls, result.tool_calls), (2, 1))
        body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertFalse(body["final_protocol_verified"])
        self.assertNotIn("facts", body["final_answer"])

    def test_missing_final_fields_never_start_a_repair_loop(self):
        progress = []
        result = AgentLoop(ScriptedModel(final_reply()), self.registry, self.store).run(
            RunRequest("查行情"), progress_handler=progress.append)
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(progress, [])
        self.assertEqual(result.answer, "合成测试答案")
        events = [json.loads(line) for line in (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        failures = [event for event in events if event["kind"] == "finish_rejected"]
        self.assertEqual(failures, [])
