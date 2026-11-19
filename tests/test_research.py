from datetime import date, datetime, timedelta
import json
from unittest.mock import patch

import httpx

from finagent.agent import AgentLoop, RunRequest
from finagent.config import load_settings
from finagent.runtime import ExecutionContext, TZ, now

from .support import ServiceCase, ScriptedModel, call_reply, final_reply, tencent_body


BARS = [{"day": d, "open": "9", "close": c, "high": "11", "low": "8", "volume": "100"}
        for d, c in [("2024-04-26", "8"), ("2024-04-29", "9"), ("2024-04-30", "10")]]


class ResearchTests(ServiceCase):
    def context(self):
        return ExecutionContext(as_of=datetime(2024, 5, 1, tzinfo=TZ))

    def invoke(self, name, args, ctx):
        value = self.store.save_result(self.registry.execute(name, args, ctx))
        ctx.evidence_ids.add(value.result_id)
        ctx.result_tools[value.result_id] = name
        if name in self.registry.tools and self.registry.tools[name].perception:
            ctx.perception_results.add(value.result_id)
        return value

    def bars(self, ctx, start="2024-04-26", end="2024-04-30"):
        self.handler = lambda r: httpx.Response(200, json=BARS)
        return self.invoke("get_daily_bars", {"symbol": "600519", "start": start, "end": end}, ctx)

    def test_decimal_computation_is_bound_to_raw_fields(self):
        ctx = self.context()
        value = self.bars(ctx)
        result = self.invoke("calculate_market_metrics", {"result_id": value.result_id, "trade_date": "2024-04-30"}, ctx)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.data["previous_trade_date"], "2024-04-29")
        self.assertEqual(result.data["change_amount"], 1)
        self.assertEqual(result.data["change_percent"], 11.111111)
        self.assertEqual(result.snapshot_ids, [value.snapshot_id])
        self.assertEqual(result.data["operands"][0]["locator"], "$[2].close")
        self.assertEqual(result.data["finality"], "unknown")

    def test_missing_previous_close_requires_new_observation(self):
        ctx = self.context()
        first = self.bars(ctx, start="2024-04-30")
        args = {"result_id": first.result_id, "trade_date": "2024-04-30"}
        self.assertEqual(self.invoke("calculate_market_metrics", args, ctx).error.code, "MISSING_PREVIOUS_CLOSE")
        previous = self.bars(ctx, start="2024-04-26", end="2024-04-29")
        args["previous_result_id"] = previous.result_id
        self.assertEqual(self.invoke("calculate_market_metrics", args, ctx).data["previous_close"], 9)

    def test_non_adjacent_previous_bar_is_rejected(self):
        ctx = self.context()
        current = self.bars(ctx, start="2024-04-30")
        previous = self.bars(ctx, end="2024-04-26")
        result = self.invoke("calculate_market_metrics", {"result_id": current.result_id,
                             "trade_date": "2024-04-30", "previous_result_id": previous.result_id}, ctx)
        self.assertEqual(result.error.code, "NON_ADJACENT_PRICES")

    def test_changed_normalized_price_cannot_bypass_raw_evidence(self):
        ctx = self.context()
        value = self.bars(ctx)
        value.data["bars"][-1]["close"] = 999
        from finagent.data.evidence import write_json
        write_json(self.store.root / "results" / (value.result_id + ".json"), value.model_dump(mode="json"))
        result = self.invoke("calculate_market_metrics", {"result_id": value.result_id, "trade_date": "2024-04-30"}, ctx)
        self.assertEqual(result.error.code, "PRICE_EVIDENCE_MISMATCH")

    def test_cross_task_result_cannot_be_calculated(self):
        value = self.bars(self.context())
        result = self.invoke("calculate_market_metrics", {"result_id": value.result_id, "trade_date": "2024-04-30"}, self.context())
        self.assertEqual(result.error.code, "EVIDENCE_DENIED")

    def test_missing_target_date_is_not_replaced(self):
        ctx = self.context()
        value = self.bars(ctx)
        result = self.invoke("calculate_market_metrics", {"result_id": value.result_id, "trade_date": "2024-04-28"}, ctx)
        self.assertEqual(result.error.code, "TARGET_BAR_MISSING")

    def test_raw_price_arguments_cannot_be_supplied(self):
        ctx = self.context()
        result = self.invoke("calculate_market_metrics", {"result_id": "result_" + "a"*32, "price": 1, "previous_close": 2}, ctx)
        self.assertEqual(result.error.code, "VALIDATION_ERROR")

    def test_current_snapshot_calculates_but_not_for_history(self):
        ctx = ExecutionContext()
        self.handler = lambda r: httpx.Response(200, content=tencent_body(stamp=(now()-timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")))
        quote = self.invoke("get_quote", {"symbol": "600519"}, ctx)
        result = self.invoke("calculate_market_metrics", {"result_id": quote.result_id}, ctx)
        self.assertEqual(result.data["change_percent"], 11.111111)
        result = self.invoke("calculate_market_metrics", {"result_id": quote.result_id, "trade_date": "2024-04-30"}, ctx)
        self.assertEqual(result.error.code, "SNAPSHOT_NOT_HISTORICAL")

    def test_removed_skill_is_unavailable_without_blocking_calculation(self):
        ctx = self.context()
        self.assertNotIn("load_skill", {s["function"]["name"] for s in self.registry.schemas()})
        loaded = self.invoke("load_skill", {"name": "closing_review"}, ctx)
        self.assertEqual(loaded.error.code, "UNKNOWN_TOOL")
        self.assertFalse(ctx.required_tools)
        bars = self.bars(ctx)
        result = self.invoke("calculate_market_metrics", {"result_id": bars.result_id, "trade_date": "2024-04-30"}, ctx)
        self.assertEqual(result.data["change_percent"], 11.111111)

    def test_completed_metrics_requires_computation_and_citation(self):
        model = ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519"})),
                              lambda ms: final_reply([json.loads(m["content"])["snapshot_id"] for m in ms if m["role"] == "tool"]))
        # v3 workflow requirements are explicit, never inferred by root keywords.
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("查询涨跌幅", required_tools=("calculate_market_metrics",)))
        self.assertNotEqual(result.status, "completed")
        self.assertIn("calculate_market_metrics", result.reason)

    def test_export_without_active_task_is_rejected(self):
        result = self.invoke("export_research_result", {"status": "partial", "answer": "缺数据", "missing": ["数据"]}, self.context())
        self.assertEqual(result.error.code, "NO_ACTIVE_RUN")

    def test_export_rejects_path_and_arbitrary_references(self):
        ctx = self.context()
        result = self.invoke("export_research_result", {"status": "partial", "answer": "测试", "path": "../escape.md"}, ctx)
        self.assertEqual(result.error.code, "VALIDATION_ERROR")
        ctx.run_id = "a"*32
        ctx.run_dir = self.root / "runs" / ctx.run_id
        result = self.invoke("export_research_result", {"status": "partial", "answer": "测试", "evidence_ids": ["raw_"+"b"*64]}, ctx)
        self.assertEqual(result.error.code, "EXPORT_VALIDATION_FAILED")
        self.assertFalse((ctx.run_dir / "exports").exists())

    def test_run_exports_checkpoints_and_versioned_contracts(self):
        result = AgentLoop(ScriptedModel(final_reply()), self.registry, self.store).run(RunRequest("解释概念", require_data=False))
        state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
        body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(state["state"], "completed")
        self.assertEqual(body["task"]["goal"], "解释概念")
        self.assertEqual(len(body["tool_schema_sha256"]), 64)
        self.assertIsNone(body["cost"])

    def test_nonconsecutive_same_failures_trigger_windowed_no_progress(self):
        model = ScriptedModel(call_reply(("a", "unknown_a", {})), call_reply(("b", "unknown_b", {})),
                              call_reply(("c", "unknown_a", {})), call_reply(("d", "unknown_c", {})),
                              call_reply(("e", "unknown_a", {})), final_reply(status="partial"))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("合成不同反馈"))
        self.assertIn("NO_PROGRESS", result.reason)
        self.assertEqual(result.model_calls, 5)

    def test_dotenv_literal_loading_and_environment_precedence(self):
        config = self.root / "finagent.toml"
        config.write_text('[model]\nname="toml-model"\n', encoding="utf-8")
        (self.root / ".env").write_text('FINAGENT_BASE_URL="https://model.example/v1"\nFINAGENT_MODEL=dotenv-model # comment\nFINAGENT_API_KEY=\'literal-$()-key\'\n', encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True):
            settings = load_settings(config)
            self.assertEqual(settings.model.name, "dotenv-model")
            self.assertEqual(settings.model.api_key.get_secret_value(), "literal-$()-key")
            self.assertNotIn("literal-$()-key", settings.model_dump_json())
        with patch.dict("os.environ", {"FINAGENT_MODEL": "env-model"}):
            self.assertEqual(load_settings(config).model.name, "env-model")

    def test_cancel_during_tool_pairs_all_remaining_calls(self):
        self.registry.tools["get_quote"].handler = lambda a, c: (_ for _ in ()).throw(KeyboardInterrupt())
        model = ScriptedModel(call_reply(("a", "get_quote", {"symbol": "600519"}), ("b", "get_daily_bars", {"symbol": "600519", "start": "2024-04-26", "end": "2024-04-30"})))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("取消中途工具"))
        messages = json.loads((result.run_dir / "messages.json").read_text(encoding="utf-8"))
        self.assertEqual(result.status, "cancelled")
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["a", "b"])
        self.assertEqual(self.requests, [])

    def test_total_revenue_never_substitutes_operating_revenue(self):
        from .support import finance_body
        body = finance_body(raw_field="BIZTOTINCO")
        body["result"]["data"]["report_list"]["20240331"]["data"][0]["item_title"] = "营业总收入"
        self.handler = lambda r: httpx.Response(200, json=body)
        value = self.invoke("get_financial_statements", {"symbol": "600519", "statement": "income"}, self.context())
        metrics = value.data["reports"][0]["metrics"]
        self.assertIsNone(metrics["revenue"])
        self.assertEqual(metrics["total_revenue"]["raw_field"], "BIZTOTINCO")

    def test_budget_after_answering_clarification_has_no_stale_final_answer(self):
        model = ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519"})),
                              final_reply(status="needs_clarification", answer="补充期间？"))
        ctx = ExecutionContext()
        ctx.limits.model_calls = 2
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("期间澄清测试"), ctx, clarification_handler=lambda question: "本期")
        body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertIn("MODEL_BUDGET", result.reason)
        self.assertFalse(body["final_protocol_verified"])
        self.assertIsNone(body["final_answer"])
