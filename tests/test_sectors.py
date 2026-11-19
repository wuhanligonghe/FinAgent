"""Synthetic industry responses, never market observations or live rankings."""
from datetime import datetime, timedelta
import json
from time import monotonic
from unittest.mock import patch

import httpx

from finagent.config import Settings
from finagent.data.service import DataService
from finagent.runtime import ExecutionContext, TZ, now
from tests.support import ServiceCase


def row(code, name, change, count="3"):
    return ",".join([code, name, count, "10", "1", change, "100", "1000", "sh600519", "1", "10", "1", "合成个股"])


def response(rows=None):
    rows = rows if rows is not None else {"new_a": row("new_a", "合成行业甲", "2"), "new_b": row("new_b", "合成行业乙", "10")}
    return ("var S_Finance_bankuai_sinaindustry = " + json.dumps(rows, ensure_ascii=False) + ";").encode("gbk")


class SectorTests(ServiceCase):
    def handler(self, request):
        return httpx.Response(200, content=response())

    def ranking(self, context=None, **args):
        return self.registry.execute("get_sector_ranking", args, context or ExecutionContext())

    def test_industry_ranking_does_not_require_security_or_benchmark(self):
        context = ExecutionContext()
        value = self.ranking(context, limit=1)
        self.assertEqual(value.status, "partial")
        self.assertEqual(value.data["rows"][0]["code"], "new_b")
        self.assertIsNone(context.symbol)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/q/view/newSinaHy.php")

    def test_ranking_preserves_missing_time_and_source_classification(self):
        value = self.ranking()
        self.assertIsNone(value.data_time)
        self.assertEqual(value.data["data_time_status"], "unknown")
        self.assertEqual(value.data["classification"], "sina_industry")
        self.assertFalse(value.data["realtime_verified"])
        self.assertFalse(value.data["market_coverage_verified"])
        self.assertEqual(value.units["average_price_cny"], "CNY")
        self.assertIsNotNone(value.fetched_at)
        self.assertTrue(self.store.has_raw(value.snapshot_id))
        self.assertIn("S_Finance_bankuai", self.store.read(value.snapshot_id)["text"])

    def test_missing_percentage_is_not_zero_or_silently_ranked(self):
        self.handler = lambda request: httpx.Response(200, content=response({"new_a": row("new_a", "合成行业甲", "--"), "new_b": row("new_b", "合成行业乙", "-2")}))
        value = self.ranking()
        self.assertEqual(value.data["ranked_count"], 1)
        self.assertFalse(value.data["ranking_complete_for_returned_set"])
        self.assertEqual(value.data["rows"][0]["vendor_change_percent"], "-2")
        self.assertEqual(len(value.data["unranked"]), 1)

    def test_cache_limit_does_not_discard_other_ranked_rows(self):
        self.assertEqual(len(self.ranking(limit=1).data["rows"]), 1)
        value = self.ranking(limit=2)
        self.assertEqual(len(value.data["rows"]), 2)
        self.assertTrue(value.cache_hit)
        self.assertEqual(len(self.requests), 1)

    def test_historical_and_explicit_cutoff_rejected_before_network(self):
        for context in (ExecutionContext(as_of=datetime(2024, 5, 1, tzinfo=TZ)), ExecutionContext(cutoff_explicit=True)):
            value = self.ranking(context)
            self.assertEqual(value.error.code, "SNAPSHOT_NOT_HISTORICAL")
        self.assertEqual(self.requests, [])

    def test_refresh_failure_keeps_original_fetch_time_and_stale_flag(self):
        first = self.ranking()
        cache_key = ("industry_ranking", "sina")
        self.service._cache[cache_key] = (monotonic() - 60, self.service._cache[cache_key][1])
        self.handler = lambda request: httpx.Response(503)
        stale = self.ranking()
        self.assertTrue(stale.stale)
        self.assertEqual(stale.fetched_at, first.fetched_at)
        self.assertEqual(stale.snapshot_id, first.snapshot_id)
        self.assertIsNone(stale.data_time)

    def test_duplicate_or_wrong_identity_is_rejected(self):
        duplicate = 'var S_Finance_bankuai_sinaindustry = {"new_a":"' + row("new_a", "合成行业甲", "2") + '","new_a":"' + row("new_a", "合成行业甲", "3") + '"};'
        for payload, code in ((duplicate.encode("gbk"), "DATA_CONFLICT"), (response({"new_a": row("new_b", "合成行业甲", "2")}), "IDENTITY_MISMATCH")):
            self.handler = lambda request, content=payload: httpx.Response(200, content=content)
            self.assertEqual(self.ranking().error.code, code)

    def test_nonfinite_or_negative_counts_rejected(self):
        for percentage, count in (("NaN", "3"), ("Infinity", "3"), ("2", "-1"), ("2", "1.5")):
            self.handler = lambda request, change=percentage, size=count: httpx.Response(200, content=response({"new_a": row("new_a", "合成行业甲", change, size)}))
            self.assertEqual(self.ranking().error.code, "INVALID_RESPONSE")

    def test_empty_and_html_error_are_not_empty_market(self):
        for content in (response({}), b"<html>error</html>"):
            self.handler = lambda request, payload=content: httpx.Response(200, content=payload)
            self.assertEqual(self.ranking().error.code, "INVALID_RESPONSE")

    def test_tool_does_not_accept_model_urls_or_dates(self):
        for args in ({"url": "https://example.com"}, {"start": "2026-01-01"}, {"limit": 0}):
            self.assertEqual(self.ranking(**args).error.code, "VALIDATION_ERROR")
        self.assertEqual(self.requests, [])

    def test_fund_flow_missing_object_is_not_reported_as_bad_window(self):
        end = now().date() - timedelta(days=1)
        value = self.registry.execute("get_fund_flow", {"start": str(end), "end": str(end)}, ExecutionContext())
        self.assertEqual(value.error.code, "SYMBOL_REQUIRED")
        self.assertEqual(self.requests, [])

    def test_global_attention_replay_does_not_require_stock(self):
        end = now().date() - timedelta(days=1)
        value = self.registry.execute("get_market_attention", {"start": str(end), "end": str(end), "mode": "replay"}, ExecutionContext())
        self.assertEqual(value.error.code, "REPLAY_UNAVAILABLE")

    def test_data_service_uses_dedicated_proxy(self):
        settings = Settings(data_dir=self.root, data_proxy_url="http://127.0.0.1:17897")
        with patch("finagent.data.http.httpx.Client") as client:
            service = DataService(settings)
            client.assert_called_once_with(follow_redirects=False, proxy="http://127.0.0.1:17897")
            service.close()

    def test_injected_offline_client_is_not_replaced_by_proxy(self):
        settings = Settings(data_dir=self.root, data_proxy_url="http://127.0.0.1:17897")
        service = DataService(settings, self.http)
        self.assertIs(service.http.client, self.client)

