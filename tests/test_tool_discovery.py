"""Synthetic wire/persistence checks; no claim of live provider cache hits."""
from copy import deepcopy
import json
from unittest.mock import Mock

import httpx
from pydantic import SecretStr

from finagent.config import ModelSettings
from finagent.errors import AgentError
from finagent.harness.host import SessionHost
from finagent.harness.kernel import AgentKernel, Ledger
from finagent.harness.tools import RuntimeTool
from finagent.intent_routing import ToolRoute, routing_context
from finagent.models import CompatibleChatModel, ModelReply, ToolCall
from tests.support import ServiceCase, ScriptedModel


def without_state_snapshots(messages):
    result = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            try:
                body = json.loads(content)
            except ValueError:
                body = {}
            if isinstance(body, dict) and ("session" in body or body.get("_finagent_security", {}).get("kind") == "runtime_context"):
                continue
        result.append(message)
    return result


class DiscoveryTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.settings.model.tool_loading = "deferred"
        self.settings.harness.memory_enabled = False
        self.hosts = []

    def host(self, model=None, name=None, **kwargs):
        host = SessionHost(self.settings, name or f"discovery{len(self.hosts)}",
                           model=model or ScriptedModel(ModelReply({"role": "assistant", "content": "完成"})),
                           service=self.service, **kwargs)
        self.hosts.append(host)
        return host

    def tearDown(self):
        for host in reversed(self.hosts):
            host.close()
        super().tearDown()

    def test_native_discovery_preserves_conversation_prefix_and_cache_usage(self):
        requests = []
        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                content = [{"type": "tool_use", "id": "search", "name": "search_tools", "input": {"query": "get_quote"}}]
            elif len(requests) == 2:
                content = [{"type": "tool_use", "id": "quote", "name": "get_quote", "input": {"symbol": "600519.SH"}}]
            else:
                content = [{"type": "text", "text": "合成行情已读取。"}]
            return httpx.Response(200, json={"role": "assistant", "content": content,
                "stop_reason": "end_turn" if len(requests) == 3 else "tool_use",
                "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 200}})
        model = CompatibleChatModel(ModelSettings(base_url="https://model.example/anthropic", name="synthetic",
            api_key=SecretStr("synthetic-key"), tool_loading="deferred", prompt_caching=True),
            httpx.Client(transport=httpx.MockTransport(respond)))
        host = self.host(model)
        result = host.submit("查询茅台行情")
        self.assertEqual((result.status, result.model_calls, result.tool_calls), ("completed", 3, 2))
        self.assertEqual(len(requests), 3)
        for previous, current in zip(requests, requests[1:]):
            self.assertEqual(previous["tools"], current["tools"])
            self.assertEqual(previous["system"], current["system"])
            prior, now = without_state_snapshots(previous["messages"]), without_state_snapshots(current["messages"])
            self.assertEqual(prior, now[:len(prior)])
            self.assertEqual(len(current["messages"])-len(now), 1)
        definitions = {t["name"]: t for t in requests[0]["tools"]}
        self.assertFalse(definitions["search_tools"]["defer_loading"])
        self.assertTrue(definitions["get_quote"]["defer_loading"])
        blocks = [b for m in requests[1]["messages"] if isinstance(m["content"], list) for b in m["content"]]
        discovered = next(b for b in blocks if b["type"] == "tool_result")
        self.assertIn({"type": "tool_reference", "tool_name": "get_quote"}, discovered["content"])
        self.assertEqual(host.state["usage"]["main"]["cache_read_input_tokens"], 600)
        self.assertEqual(requests[0]["cache_control"], {"type": "ephemeral"})

    def test_group_selection_and_user_rerouting_leave_catalog_unchanged(self):
        host = self.host()
        host.tool_route = ToolRoute(intent="query", tool_groups=["market"], confidence=1, source="rule")
        before = host.schemas(host.ctx)
        output = host.select_tool_groups(["fundamentals"])
        call = ToolCall("expand", "select_tool_groups", '{}')
        message = host.tool_message(call, output, {"role": "tool", "content": "{}"}, host.ctx)
        self.assertIn({"type": "tool_reference", "tool_name": "get_financial_statements"}, message["_tool_references"])
        host.tool_route = ToolRoute(intent="conversation", tool_groups=[], confidence=1, source="rule")
        self.assertEqual(before, host.schemas(host.ctx))

    def test_role_filter_and_permissions_apply_to_search_and_execution(self):
        host = self.host(tool_filter={"search_tools", "select_tool_groups", "get_quote"})
        self.assertEqual({t["function"]["name"] for t in host.schemas(host.ctx)}, host.tool_filter)
        self.assertEqual(host.search_tools("write_file")["discovered_tools"], [])
        self.assertNotIn("write_file", host.select_tool_groups(["workspace"])["discovered_tools"])
        self.assertEqual(host.execute(ToolCall("write", "write_file", '{}'), host.ctx, Ledger(self.settings.limits))["error"], "TOOL_DENIED")
        other = self.host()
        action = Mock()
        other.pool.register(RuntimeTool("sensitive_extension", "external", {"type": "object"}, action, "ask"))
        self.assertIn("sensitive_extension", other.search_tools("sensitive_extension")["discovered_tools"])
        self.assertEqual(other.execute(ToolCall("external", "sensitive_extension", '{}'), other.ctx,
                         Ledger(self.settings.limits))["error"], "PERMISSION_REQUIRED")
        action.assert_not_called()

    def test_untrusted_outputs_cannot_introduce_references(self):
        host = self.host()
        base = {"role": "tool", "content": "{}"}
        output = {"discovered_tools": ["get_quote", "unknown_tool"]}
        message = host.tool_message(ToolCall("x", "get_quote", '{}'), output, dict(base), host.ctx)
        self.assertNotIn("_tool_references", message)
        message = host.tool_message(ToolCall("x", "search_tools", '{}'), output, dict(base), host.ctx)
        self.assertEqual(message["_tool_references"], [{"type": "tool_reference", "tool_name": "get_quote"}])

    def test_narrowed_policy_keeps_historical_schema_without_restoring_permission(self):
        host = self.host()
        host.schemas(host.ctx)
        host.messages.append(host.tool_message(ToolCall("find", "search_tools", '{}'),
            host.search_tools("write_file"), {"role": "tool", "tool_call_id": "find", "content": "{}"}, host.ctx))
        host.tool_filter = {"search_tools", "get_quote"}
        tools = {t["function"]["name"]: t for t in host.schemas(host.ctx)}
        self.assertIn("write_file", tools)  # Required to expand the old reference.
        self.assertTrue(tools["write_file"]["defer_loading"])
        self.assertEqual(host.search_tools("write_file")["discovered_tools"], [])
        self.assertEqual(host.execute(ToolCall("write", "write_file", '{}'), host.ctx,
                         Ledger(self.settings.limits))["error"], "TOOL_DENIED")

    def test_loaded_definitions_and_references_survive_restart_without_schema_rewrite(self):
        host = self.host(name="resume")
        before = host.schemas(host.ctx)
        host.messages.append(host.tool_message(ToolCall("search", "search_tools", '{}'),
            host.search_tools("get_quote"), {"role": "tool", "tool_call_id": "search", "content": "{}"}, host.ctx))
        host.checkpoint()
        host.close()
        restored = self.host(name="resume")
        restored.pool.tools["get_quote"].description = "changed live description"
        self.assertEqual(before, restored.schemas(restored.ctx))
        self.assertEqual(restored.messages[-1]["_tool_references"], [{"type": "tool_reference", "tool_name": "get_quote"}])

    def test_only_latest_context_snapshot_is_sent_and_bookkeeping_is_ignored(self):
        host = self.host()
        host._register_user("解释市盈率")
        first = deepcopy(host.model_messages(host.messages, host.ctx))
        host.record_usage({"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 80}})
        host.checkpoint()
        self.assertEqual(first, host.model_messages(host.messages, host.ctx))
        self.assertEqual(host.state["usage"]["main"]["prompt_tokens_details.cached_tokens"], 80)
        host.ctx.symbol = "600519.SH"
        host.checkpoint()
        second = host.model_messages(host.messages, host.ctx)
        self.assertEqual(without_state_snapshots(first), without_state_snapshots(second))
        self.assertEqual(sum(m.get("_finagent_origin") == "runtime_context" for m in host.messages), 1)
        latest = json.loads(second[-1]["content"])
        self.assertEqual(latest["runtime_state"]["symbol"], "600519.SH")
        self.assertNotIn("usage", latest["session"])
        self.assertNotIn("revision", latest["session"])
        self.assertNotIn("_tool_catalog", latest["session"])
        self.assertEqual(len(routing_context(host.messages)["history"]), 1)

    def test_narrow_worker_without_discovery_keeps_its_tools_callable(self):
        host = self.host(tool_filter={"get_quote"})
        tools = host.schemas(host.ctx)
        self.assertEqual(len(tools), 1)
        self.assertFalse(tools[0]["defer_loading"])

    def test_worker_can_discover_plan_tool_before_plan_approval(self):
        host = self.host()
        host.member = {"name": "worker"}
        host.teams.can_act = Mock(return_value=False)
        ledger = Ledger(self.settings.limits)
        output = host.execute(ToolCall("search", "search_tools", '{"query":"submit_plan"}'), host.ctx, ledger)
        self.assertIn("submit_plan", output["discovered_tools"])
        denied = host.execute(ToolCall("quote", "get_quote", '{}'), host.ctx, ledger)
        self.assertEqual(denied["error"], "PLAN_OR_ASSIGNMENT_REQUIRED")

    def test_web_can_discover_financial_tools_without_exposing_shell(self):
        from finagent.web.session_manager import SessionManager
        from finagent.web.store import WebStore
        store = WebStore(self.root / "web")
        session = store.create_session("discovery")
        manager = SessionManager(self.settings, store,
            host_factory=lambda settings, session_id: self.host(name=session_id))
        try:
            host = manager.host(session["id"])
            names = {tool["function"]["name"] for tool in host.schemas(host.ctx)}
            self.assertIn("search_tools", names)
            self.assertNotIn("shell", names)
            self.assertNotIn("write_file", names)
            self.assertEqual(host.search_tools("write_file")["discovered_tools"], [])
            self.assertIn("get_quote", host.search_tools("get_quote")["discovered_tools"])
        finally:
            manager.close()

    def test_deferred_mode_rejects_plain_chat_protocol_before_http(self):
        with self.assertRaises(AgentError) as caught:
            CompatibleChatModel(ModelSettings(base_url="https://model.example/v1", name="synthetic",
                api_key=SecretStr("synthetic-key"), tool_loading="deferred"))
        self.assertEqual(caught.exception.code, "MODEL_TOOL_LOADING_UNSUPPORTED")

    def test_server_search_blocks_and_pause_are_preserved_without_local_execution(self):
        seen = []
        blocks = [{"type": "server_tool_use", "id": "srvtoolu_search", "name": "tool_search_tool_regex", "input": {"pattern": "quote"}},
                  {"type": "tool_search_tool_result", "tool_use_id": "srvtoolu_search", "content": {
                      "type": "tool_search_tool_search_result", "tool_references": [{"type": "tool_reference", "tool_name": "get_quote"}]}}]
        def respond(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"role": "assistant", "content": blocks if len(seen) == 1 else [{"type": "text", "text": "完成"}],
                "stop_reason": "pause_turn" if len(seen) == 1 else "end_turn"})
        model = CompatibleChatModel(ModelSettings(base_url="https://model.example/anthropic", name="synthetic",
            api_key=SecretStr("synthetic-key")), httpx.Client(transport=httpx.MockTransport(respond)))
        self.addCleanup(model.close)
        tools = [{"type": "function", "defer_loading": True, "function": {"name": "get_quote", "description": "行情", "parameters": {"type": "object"}}},
                 {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}]
        first = model.complete([{"role": "user", "content": "查询"}], tools, 1)
        self.assertEqual(first.calls, [])
        self.assertEqual(first.finish_reason, "pause")
        AgentKernel.validate(first, [])
        model.complete([{"role": "user", "content": "查询"}, first.message], tools, 1)
        self.assertEqual(seen[1]["messages"][-1]["content"], blocks)
        self.assertFalse(any(m.get("role") == "tool" for m in seen[1]["messages"]))
