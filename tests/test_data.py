from datetime import date, datetime, timedelta
import json
from pathlib import Path
from time import monotonic

import httpx

from finagent.config import load_settings
from finagent.data.symbols import normalize_symbol
from finagent.errors import AgentError
from finagent.runtime import ExecutionContext, TZ, now

from .support import ServiceCase, finance_body, sina_body, tencent_body


class DataTests(ServiceCase):
    def test_01_symbol_aliases_and_market_mismatch(self):
        self.assertEqual(normalize_symbol("SH600519"), "600519.SH")
        self.assertEqual(normalize_symbol("000333.SZ"), "000333.SZ")
        for bad in ["00700.HK", "HK00700", "BJ832000", "600519.SZ", "../../secret"]:
            with self.assertRaises(AgentError):
                normalize_symbol(bad)

    def test_02_invalid_input_never_reaches_network(self):
        for name, params in [("get_quote", {"symbol": "00700.HK"}),
                             ("get_quote", {"symbol": "600519", "url": "https://evil.test"}),
                             ("get_daily_bars", {"symbol": "600519", "start": "2026-09-30", "end": "2026-09-01"}),
                             ("get_quote", "not json")]:
            value = self.registry.execute(name, params, ExecutionContext())
            self.assertEqual(value.status, "error")
        self.assertEqual(self.requests, [])

    def test_03_quote_normalizes_units_keeps_missing_null(self):
        value = self.registry.execute("get_quote", {"symbol": "600519"}, ExecutionContext())
        self.assertEqual(value.data["volume_shares"], 200)
        self.assertEqual(value.data["amount_cny"], 3000)
        self.assertIsNone(value.data["pe_ttm"])
        self.assertTrue(value.snapshot_id)
        self.assertTrue(self.store.read(value.snapshot_id)["text"].startswith('v_sh600519'))

    def test_04_quote_identity_error_falls_back_to_sina(self):
        self.handler = lambda r: httpx.Response(200, content=tencent_body(code="600000") if r.url.host == "qt.gtimg.cn" else sina_body())
        value = self.registry.execute("get_quote", {"symbol": "600519"}, ExecutionContext())
        self.assertEqual(value.source, "sina_quote")
        self.assertTrue(any("IDENTITY_MISMATCH" in w for w in value.warnings))
        self.assertEqual(len(self.requests), 2)

    def test_05_wrong_sina_variable_cannot_pass(self):
        self.handler = lambda r: httpx.Response(200, content=sina_body("sz000001"))
        value = self.registry.execute("get_quote", {"symbol": "600519", "source": "sina"}, ExecutionContext())
        self.assertEqual(value.status, "error")

    def test_06_stale_cache_preserves_fetch_time(self):
        first = self.registry.execute("get_quote", {"symbol": "600519"}, ExecutionContext())
        key = next(iter(self.service._cache))
        self.service._cache[key] = (monotonic() - 60, self.service._cache[key][1])
        self.handler = lambda r: httpx.Response(503)
        stale = self.registry.execute("get_quote", {"symbol": "600519"}, ExecutionContext())
        self.assertTrue(stale.stale)
        self.assertEqual(stale.status, "partial")
        self.assertEqual(stale.fetched_at, first.fetched_at)
        self.assertEqual(stale.snapshot_id, first.snapshot_id)

    def test_07_cache_hit_has_no_extra_request(self):
        first = self.registry.execute("get_quote", {"symbol": "600519"}, ExecutionContext())
        second = self.registry.execute("get_quote", {"symbol": "600519"}, ExecutionContext())
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(len(self.requests), 1)

    def test_08_historical_quote_rejected_before_network(self):
        context = ExecutionContext(as_of=datetime(2024, 5, 1, tzinfo=TZ))
        result = self.registry.execute("get_quote", {"symbol": "600519"}, context)
        self.assertEqual(result.error.code, "SNAPSHOT_NOT_HISTORICAL")
        self.assertEqual(self.requests, [])

    def test_09_daily_ohlcv_and_conflicts_rejected(self):
        for rows in [[{"day": "2024-04-02", "open": "10", "close": "9", "high": "8", "low": "7", "volume": "1"}],
                     [{"day": "2024-04-02", "open": "9", "close": "10", "high": "11", "low": "8", "volume": "-1"}]]:
            self.handler = lambda r, data=rows: httpx.Response(200, json=data)
            value = self.registry.execute("get_daily_bars", {"symbol": "600519", "start": "2024-04-01", "end": "2024-04-30"}, ExecutionContext())
            self.assertEqual(value.status, "error")

    def test_10_daily_filters_sorts_and_excludes_current_day(self):
        end = now().date()
        days = [end, end - timedelta(days=2), end - timedelta(days=1)]
        self.handler = lambda r: httpx.Response(200, json=[{"day": str(d), "open": "9", "close": "10", "high": "11", "low": "8", "volume": "100"} for d in days])
        result = self.registry.execute("get_daily_bars", {"symbol": "600519", "start": str(days[1]), "end": str(end)}, ExecutionContext())
        self.assertEqual([b["date"] for b in result.data["bars"]], [str(days[1]), str(days[2])])
        self.assertTrue(all(b["finality"] == "unknown" for b in result.data["bars"]))

    def test_11_financial_cutoff_date_precision_and_field_locator(self):
        self.handler = lambda r: httpx.Response(200, json=finance_body())
        params = {"symbol": "600519", "statement": "income", "report_period": "2024-03-31"}
        same_day = self.registry.execute("get_financial_statements", params, ExecutionContext(as_of=datetime(2024, 4, 30, 23, 59, 59, tzinfo=TZ)))
        self.assertEqual(same_day.status, "empty")
        next_day = self.registry.execute("get_financial_statements", params, ExecutionContext(as_of=datetime(2024, 5, 1, tzinfo=TZ)))
        metric = next_day.data["reports"][0]["metrics"]["revenue"]
        self.assertEqual(metric["value"], 123.45)
        self.assertIn("item_value", metric["locator"])
        self.assertIsNone(next_day.data["reports"][0]["metrics"]["net_profit"])

    def test_12_unknown_financial_publication_time_excluded(self):
        self.handler = lambda r: httpx.Response(200, json=finance_body(published=""))
        result = self.registry.execute("get_financial_statements", {"symbol": "600519"}, ExecutionContext())
        self.assertEqual(result.status, "empty")

    def test_13_changed_finance_schema_is_error_not_empty(self):
        self.handler = lambda r: httpx.Response(200, json={"result": {"data": {"report_list": []}}})
        result = self.registry.execute("get_financial_statements", {"symbol": "600519"}, ExecutionContext())
        self.assertEqual(result.status, "error")

    def test_14_news_filters_unknown_and_after_cutoff(self):
        items = [{"title": "<em>合成新闻</em>", "content": "<b>摘要</b>", "date": d, "url": "https://example.test/news", "mediaName": "合成媒体"}
                 for d in ["2024-04-29 12:00:00", "2024-05-02 12:00:00", "unknown"]]
        self.handler = lambda r: httpx.Response(200, text="callback(" + json.dumps({"result": {"cmsArticleWebOld": items}}) + ");")
        value = self.registry.execute("get_stock_news", {"symbol": "600519", "start": "2024-04-01", "end": "2024-05-01", "source": "eastmoney"}, ExecutionContext(as_of=datetime(2024, 5, 1, tzinfo=TZ)))
        self.assertEqual(len(value.data["articles"]), 1)
        self.assertEqual(value.data["articles"][0]["title"], "合成新闻")
        self.assertEqual(value.data["excluded_count"], 2)

    def test_15_evidence_traversal_and_cross_task_read_denied(self):
        value = self.registry.execute("get_quote", {"symbol": "600519"}, ExecutionContext())
        for key in ["../../config", value.snapshot_id]:
            result = self.registry.execute("read_evidence", {"evidence_id": key}, ExecutionContext())
            self.assertEqual(result.status, "error")

    def test_16_switching_security_is_allowed_but_cancellation_blocks_network(self):
        context = ExecutionContext(symbol="000333.SZ")
        value = self.registry.execute("get_quote", {"symbol": "600519"}, context)
        self.assertIsNone(value.error)
        self.assertEqual(value.symbol, "600519.SH")
        self.assertEqual(context.symbol, "600519.SH")
        self.assertEqual(len(self.requests), 1)
        request_count = len(self.requests)
        context = ExecutionContext()
        context.cancelled.set()
        value = self.registry.execute("get_quote", {"symbol": "600519"}, context)
        self.assertEqual(value.error.code, "CANCELLED")
        self.assertEqual(len(self.requests), request_count)

    def test_17_config_paths_independent_of_cwd(self):
        config = self.root / "nested" / "agent.toml"
        config.parent.mkdir()
        config.write_text('data_dir="../data"', encoding="utf-8")
        self.assertEqual(load_settings(config).data_dir, self.root / "data")

    def test_18_no_auth_retries_and_finite_network_retries(self):
        self.http.retries = 2
        self.handler = lambda r: httpx.Response(403)
        self.registry.execute("get_quote", {"symbol": "600519", "source": "tencent"}, ExecutionContext())
        self.assertEqual(len(self.requests), 1)
        self.requests.clear()
        self.handler = lambda r: httpx.Response(503)
        self.registry.execute("get_quote", {"symbol": "600519", "source": "tencent"}, ExecutionContext())
        self.assertEqual(len(self.requests), 3)

    def test_19_source_request_budget_blocks_fallback(self):
        self.handler = lambda r: httpx.Response(503)
        context = ExecutionContext()
        context.limits.source_requests = 1
        result = self.registry.execute("get_quote", {"symbol": "600519"}, context)
        self.assertEqual(result.error.code, "SOURCE_BUDGET")
        self.assertEqual(len(self.requests), 1)

    def test_20_name_resolution_is_explicit_about_limited_catalog(self):
        context = ExecutionContext()
        result = self.registry.execute("resolve_entity", {"query": "茅台"}, context)
        self.assertEqual(result.data["selected"]["symbol"], "600519.SH")
        self.assertFalse(result.data["selected"]["identity_verified"])
        self.assertEqual(self.requests, [])
