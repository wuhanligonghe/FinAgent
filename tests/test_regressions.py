from datetime import datetime, timedelta
import json

import httpx
from pydantic import SecretStr

from finagent.agent import AgentLoop, RunRequest
from finagent.config import ModelSettings
from finagent.errors import AgentError
from finagent.models import CompatibleChatModel
from finagent.runtime import ExecutionContext, TZ, now
from finagent.config import StrictModel
from finagent.tools.registry import Tool

from .support import ServiceCase, ScriptedModel, call_reply, final_reply, finance_body, tencent_body


class RegressionTests(ServiceCase):
    def test_cashflow_field_observed_in_real_source_is_mapped(self):
        body = finance_body(raw_field="MANANETR")
        body["result"]["data"]["report_list"]["20240331"]["data"][0]["item_title"] = "经营活动产生的现金流量净额"
        self.handler = lambda r: httpx.Response(200, json=body)
        result = self.registry.execute("get_financial_statements", {"symbol": "600519", "statement": "cashflow"}, ExecutionContext())
        metric = result.data["reports"][0]["metrics"]["operating_cashflow"]
        self.assertEqual(metric["raw_field"], "MANANETR")
        self.assertEqual(metric["value"], 123.45)  # Synthetic value, not a market observation.

    def test_financial_label_conflict_and_unknown_currency_rejected(self):
        for changed in ["label", "currency", "duplicate"]:
            body = finance_body()
            report = body["result"]["data"]["report_list"]["20240331"]
            if changed == "label":
                report["data"][0]["item_title"] = "经营费用"
            elif changed == "currency":
                report["rCurrency"] = "unknown"
            else:
                report["data"].append(report["data"][0])
            self.handler = lambda r, data=body: httpx.Response(200, json=data)
            self.service._cache.clear()
            value = self.registry.execute("get_financial_statements", {"symbol": "600519"}, ExecutionContext())
            self.assertEqual(value.status, "error", changed)

    def test_sina_news_pairs_each_timestamp_with_its_own_link(self):
        html = '''<div class="datelist"><ul>
        2024-04-29&nbsp;12:00&nbsp;<a target='_blank' href='https://example.test/first'>合成新闻一</a><br>
        2024-04-28&nbsp;09:30&nbsp;<a target='_blank' href='https://example.test/second'>合成新闻二</a>
        </ul></div>'''
        self.handler = lambda r: httpx.Response(200, content=html.encode("gb18030"))
        value = self.registry.execute("get_stock_news", {"symbol": "600519", "start": "2024-04-01", "end": "2024-04-30", "source": "sina"},
                                      ExecutionContext(as_of=datetime(2024, 5, 1, tzinfo=TZ)))
        self.assertEqual([a["published_at"] for a in value.data["articles"]], ["2024-04-29T12:00:00+08:00", "2024-04-28T09:30:00+08:00"])

    def test_conflicting_duplicate_daily_bars_fail(self):
        rows = [{"day": "2024-04-01", "open": "9", "close": close, "high": "12", "low": "8", "volume": "100"} for close in ["10", "11"]]
        self.handler = lambda r: httpx.Response(200, json=rows)
        value = self.registry.execute("get_daily_bars", {"symbol": "600519", "start": "2024-04-01", "end": "2024-04-02"}, ExecutionContext())
        self.assertEqual(value.status, "error")
        self.assertIn("CONFLICTING_BARS", value.error.message)

    def test_future_quote_is_not_used_as_cutoff_evidence(self):
        stamp = (now() + timedelta(hours=1)).strftime("%Y%m%d%H%M%S")
        self.handler = lambda r: httpx.Response(200, content=tencent_body(stamp=stamp))
        value = self.registry.execute("get_quote", {"symbol": "600519", "source": "tencent"}, ExecutionContext())
        self.assertEqual(value.status, "error")
        self.assertIn("AFTER_CUTOFF", value.error.message)

    def test_turn_rejections_count_as_logical_attempts(self):
        model = ScriptedModel(call_reply(("a", "unknown", {}), ("b", "unknown", {})), final_reply(status="partial"))
        context = ExecutionContext()
        context.limits.tools_per_turn = 1
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("合成测试"), context)
        self.assertEqual(result.tool_calls, 2)
        messages = json.loads((result.run_dir / "messages.json").read_text(encoding="utf-8"))
        outputs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.assertEqual(outputs[1]["error"]["code"], "TURN_TOOL_LIMIT")

    def test_keyboard_interrupt_exports_cancelled_state(self):
        class InterruptModel:
            def complete(self, *args):
                raise KeyboardInterrupt()
        result = AgentLoop(InterruptModel(), self.registry, self.store).run(RunRequest("取消测试"))
        self.assertEqual(result.status, "cancelled")
        self.assertTrue((result.run_dir / "result.json").exists())

    def test_duplicate_model_call_id_rejected_before_tools(self):
        model = ScriptedModel(call_reply(("same", "get_quote", {"symbol": "600519"}), ("same", "get_quote", {"symbol": "600519"})))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("协议测试"))
        self.assertIn("MODEL_PROTOCOL_ERROR", result.reason)
        self.assertEqual(self.requests, [])

    def test_wall_budget_prevents_model_start(self):
        model = ScriptedModel(final_reply())
        context = ExecutionContext()
        context.started -= 1000
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("超时测试"), context)
        self.assertIn("TIME_BUDGET", result.reason)
        self.assertEqual(result.model_calls, 0)

    def test_http_failure_raw_response_remains_readable(self):
        self.handler = lambda r: httpx.Response(403, text="synthetic denied response")
        context = ExecutionContext()
        self.registry.execute("get_quote", {"symbol": "600519", "source": "tencent"}, context)
        self.assertEqual(context.source_events[0]["status"], 403)
        key = context.source_events[0]["snapshot_id"]
        self.assertEqual(self.store.read(key)["text"], "synthetic denied response")

    def test_model_http_error_does_not_expose_body_or_key(self):
        config = ModelSettings(base_url="https://model.example/v1", name="test", api_key=SecretStr("synthetic-secret"))
        model = CompatibleChatModel(config, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="synthetic-secret"))))
        with self.assertRaises(AgentError) as caught:
            model.complete([], [], 1)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertFalse(caught.exception.retryable)
        model.close()

    def test_empty_news_stays_empty_and_has_coverage_warning(self):
        self.handler = lambda r: httpx.Response(200, text='callback({"result":{"cmsArticleWebOld":[]}});')
        context = ExecutionContext(as_of=datetime(2024, 5, 1, tzinfo=TZ))
        value = self.registry.execute("get_stock_news", {"symbol": "600519", "start": "2024-04-01", "end": "2024-04-30", "source": "eastmoney"}, context)
        self.assertEqual(value.status, "empty")
        self.assertIn("空结果不代表", " ".join(value.warnings))

    def test_proxy_failure_uses_fallback_instead_of_internal_error(self):
        def handler(request):
            if request.url.host == "qt.gtimg.cn":
                raise httpx.ProxyError("synthetic proxy failure")
            from .support import sina_body
            return httpx.Response(200, content=sina_body())
        self.handler = handler
        value = self.registry.execute("get_quote", {"symbol": "600519"}, ExecutionContext())
        self.assertEqual(value.source, "sina_quote")
        self.assertTrue(any("SOURCE_UNAVAILABLE" in w for w in value.warnings))

    def test_invalid_nested_numeric_output_returns_paired_error(self):
        class NoArgs(StrictModel):
            pass
        self.registry.register(Tool("bad_output", "Synthetic invalid source", NoArgs,
                                    lambda a, c: {"status": "ok", "data": {"value": float("nan")}}))
        model = ScriptedModel(call_reply(("a", "bad_output", {})), final_reply(status="partial"))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("输出校验测试"))
        messages = json.loads((result.run_dir / "messages.json").read_text(encoding="utf-8"))
        outputs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.assertEqual(outputs[0]["error"]["code"], "VALIDATION_ERROR")

    def test_no_progress_stops_remaining_calls_in_same_turn(self):
        model = ScriptedModel(call_reply(("a", "unknown", {}), ("b", "unknown", {}),
                                         ("c", "unknown", {}), ("d", "get_quote", {"symbol": "600519"})))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("同轮停止测试"))
        self.assertIn("NO_PROGRESS", result.reason)
        self.assertEqual(self.requests, [])
        messages = json.loads((result.run_dir / "messages.json").read_text(encoding="utf-8"))
        outputs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.assertEqual(len(outputs), 4)
        self.assertEqual(outputs[-1]["error"]["code"], "RUN_STOPPED")
