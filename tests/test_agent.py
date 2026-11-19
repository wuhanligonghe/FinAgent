from datetime import datetime
import json

import httpx
from pydantic import SecretStr

from finagent.agent import AgentLoop, RunRequest
from finagent.config import ModelSettings
from finagent.models import CompatibleChatModel, ModelReply
from finagent.runtime import ExecutionContext

from .support import ServiceCase, ScriptedModel, call_reply, final_reply


def cite_last(messages):
    results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
    return final_reply([r["snapshot_id"] for r in results if r["snapshot_id"]])


class LoopTests(ServiceCase):
    def loop(self, model, request=None, context=None):
        return AgentLoop(model, self.registry, self.store).run(request or RunRequest("合成测试数据任务"), context)

    def test_21_model_observes_tool_result_before_next_action(self):
        def next_action(messages):
            observation = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
            return call_reply(("second", "get_quote", {"symbol": observation["symbol"], "source": "tencent"}))
        model = ScriptedModel(call_reply(("first", "get_quote", {"symbol": "600519"})), next_action, cite_last)
        result = self.loop(model, RunRequest("合成数据查询", ("get_quote",)))
        self.assertEqual(result.model_calls, 3)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.status, "completed")
        self.assertTrue(any(m["role"] == "tool" for m in model.messages_seen[1]))

    def test_22_multi_tool_mixed_success_ids_paired(self):
        model = ScriptedModel(call_reply(("a", "get_quote", {"symbol": "600519"}), ("b", "unknown_tool", {})), cite_last)
        result = self.loop(model)
        messages = json.loads((result.run_dir / "messages.json").read_text(encoding="utf-8"))
        outputs = [m for m in messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in outputs], ["a", "b"])
        self.assertEqual(json.loads(outputs[1]["content"])["error"]["code"], "UNKNOWN_TOOL")

    def test_23_missing_data_is_recorded_without_format_corrections(self):
        result = self.loop(ScriptedModel(final_reply()))
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(result.status, "partial")
        self.assertIn("DATA_UNAVAILABLE", result.reason)

    def test_24_model_budget_stops_even_with_tool_requests(self):
        model = ScriptedModel(call_reply(("a", "get_quote", {"symbol": "600519"})))
        ctx = ExecutionContext()
        ctx.limits.model_calls = 1
        result = self.loop(model, context=ctx)
        self.assertEqual(result.model_calls, 1)
        self.assertIn("MODEL_BUDGET", result.reason)

    def test_25_tool_budget_pairs_rejected_requests(self):
        model = ScriptedModel(call_reply(("a", "get_quote", {"symbol": "600519"}), ("b", "get_quote", {"symbol": "600519"})))
        ctx = ExecutionContext()
        ctx.limits.tool_calls = 1
        result = self.loop(model, context=ctx)
        outputs = [m for m in json.loads((result.run_dir / "messages.json").read_text(encoding="utf-8")) if m["role"] == "tool"]
        self.assertEqual(len(outputs), 2)
        self.assertEqual(json.loads(outputs[-1]["content"])["error"]["code"], "TOOL_BUDGET")

    def test_26_no_progress_and_wrong_stop_reason(self):
        def repeated(messages):
            index = len([m for m in messages if m["role"] == "tool"])
            return call_reply((f"c{index}", "unknown_tool", {}), finish_reason="stop")
        result = self.loop(ScriptedModel(repeated))
        self.assertEqual(result.tool_calls, 3)
        self.assertIn("NO_PROGRESS", result.reason)

    def test_27_truncated_calls_not_executed_but_paired(self):
        result = self.loop(ScriptedModel(call_reply(("a", "get_quote", {"symbol": "600519"}), finish_reason="length")))
        self.assertEqual(self.requests, [])
        self.assertIn("MODEL_TRUNCATED", result.reason)
        outputs = [m for m in json.loads((result.run_dir / "messages.json").read_text(encoding="utf-8")) if m["role"] == "tool"]
        self.assertEqual(len(outputs), 1)

    def test_28_unknown_citation_is_not_registered_or_verified(self):
        model = ScriptedModel(call_reply(("a", "get_quote", {"symbol": "600519"})), final_reply(["raw_" + "a" * 64]))
        result = self.loop(model)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.model_calls, 2)
        body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(body["cited_evidence_ids"], [])
        self.assertEqual(body["unresolved_reference_ids"], ["raw_" + "a" * 64])
        self.assertFalse(body["final_protocol_verified"])

    def test_29_clarification_prevents_dependent_calls(self):
        model = ScriptedModel(call_reply(("a", "ask_user", {"question": "请选择利润口径"}), ("b", "get_quote", {"symbol": "600519"})))
        result = self.loop(model)
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(self.requests, [])

    def test_30_model_adapter_protocol_and_secret_not_logged(self):
        key = "synthetic-secret-for-unit-test"
        settings = ModelSettings(base_url="https://model.example/v1", name="synthetic-model", api_key=SecretStr(key))
        def respond(request):
            self.assertEqual(request.headers["Authorization"], "Bearer " + key)
            self.assertEqual(request.url.path, "/v1/chat/completions")
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
        client = httpx.Client(transport=httpx.MockTransport(respond))
        model = CompatibleChatModel(settings, client)
        reply = model.complete([], self.registry.schemas(), 1)
        self.assertEqual(reply.message["content"], "ok")
        self.assertNotIn(key, settings.model_dump_json())
        self.assertNotIn(key, repr(settings))
        model.close()
