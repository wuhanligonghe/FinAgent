import json
import unittest

import httpx
from pydantic import SecretStr

from finagent.config import ModelSettings
from finagent.errors import AgentError
from finagent.models import CompatibleChatModel


class MessagesTests(unittest.TestCase):
    def model(self, handler):
        model = CompatibleChatModel(ModelSettings(base_url="https://model.example/anthropic", name="synthetic-model",
                                    api_key=SecretStr("synthetic-key")), httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(model.close)
        return model

    def test_tool_roundtrip_and_usage(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            self.assertEqual(request.url.path, "/anthropic/v1/messages")
            self.assertEqual(request.headers["x-api-key"], "synthetic-key")
            if len(requests) == 1:
                content = [{"type": "text", "text": "查询数据"}, {"type": "tool_use", "id": "one", "name": "get_quote", "input": {"symbol": "600519"}},
                           {"type": "tool_use", "id": "two", "name": "get_daily_bars", "input": {}}]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": "完成"}]
                stop = "end_turn"
            return httpx.Response(200, json={"role": "assistant", "content": content, "stop_reason": stop,
                                             "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2}})
        model = self.model(handler)
        tool = {"type": "function", "function": {"name": "get_quote", "description": "描述", "parameters": {"type": "object"}}}
        messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "目标"}]
        first = model.complete(messages, [tool], 1)
        self.assertEqual(first.usage["prompt_tokens"], 12)
        self.assertEqual([c.id for c in first.calls], ["one", "two"])
        messages += [first.message, {"role": "tool", "tool_call_id": "one", "content": "结果1"},
                      {"role": "tool", "tool_call_id": "two", "content": "结果2"}, {"role": "system", "content": "新状态"}]
        second = model.complete(messages, [tool], 1)
        self.assertEqual(second.finish_reason, "stop")
        self.assertEqual(requests[1]["messages"][1]["content"], first.message["_anthropic_content"])
        self.assertEqual([b["tool_use_id"] for b in requests[1]["messages"][2]["content"]], ["one", "two"])
        self.assertIn("新状态", requests[1]["system"])
        self.assertEqual(requests[0]["tools"][0]["input_schema"], {"type": "object"})

    def test_duplicate_ids_and_bad_input_rejected(self):
        for content in ([{"type": "tool_use", "id": "same", "name": "a", "input": {}}, {"type": "tool_use", "id": "same", "name": "b", "input": {}}],
                        [{"type": "tool_use", "id": "x", "name": "a", "input": "not-an-object"}]):
            model = self.model(lambda r: httpx.Response(200, json={"role": "assistant", "content": content, "stop_reason": "tool_use"}))
            with self.assertRaises(AgentError) as caught:
                model.complete([], [], 1)
            self.assertEqual(caught.exception.code, "MODEL_PROTOCOL_ERROR")

    def test_http_error_never_exposes_response_secret(self):
        model = self.model(lambda r: httpx.Response(401, text="synthetic-key"))
        with self.assertRaises(AgentError) as caught:
            model.complete([], [], 1)
        self.assertNotIn("synthetic-key", str(caught.exception))
