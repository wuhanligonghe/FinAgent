"""Synthetic search results; real interface checks are saved separately."""
import json
from time import monotonic

import httpx

from finagent.agent import AgentLoop
from finagent.chat import chat_loop
from finagent.runtime import ExecutionContext

from .support import ServiceCase, ScriptedModel, call_reply, tencent_body
from .test_agent import cite_last


def stock(code="600900", name="长江电力", market="1", kind="AStock", security_type="1"):
    return {"Code": code, "Name": name, "MktNum": market, "Classify": kind,
            "SecurityType": security_type, "QuoteID": f"{market}.{code}"}


def search_body(*rows, total=None):
    return {"QuotationCodeTable": {"Status": 0, "Data": list(rows),
                                    "TotalCount": len(rows) if total is None else total}}


class SymbolSearchTests(ServiceCase):
    def resolve(self, query, ctx=None):
        return self.registry.execute("resolve_entity", {"query": query}, ctx or ExecutionContext())

    def test_builtin_names_aliases_and_codes_require_no_http(self):
        for query, expected in (("贵州茅台", "600519.SH"), ("茅台", "600519.SH"),
                                ("宁德时代", "300750.SZ"), ("美的", "000333.SZ"),
                                ("ＳＨ６００９００", "600900.SH")):
            with self.subTest(query=query):
                ctx = ExecutionContext()
                result = self.resolve(query, ctx)
                self.assertEqual(result.data["selected"]["symbol"], expected)
                self.assertEqual(ctx.symbol, expected)
                self.assertFalse(result.data["needs_clarification"])
        self.assertEqual(self.requests, [])

    def test_online_exact_name_binds_symbol_and_saves_raw_evidence(self):
        self.handler = lambda r: httpx.Response(200, json=search_body(stock()))
        ctx = ExecutionContext()
        result = self.resolve("长江电力", ctx)
        self.assertEqual(result.source, "eastmoney_security_search")
        self.assertEqual(result.data["selected"]["symbol"], "600900.SH")
        self.assertEqual(ctx.symbol, "600900.SH")
        self.assertEqual(ctx.source_requests, 1)
        self.assertTrue(self.store.has_raw(result.snapshot_id))
        self.assertIn(result.snapshot_id, ctx.evidence_ids)
        self.assertEqual(self.requests[0].url.params["input"], "长江电力")
        self.assertEqual(result.data["selected"]["locator"], "$.QuotationCodeTable.Data[0]")

    def test_exact_match_ignores_foreign_bond_and_fuzzy_candidates(self):
        self.handler = lambda r: httpx.Response(200, json=search_body(
            stock(), stock("CYPC", "长江电力", "156", "LSEIOB", "24"),
            stock("110000", "长江转债", "1", "Bond", "7"),
            stock("600001", "长江其他")))
        result = self.resolve("长江电力")
        self.assertEqual(result.data["selected"]["symbol"], "600900.SH")
        self.assertEqual(result.data["excluded_count"], 2)
        self.assertEqual(len(result.data["candidates"]), 2)

    def test_multiple_or_single_fuzzy_candidates_require_name_confirmation(self):
        for rows in ((stock(),), (stock(), stock("600001", "长江其他"))):
            with self.subTest(count=len(rows)):
                self.service._cache.clear()
                self.handler = lambda r: httpx.Response(200, json=search_body(*rows))
                ctx = ExecutionContext()
                result = self.resolve("长江", ctx)
                self.assertIsNone(result.data["selected"])
                self.assertIsNone(ctx.symbol)
                self.assertIn("长江电力", ctx.clarification)
                self.assertIn("选项序号", ctx.clarification)
                denied = self.registry.execute("get_quote", {"symbol": "600900"}, ctx)
                self.assertEqual(denied.error.code, "CLARIFICATION_PENDING")

    def test_ambiguous_exact_matches_and_truncated_search_are_not_auto_selected(self):
        for rows, total in (((stock(), stock("600001", "长江电力")), 2), ((stock(),), 30)):
            with self.subTest(total=total):
                self.service._cache.clear()
                self.handler = lambda r: httpx.Response(200, json=search_body(*rows, total=total))
                result = self.resolve("长江电力")
                self.assertIsNone(result.data["selected"])
                self.assertTrue(result.data["needs_clarification"])

    def test_empty_or_only_unsupported_markets_offer_name_instead_of_mandatory_code(self):
        for rows in ((), (stock("00700", "腾讯控股", "116", "HK", "5"),)):
            with self.subTest(rows=rows):
                self.service._cache.clear()
                self.handler = lambda r: httpx.Response(200, json=search_body(*rows))
                result = self.resolve("腾讯控股")
                self.assertEqual(result.status, "empty")
                self.assertIsNone(result.data["selected"])
                self.assertIn("更完整名称", result.data["needs_clarification"])
                self.assertIn("在线检索只覆盖沪深A股", result.data["needs_clarification"])

    def test_source_null_data_with_zero_count_is_a_valid_empty_result(self):
        self.handler = lambda r: httpx.Response(200, json={"QuotationCodeTable": {"Status": 0, "Data": None, "TotalCount": 0}})
        self.assertEqual(self.resolve("未匹配公司").status, "empty")

    def test_shenzhen_security_type_two_is_supported(self):
        self.handler = lambda r: httpx.Response(200, json=search_body(stock("002594", "比亚迪", "0", security_type="2")))
        self.assertEqual(self.resolve("比亚迪").data["selected"]["symbol"], "002594.SZ")

    def test_schema_failure_is_not_reported_as_name_not_found(self):
        for body in ({}, {"QuotationCodeTable": {"Status": 0, "Data": None, "TotalCount": 2}},
                     search_body({**stock(), "QuoteID": "0.600900"})):
            with self.subTest(body=body):
                self.service._cache.clear()
                self.handler = lambda r: httpx.Response(200, json=body)
                ctx = ExecutionContext()
                result = self.resolve("长江电力", ctx)
                self.assertEqual(result.error.code, "INVALID_RESPONSE")
                self.assertIsNone(ctx.clarification)
                self.assertIsNone(ctx.symbol)

    def test_source_failure_or_budget_does_not_guess_or_bind(self):
        self.handler = lambda r: httpx.Response(503)
        result = self.resolve("长江电力")
        self.assertEqual(result.error.code, "HTTP_ERROR")
        ctx = ExecutionContext()
        ctx.source_requests = ctx.limits.source_requests
        count = len(self.requests)
        result = self.resolve("长江电力", ctx)
        self.assertEqual(result.error.code, "SOURCE_BUDGET")
        self.assertEqual(len(self.requests), count)
        self.assertIsNone(ctx.symbol)

    def test_cache_is_reused_without_leaking_task_scope(self):
        self.handler = lambda r: httpx.Response(200, json=search_body(stock()))
        first = self.resolve("长江电力")
        ctx = ExecutionContext()
        second = self.resolve("长江电力", ctx)
        self.assertTrue(second.cache_hit)
        self.assertEqual(second.snapshot_id, first.snapshot_id)
        self.assertEqual(ctx.source_requests, 0)
        self.assertEqual(ctx.symbol, "600900.SH")
        ctx = ExecutionContext(symbol="600519.SH")
        switched = self.resolve("长江电力", ctx)
        self.assertIsNone(switched.error)
        self.assertEqual(ctx.symbol, "600900.SH")
        self.assertEqual(ctx.queried_symbols, {"600900.SH"})
        self.assertEqual(len(self.requests), 1)

    def test_stale_search_after_failed_refresh_is_not_auto_selected(self):
        self.handler = lambda r: httpx.Response(200, json=search_body(stock()))
        self.resolve("长江电力")
        key = ("security_search", "长江电力")
        self.service._cache[key] = (monotonic() - 400, self.service._cache[key][1])
        self.handler = lambda r: httpx.Response(503)
        result = self.resolve("长江电力")
        self.assertTrue(result.stale)
        self.assertIsNone(result.data["selected"])

    def test_explicit_unsupported_market_and_invalid_code_never_search(self):
        for query in ("00700.HK", "港股工商银行", "600519.SZ", "../../secret"):
            with self.subTest(query=query):
                self.assertEqual(self.resolve(query).status, "error")
        self.assertEqual(self.requests, [])

    def test_custom_full_company_name_and_alias_are_supported(self):
        catalog = self.root / "securities.json"
        catalog.write_text(json.dumps([{"symbol": "600900.SH", "name": "长江电力",
                                        "aliases": ["中国长江电力股份有限公司"]}], ensure_ascii=False), encoding="utf-8")
        self.settings.securities_file = catalog
        self.assertEqual(self.resolve("中国长江电力股份有限公司").data["selected"]["symbol"], "600900.SH")
        self.assertEqual(self.requests, [])

    def test_name_with_digits_reaches_search_instead_of_invalid_code(self):
        self.handler = lambda r: httpx.Response(200, json=search_body())
        self.assertEqual(self.resolve("测试3公司").status, "empty")
        self.assertEqual(len(self.requests), 1)

    def test_chat_resolves_name_then_queries_without_requesting_user_code(self):
        def respond(request):
            if request.url.host == "searchapi.eastmoney.com":
                return httpx.Response(200, json=search_body(stock()))
            return httpx.Response(200, content=tencent_body(code="600900", vendor="sh600900"))

        def after_resolve(messages):
            observed = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
            return call_reply(("quote", "get_quote", {"symbol": observed["data"]["selected"]["symbol"]}))

        self.handler = respond
        model = ScriptedModel(call_reply(("resolve", "resolve_entity", {"query": "长江电力"})), after_resolve, cite_last)
        entries, output = iter(["查长江电力行情", "/exit"]), []
        chat_loop(AgentLoop(model, self.registry, self.store), self.settings.limits,
                  read=lambda prompt: next(entries), write=output.append)
        from finagent.harness.state import RuntimeStore
        result = RuntimeStore(self.root / "v3").load("default")
        state, result = result["state"], result["state"]["last_result"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(state["focus"], "600900.SH")
        self.assertEqual(result["tool_calls"], 2)
        self.assertEqual(state["last_ledger"]["operations"]["source"], 2)
        self.assertFalse(any("需要补充" in line for line in output))

    def test_online_name_search_alone_does_not_complete_financial_task(self):
        from finagent.agent import RunRequest
        self.handler = lambda r: httpx.Response(200, json=search_body(stock()))
        model = ScriptedModel(call_reply(("resolve", "resolve_entity", {"query": "长江电力"})), cite_last)
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("查长江电力行情"))
        self.assertEqual(result.status, "partial")
        self.assertIn("DATA_UNAVAILABLE", result.reason)
        self.assertEqual(result.model_calls, 2)

    def test_large_candidate_lists_are_bounded_and_require_clarification(self):
        rows = [stock(f"{600000+i:06d}", f"中国测试{i}") for i in range(40)]
        self.handler = lambda r: httpx.Response(200, json=search_body(*rows))
        result = self.resolve("中国")
        self.assertEqual(len(result.data["candidates"]), 20)
        self.assertTrue(result.data["truncated"])
        self.assertIsNone(result.data["selected"])
        self.assertIn("10.", result.data["needs_clarification"])
        self.assertNotIn("11.", result.data["needs_clarification"])
