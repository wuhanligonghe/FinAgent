"""Switching and multi-stock queries use synthetic models and HTTP responses."""
from datetime import datetime
import json

import httpx

from finagent.agent import AgentLoop, RunRequest
from finagent.chat import chat_loop
from finagent.data.contracts import Observation
from finagent.models import ModelReply
from finagent.runtime import ExecutionContext, TZ, now
from finagent.session import Conversation, restore_context

from .support import ServiceCase, ScriptedModel, call_reply, final_reply, tencent_body
from .test_symbol_search import search_body, stock


class SymbolSwitchingTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.stamp = now().strftime("%Y%m%d%H%M%S")

    def handler(self, request):
        if request.url.host == "searchapi.eastmoney.com":
            return httpx.Response(200, json=search_body(stock("002298", "中电鑫龙", "0", security_type="2")))
        vendor = request.url.params["q"]
        return httpx.Response(200, content=tencent_body(
            code=vendor[2:], vendor=vendor, price="11" if vendor == "sz002298" else "10", stamp=self.stamp))

    def quote_resolved(self, messages):
        observation = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
        self.assertIsNone(observation["error"])
        self.assertEqual(observation["data"]["selected"]["symbol"], "002298.SZ")
        state = next(json.loads(m["content"])["runtime_state"] for m in reversed(messages)
                     if isinstance(m.get("content"), str) and m["content"].startswith("{")
                     and "runtime_state" in json.loads(m["content"]))
        self.assertEqual(state["symbol"], "002298.SZ")
        self.assertEqual(state["symbol_role"], "current_focus")
        return call_reply(("new_quote", "get_quote", {"symbol": "002298.SZ"}))

    def test_reported_unknown_name_switches_from_inherited_bank_and_startup_default(self):
        session = Conversation(self.store)
        session.state["symbol"] = "601166.SH"
        goal = "那中电鑫龙今天行情怎么样"
        ctx = session.context(goal, self.settings.limits, symbol="601166.SH")
        model = ScriptedModel(call_reply(("resolve", "resolve_entity", {"query": "中电鑫龙"})),
                              self.quote_resolved, ModelReply({"role": "assistant", "content": "本次合成测试已取得中电鑫龙行情。"}))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest(goal), ctx)
        self.assertEqual((result.status, result.model_calls, result.tool_calls), ("completed", 3, 2))
        body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(body["symbol"], "002298.SZ")
        self.assertEqual(body["queried_symbols"], ["002298.SZ"])
        self.assertNotIn("SCOPE_MISMATCH", result.reason)
        session.remember(result)
        self.assertEqual(Conversation(self.store).state["symbol"], "002298.SZ")
        quote_requests = [r.url.params["q"] for r in self.requests if r.url.host == "qt.gtimg.cn"]
        self.assertEqual(quote_requests, ["sz002298"])

    def test_chat_switches_without_new_session_or_forget_command(self):
        model = ScriptedModel(call_reply(("bank", "get_quote", {"symbol": "601166.SH"})),
                              ModelReply({"role": "assistant", "content": "已取得兴业银行合成行情。"}),
                              ModelReply({"role": "assistant", "content": "[]"}),
                              call_reply(("resolve", "resolve_entity", {"query": "中电鑫龙"})),
                              self.quote_resolved, ModelReply({"role": "assistant", "content": "已切换到中电鑫龙合成行情。"}),
                              ModelReply({"role": "assistant", "content": "[]"}))
        entries, output = iter(["兴业银行今天行情怎么样", "那中电鑫龙今天行情怎么样", "/exit"]), []
        chat_loop(AgentLoop(model, self.registry, self.store), self.settings.limits,
                  symbol="601166.SH", read=lambda _: next(entries), write=output.append)
        from finagent.harness.state import RuntimeStore
        runtime = RuntimeStore(self.root / "v3")
        records = [json.loads(r['body'])["payload"] for r in runtime.events(kind='turn_completed')]
        first, second = records
        self.assertEqual((first["result"]["status"], second["result"]["status"]), ("completed", "completed"))
        self.assertEqual(runtime.load("default")["state"]["focus"], "002298.SZ")
        self.assertTrue(set(first["result_tools"]).issubset(second["result_tools"]))
        self.assertEqual(model.index, 7)  # Five main calls and two discoveries.
        self.assertFalse(any("需要补充" in item for item in output))

    def test_explicit_security_and_current_reference_override_startup_default(self):
        session = Conversation(self.store)
        session.state["symbol"] = "002298.SZ"
        for goal, expected in (("那宁德时代今天怎么样", "300750.SZ"),
                               ("查002298今天行情", "002298.SZ"), ("那它的成交量呢", "002298.SZ")):
            with self.subTest(goal=goal):
                ctx = session.context(goal, self.settings.limits, symbol="601166.SH")
                self.assertEqual(ctx.symbol, expected)

    def test_one_run_queries_two_stocks_and_calculates_each_from_its_own_input(self):
        def calculate_both(messages):
            quotes = {value["symbol"]: value for m in messages if m["role"] == "tool"
                      for value in [json.loads(m["content"]) ]}
            return call_reply(("calc_bank", "calculate_market_metrics", {"result_id": quotes["601166.SH"]["result_id"]}),
                              ("calc_new", "calculate_market_metrics", {"result_id": quotes["002298.SZ"]["result_id"]}))
        model = ScriptedModel(call_reply(("bank", "get_quote", {"symbol": "601166.SH"}),
                                        ("new", "get_quote", {"symbol": "002298.SZ"})),
                              calculate_both, ModelReply({"role": "assistant", "content": "两家公司合成行情已分别取得并计算。"}))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("比较两家公司的涨跌幅"))
        self.assertEqual((result.status, result.tool_calls), ("completed", 4))
        calculations = {value.symbol: value for key in result.calculation_ids for value in [self.store.load_result(key)]}
        self.assertEqual(calculations["601166.SH"].data["current_price"], 10)
        self.assertEqual(calculations["002298.SZ"].data["current_price"], 11)
        self.assertEqual(calculations["601166.SH"].data["change_amount"], 1)
        self.assertEqual(calculations["002298.SZ"].data["change_amount"], 2)
        report = (result.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("研究对象：002298.SZ、601166.SH", report)

    def test_daily_calculation_still_rejects_mixing_different_companies(self):
        ctx = ExecutionContext(as_of=datetime(2024, 5, 1, tzinfo=TZ))
        inputs = []
        for symbol, day in (("601166.SH", "2024-04-30"), ("002298.SZ", "2024-04-29")):
            raw = self.store.save_raw(b"[]", {"source": "SYNTHETIC", "encoding": "utf-8"})
            value = self.store.save_result(Observation(status="partial", symbol=symbol, market="CN_A", currency="CNY",
                                                       source="SYNTHETIC", price_basis="unadjusted", units={"price": "CNY/share"},
                                                       snapshot_id=raw, data={"bars": [{"date": day, "close": 10, "locator": "$[0]"}]}))
            ctx.result_tools[value.result_id] = "get_daily_bars"
            ctx.evidence_ids.add(value.result_id)
            inputs.append(value.result_id)
        result = self.registry.execute("calculate_market_metrics", {"result_id": inputs[0], "trade_date": "2024-04-30",
                                                                   "previous_result_id": inputs[1]}, ctx)
        self.assertEqual(result.error.code, "INCOMPARABLE_PRICES")
        self.assertIn("同一证券", result.error.message)

    def test_queried_instruments_survive_explicit_resume_without_resetting_budget(self):
        model = ScriptedModel(call_reply(("a", "get_quote", {"symbol": "601166.SH"}),
                                        ("b", "get_quote", {"symbol": "002298.SZ"})), final_reply(status="partial"))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("合成双证券查询"))
        state, restored = restore_context(result.run_id, self.store)
        self.assertEqual(restored.queried_symbols, {"601166.SH", "002298.SZ"})
        self.assertEqual(restored.symbol, "002298.SZ")
        self.assertEqual(restored.source_requests, 2)
        self.assertEqual(state["tool_calls"], 2)
