"""Synthetic regressions for stock/index identity, units, evidence and tool paths."""
from datetime import date, datetime
import json

import httpx

from finagent.agent import AgentLoop, RunRequest
from finagent.data.evidence import write_json
from finagent.data.symbols import INDICES, normalize_symbol, resolve, vendor_symbol
from finagent.errors import AgentError
from finagent.finish import FinalAnswer, finish_errors
from finagent.market import MarketImport, TradingCalendar, import_market
from finagent.models import ModelReply
from finagent.runtime import ExecutionContext, TZ, now
from finagent.session import Conversation

from .support import ServiceCase, ScriptedModel, call_reply, sina_body, tencent_body


class IndexTests(ServiceCase):
    def test_unmatched_index_is_not_described_as_nonexistent_stock(self):
        self.handler = lambda request: httpx.Response(200, json={"QuotationCodeTable": {"Status": 0, "TotalCount": 0, "Data": []}})
        context = ExecutionContext()
        value = self.registry.execute("resolve_entity", {"query": "尚未接入行业指数"}, context)
        self.assertIsNone(value.error)
        self.assertIsNone(value.data["selected"])
        self.assertIn("其他指数可能尚未接入", context.clarification)
        self.assertNotIn("公司名或股票简称", context.clarification)

    def test_csi_bank_index_and_sector_proxy_resolve_without_stock_search(self):
        for query in ("中证银行指数", "中证银行", "399986.SZ", "399986", "银行板块", "银行行业", "银行业", "银行"):
            with self.subTest(query=query):
                context = ExecutionContext()
                value = self.registry.execute("resolve_entity", {"query": query}, context)
                self.assertIsNone(value.error)
                selected = value.data["selected"]
                self.assertEqual(selected["symbol"], "399986.SZ")
                self.assertEqual(selected["instrument_type"], "index")
                self.assertEqual(context.symbol, "399986.SZ")
                if query in {"银行板块", "银行行业", "银行业", "银行"}:
                    self.assertEqual(selected["match_type"], "sector_proxy")
                    self.assertEqual(selected["requested_query"], query)
                    self.assertEqual(selected["requested_sector"], "银行")
                    self.assertIn("代表口径", selected["proxy_note"])
                    self.assertTrue(any("代表口径" in warning for warning in value.warnings))
        self.assertEqual(self.requests, [])

    def test_bank_proxy_does_not_silently_include_etfs_or_other_classifications(self):
        for query in ("银行ETF", "申万银行", "东财银行", "银行全部成分股"):
            with self.subTest(query=query):
                self.assertEqual(resolve(query), [])
        self.assertEqual(normalize_symbol("399986"), "399986.SZ")
        with self.assertRaises(AgentError):
            normalize_symbol("399986.SH")

    def test_bank_snapshot_and_calculation_keep_index_scope_and_time(self):
        context = ExecutionContext()
        self.handler = lambda request: httpx.Response(200, content=tencent_body(code="399986", vendor="sz399986", stamp=context.as_of.strftime("%Y%m%d%H%M%S")))
        value = self.invoke("get_quote", {"symbol": "399986.SZ"}, context)
        self.assertEqual(value.units["price"], "index_point")
        self.assertIn("银行板块代表口径", value.data["index_scope"])
        self.assertEqual(value.data_time, context.as_of.replace(microsecond=0).isoformat())
        calculated = self.invoke("calculate_market_metrics", {"result_id": value.result_id}, context)
        self.assertEqual(calculated.data["change_amount"], 1)
        self.assertEqual(calculated.units["change_amount"], "index_point")
        self.assertEqual(self.requests[-1].url.params["q"], "sz399986")

    def invoke(self, tool, args, ctx):
        value = self.registry.execute(tool, args, ctx)
        self.assertIsNone(value.error, value.error)
        value = self.store.save_result(value)
        ctx.result_tools[value.result_id] = tool
        ctx.evidence_ids.add(value.result_id)
        if self.registry.tools[tool].perception:
            ctx.perception_results.add(value.result_id)
        return value

    def historical_context(self, symbol="000001.SH"):
        return ExecutionContext(symbol=symbol, as_of=datetime(2024, 5, 1, tzinfo=TZ))

    def market_rows(self):
        sessions = TradingCalendar().calendar.sessions_in_range("2024-01-02", "2024-04-30")
        return [{"day": str(s.date()), "open": 10 + i, "high": 11 + i,
                 "low": 9 + i, "close": 10 + i, "volume": 100 + i}
                for i, s in enumerate(sessions)]

    def test_explicit_index_identity_and_bare_stock_codes(self):
        for symbol in INDICES:
            code, exchange = symbol.split(".")
            self.assertEqual(normalize_symbol(symbol.lower()), symbol)
            self.assertEqual(normalize_symbol(exchange + code), symbol)
            self.assertEqual(vendor_symbol(symbol), exchange.lower() + code)
        self.assertEqual(normalize_symbol("000001"), "000001.SZ")
        self.assertEqual(normalize_symbol("000300"), "000300.SZ")
        self.assertEqual(normalize_symbol("399001"), "399001.SZ")
        for invalid in ("600519.SZ", "000001.SH.SZ", "SH000001.SZ", "000999.SH", "399999.SZ"):
            with self.assertRaises(AgentError):
                normalize_symbol(invalid)

    def test_names_and_aliases_resolve_locally_with_custom_stock_catalog(self):
        catalog = self.root / "custom.json"
        catalog.write_text("[]")
        self.settings.securities_file = catalog
        for symbol, entry in INDICES.items():
            for name in [entry["name"], *entry["aliases"]]:
                ctx = ExecutionContext()
                result = self.invoke("resolve_entity", {"query": name}, ctx)
                self.assertEqual(result.data["selected"]["symbol"], symbol)
                self.assertEqual(result.data["selected"]["instrument_type"], "index")
                self.assertEqual(ctx.symbol, symbol)
        self.assertEqual(self.requests, [])

    def test_switching_from_stock_to_index_in_conversation(self):
        conversation = Conversation(self.store)
        conversation.state["symbol"] = "600519.SH"
        for goal in ("那沪指今天怎么样", "上证指数历史行情", "查000001.SH行情"):
            ctx = conversation.context(goal, self.settings.limits, symbol="600519.SH")
            self.assertEqual(ctx.symbol, "000001.SH")

    def test_tencent_quotes_use_points_and_preserve_raw_index_volume(self):
        for symbol in INDICES:
            ctx = ExecutionContext()
            vendor = vendor_symbol(symbol)
            self.handler = lambda r: httpx.Response(200, content=tencent_body(
                code=symbol[:6], vendor=vendor, stamp=ctx.as_of.strftime("%Y%m%d%H%M%S")))
            result = self.invoke("get_quote", {"symbol": symbol}, ctx)
            self.assertEqual(self.requests[-1].url.params["q"], vendor)
            self.assertEqual(result.units["price"], "index_point")
            self.assertEqual(result.units["volume_shares"], "vendor_index_volume")
            self.assertEqual(result.data["volume_shares"], 2)
            self.assertIsNone(result.data["pe_ttm"])
            self.assertIsNone(result.data["pb"])
            self.assertTrue(self.store.has_raw(result.snapshot_id))

    def test_sina_quote_fallback_preserves_index_units(self):
        def respond(request):
            if request.url.host == "qt.gtimg.cn":
                return httpx.Response(503)
            return httpx.Response(200, content=sina_body(vendor="sh000001"))
        self.handler = respond
        result = self.invoke("get_quote", {"symbol": "000001.SH"}, ExecutionContext())
        self.assertEqual(result.source, "sina_quote")
        self.assertEqual(result.units["price"], "index_point")
        self.assertEqual(result.data["volume_shares"], 200)
        self.assertEqual(self.requests[-1].url.params["list"], "sh000001")

    def test_index_quote_and_change_facts_validate_in_points(self):
        ctx = ExecutionContext()
        self.handler = lambda r: httpx.Response(200, content=tencent_body(
            code="000001", vendor="sh000001", stamp=ctx.as_of.strftime("%Y%m%d%H%M%S")))
        quote = self.invoke("get_quote", {"symbol": "000001.SH"}, ctx)
        calculation = self.invoke("calculate_market_metrics", {"result_id": quote.result_id}, ctx)
        self.assertEqual(calculation.units["change_amount"], "index_point")
        final = FinalAnswer(status="completed", answer="合成指数快照，最终收盘状态未知",
                            evidence_ids=[quote.snapshot_id], calculation_ids=[calculation.result_id], facts=[
                                {"reference": quote.result_id, "field": "price", "value": 10, "unit": "index_point"},
                                {"reference": quote.result_id, "field": "volume_shares", "value": 2, "unit": "vendor_index_volume"},
                                {"reference": calculation.result_id, "field": "change_amount", "value": 1, "unit": "index_point"}])
        self.assertEqual(finish_errors(final, ctx, self.store), [])
        final.facts[0]["unit"] = "CNY/share"
        self.assertTrue(finish_errors(final, ctx, self.store))

    def test_index_daily_calculation_and_cross_instrument_rejection(self):
        self.handler = lambda r: httpx.Response(200, json=self.market_rows())
        ctx = self.historical_context()
        daily = self.invoke("get_daily_bars", {"symbol": "000001.SH", "start": "2024-04-29", "end": "2024-04-30"}, ctx)
        self.assertEqual(daily.units["price"], "index_point")
        self.assertEqual(daily.units["volume_shares"], "vendor_index_volume")
        result = self.invoke("calculate_market_metrics", {"result_id": daily.result_id, "trade_date": "2024-04-30"}, ctx)
        self.assertEqual(result.units["price"], "index_point")
        self.assertEqual(result.data["change_amount"], 1)
        stock = self.invoke("get_daily_bars", {"symbol": "000001.SZ", "start": "2024-04-29", "end": "2024-04-30"}, ctx)
        rejected = self.registry.execute("calculate_market_metrics", {
            "result_id": daily.result_id, "trade_date": "2024-04-30", "previous_result_id": stock.result_id}, ctx)
        self.assertEqual(rejected.error.code, "INCOMPARABLE_PRICES")

    def test_stock_only_tools_reject_index_before_network_or_focus_change(self):
        ctx = ExecutionContext(symbol="600519.SH")
        for tool, args in (("get_financial_statements", {}), ("get_company_profile", {}),
                           ("get_stock_news", {"start": "2024-04-01", "end": "2024-04-30"})):
            value = self.registry.execute(tool, {"symbol": "000001.SH", **args}, ctx)
            self.assertEqual(value.error.code, "UNSUPPORTED_INSTRUMENT")
            self.assertEqual(ctx.symbol, "600519.SH")
        self.assertEqual(self.requests, [])

    def test_selectable_benchmarks_preserve_focus_and_default(self):
        self.handler = lambda r: httpx.Response(200, json=self.market_rows())
        ctx = self.historical_context("600519.SH")
        for symbol in INDICES:
            result = self.invoke("get_benchmark_data", {"trade_date": "2024-04-30", "symbol": symbol}, ctx)
            self.assertEqual(result.symbol, symbol)
            self.assertEqual(result.units["price"], "index_point")
            self.assertEqual(self.requests[-1].url.params["symbol"], vendor_symbol(symbol))
            self.assertEqual(ctx.symbol, "600519.SH")
        default = self.invoke("get_benchmark_data", {"trade_date": "2024-04-30"}, ctx)
        self.assertEqual(default.symbol, "000300.SH")
        count = len(self.requests)
        for symbol in ("600519.SH", "000001", "000001.SZ"):
            rejected = self.registry.execute("get_benchmark_data", {"trade_date": "2024-04-30", "symbol": symbol}, ctx)
            self.assertEqual(rejected.error.code, "VALIDATION_ERROR")
        self.assertEqual(len(self.requests), count)

    def test_index_closing_review_and_technical_indicators(self):
        self.handler = lambda r: httpx.Response(200, json=self.market_rows())
        ctx = self.historical_context()
        series = self.invoke("get_market_data", {"trade_date": "2024-04-30"}, ctx)
        benchmark = self.invoke("get_benchmark_data", {"trade_date": "2024-04-30", "symbol": "399001.SZ"}, ctx)
        review = self.invoke("calculate_closing_review", {
            "result_id": series.result_id, "benchmark_result_id": benchmark.result_id}, ctx)
        self.assertEqual(review.symbol, "000001.SH")
        self.assertEqual(review.units["price"], "index_point")
        self.assertEqual(float(review.data["metrics"]["benchmark_difference_pp"]), 0)
        self.assertIn("5", review.data["metrics"]["moving_averages"])
        self.assertFalse(review.data["complete"])
        final = FinalAnswer(status="partial", answer="合成指数均线",
                            evidence_ids=review.snapshot_ids, calculation_ids=[review.result_id], facts=[{
                                "reference": review.result_id, "field": "moving_averages.5",
                                "value": review.data["metrics"]["moving_averages"]["5"], "unit": "index_point"}])
        self.assertEqual(finish_errors(final, ctx, self.store), [])
        divergence = self.invoke("calculate_technical_divergence", {"result_id": series.result_id}, ctx)
        self.assertEqual(divergence.data["kind"], "technical_divergence")
        self.assertIn("latest_indicators", divergence.data)
        # Relabeling an index series as a stock unit must still fail verification.
        series.units["price"] = "CNY/share"
        write_json(self.root / "results" / f"{series.result_id}.json", series.model_dump(mode="json"))
        rejected = self.registry.execute("calculate_closing_review", {"result_id": series.result_id}, ctx)
        self.assertEqual(rejected.error.code, "DATA_CONFLICT")

    def test_index_replay_for_focus_and_benchmark(self):
        path = self.root / "index.json"
        path.write_text(json.dumps(self.market_rows()))
        for symbol, benchmark in (("000001.SH", False), ("399001.SZ", True)):
            imported = import_market(path, MarketImport(instrument=symbol, benchmark=benchmark,
                source="SYNTHETIC", source_url="https://example.test/index", fetched_at=now(), synthetic=True), self.store)
            self.assertEqual(imported.units["price"], "index_point")
        ctx = self.historical_context()
        self.invoke("get_market_data", {"trade_date": "2024-04-30", "mode": "replay"}, ctx)
        self.invoke("get_benchmark_data", {"trade_date": "2024-04-30", "mode": "replay", "symbol": "399001.SZ"}, ctx)
        self.assertEqual(self.requests, [])

    def test_agent_resolves_index_and_queries_existing_quote_tool(self):
        ctx = ExecutionContext()
        self.handler = lambda r: httpx.Response(200, content=tencent_body(
            code="000001", vendor="sh000001", stamp=ctx.as_of.strftime("%Y%m%d%H%M%S")))
        def quote(messages):
            resolved = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
            return call_reply(("quote", "get_quote", {"symbol": resolved["data"]["selected"]["symbol"]}))
        model = ScriptedModel(call_reply(("resolve", "resolve_entity", {"query": "沪指"})), quote,
                              ModelReply({"role": "assistant", "content": "已获取合成上证指数快照。"}))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("查上证指数行情"), ctx)
        self.assertEqual(result.status, "completed")
        self.assertEqual((result.tool_calls, ctx.symbol), (2, "000001.SH"))
        self.assertEqual(len(self.requests), 1)

    def test_v3_session_resolves_index_and_persists_focus(self):
        from finagent.config import HarnessSettings
        from finagent.harness.host import SessionHost
        self.settings.harness = HarnessSettings(workspace=self.root, auto_memory=False)
        self.handler = lambda r: httpx.Response(200, content=tencent_body(
            code="000001", vendor="sh000001", stamp=now().strftime("%Y%m%d%H%M%S")))
        def quote(messages):
            resolved = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
            self.assertEqual(resolved["data"]["selected"]["symbol"], "000001.SH")
            return call_reply(("quote", "get_quote", {"symbol": "000001.SH"}))
        model = ScriptedModel(call_reply(("resolve", "resolve_entity", {"query": "上证指数"})), quote,
                              ModelReply({"role": "assistant", "content": "已获取合成上证指数快照。"}))
        host = SessionHost(self.settings, model=model, service=self.service)
        try:
            result = host.submit("查上证指数行情")
            self.assertEqual(result.status, "completed")
            self.assertEqual(host.state["focus"], "000001.SH")
            self.assertEqual(host.state["last_ledger"]["operations"]["source"], 1)
        finally:
            host.close()
