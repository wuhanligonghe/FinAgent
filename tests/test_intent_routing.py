"""Synthetic protocol/routing checks, not evidence of live Qwen accuracy."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from finagent.config import IntentSettings, load_settings
from finagent.errors import AgentError
from finagent.harness.host import SessionHost
from finagent.harness.kernel import Ledger
from finagent.harness.tools import RuntimeTool
from finagent.intent_routing import (
    COMMON_TOOLS, GROUPS, GROUP_TOOLS, Classification, IntentRouter,
    QwenIntentClassifier, fallback_route, routing_context, tools_for_groups,
)
from finagent.models import ModelReply, ToolCall
from tests.support import ServiceCase, ScriptedModel, call_reply


def classification(intent="query", groups=None, confidence=0.95):
    # Test helper accepts historical names; HTTP payload uses only the new contract.
    action = {"concept": "explain", "analysis": "analyze", "research": "research_start"}.get(intent, intent)
    return {"action": action, "data_domains": ["market"] if groups is None else groups, "confidence": confidence}


def mock_classifier(payload=None, *, status=200, change=None):
    seen = []
    def respond(request):
        seen.append(json.loads(request.content))
        body = {"model": "qwen3.5:9b", "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(payload or classification())},
                "prompt_eval_count": 100, "eval_count": 20}
        body.update(change or {})
        return httpx.Response(status, json=body)
    return QwenIntentClassifier(IntentSettings(), transport=httpx.MockTransport(respond)), seen


class ClassifierTests(unittest.TestCase):
    def test_stock_model_protocol_and_multi_label(self):
        classifier, seen = mock_classifier(classification(groups=["market", "fundamentals"]))
        route = IntentRouter(IntentSettings(), classifier=classifier).route("茅台利润和股价")
        self.assertEqual(route.tool_groups, ["market", "fundamentals"])
        self.assertEqual(route.source, "qwen")
        self.assertEqual(route.usage, {"prompt_tokens": 100, "completion_tokens": 20})
        request = seen[0]
        self.assertEqual(request["model"], "qwen3.5:9b")
        self.assertFalse(request["think"])
        self.assertFalse(request["stream"])
        self.assertEqual(request["options"]["temperature"], 0)
        self.assertNotIn("tools", request)
        self.assertEqual(request["format"]["additionalProperties"], False)

    def test_complete_social_only_skips_qwen(self):
        classifier, seen = mock_classifier()
        router = IntentRouter(IntentSettings(), classifier=classifier)
        self.assertEqual(router.route("你好，谢谢！").source, "rule")
        self.assertEqual(seen, [])
        router.route("你好，茅台股价多少")
        self.assertEqual(len(seen), 1)

    def test_low_confidence_and_unknown_keep_all_categories(self):
        for payload in (classification(confidence=0.4), classification("other", [])):
            classifier, _ = mock_classifier(payload)
            route = IntentRouter(IntentSettings(), classifier=classifier).route("继续上面的工作")
            self.assertEqual(set(route.tool_groups), set(GROUPS))
            self.assertTrue(route.fallback_reason)

    def test_pure_concept_has_no_data_tools(self):
        classifier, _ = mock_classifier(classification("concept", []))
        route = IntentRouter(IntentSettings(), classifier=classifier).route("市盈率是什么意思")
        self.assertEqual(route.tool_groups, [])
        self.assertNotIn("get_quote", tools_for_groups(route.tool_groups, {"get_quote", "ask_user"}))

    def test_invalid_output_and_truncation_fall_back(self):
        changes = [
            {"done_reason": "length"}, {"done": False}, {"model": "some-other-model"},
            {"message": {"role": "assistant", "content": "```json\n{}\n```"}},
            {"message": {"role": "assistant", "content": json.dumps(classification(groups=["delete_everything"]))}},
            {"message": {"role": "assistant", "tool_calls": [{}], "content": "{}"}},
            {"message": None},
        ]
        for change in changes:
            with self.subTest(change=change):
                classifier, _ = mock_classifier(change=change)
                result = IntentRouter(IntentSettings(), classifier=classifier).route("利润和股价")
                self.assertEqual(result.source, "fallback")
                self.assertEqual(result.fallback_reason, "INTENT_INVALID_OUTPUT")
                self.assertEqual(set(result.tool_groups), {"market", "fundamentals"})

    def test_timeout_missing_and_transport_failure(self):
        for error, code in ((httpx.ReadTimeout("slow"), "INTENT_TIMEOUT"),
                            (httpx.ConnectError("offline"), "INTENT_UNAVAILABLE")):
            def fail(request, error=error):
                raise error
            classifier = QwenIntentClassifier(IntentSettings(), transport=httpx.MockTransport(fail))
            result = IntentRouter(IntentSettings(), classifier=classifier).route("哪个行业最强")
            self.assertEqual(result.tool_groups, ["sector"])
            self.assertEqual(result.fallback_reason, code)
        classifier, _ = mock_classifier(status=404)
        result = IntentRouter(IntentSettings(), classifier=classifier).route("股价")
        self.assertEqual(result.fallback_reason, "INTENT_MODEL_MISSING")

    def test_overlong_input_falls_back_without_generation_or_truncation(self):
        classifier, seen = mock_classifier()
        result = IntentRouter(IntentSettings(), classifier=classifier).route("a" * 4001 + "政策和利润")
        self.assertEqual(result.source, "fallback")
        self.assertEqual(set(result.tool_groups), {"policy", "fundamentals"})
        self.assertFalse(seen)

    def test_strict_contract_rejects_extra_fields_duplicates_nan_and_incoherence(self):
        for payload in (dict(classification(), symbol="600519.SH"), classification(groups=["market", "market"]),
                        classification(confidence=float("nan")), classification("concept", ["market"]),
                        classification("query", []), classification(confidence="0.9")):
            classifier, _ = mock_classifier(payload)
            self.assertEqual(IntentRouter(IntentSettings(), classifier=classifier).route("股价").source, "fallback")

    def test_context_is_bounded_and_excludes_tool_data(self):
        messages = [{"role": "user", "content": str(i) + "x" * 1000} for i in range(8)]
        messages += [{"role": "tool", "content": "untrusted raw response"},
                     {"role": "assistant", "content": None}]
        context = routing_context(messages, "600519.SH")
        self.assertEqual(len(context["history"]), 3)
        self.assertTrue(all(len(m["content"]) == 600 for m in context["history"]))
        self.assertEqual(context["confirmed_symbol"], "600519.SH")

    def test_unknown_fallback_exposes_every_group(self):
        self.assertEqual(set(fallback_route("那它呢").tool_groups), set(GROUPS))

    def test_env_override_and_endpoint_validation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "finagent.toml"
            path.write_text('[intent]\nenabled = true\n', encoding="utf-8")
            with patch.dict("os.environ", {"FINAGENT_INTENT_ENABLED": "false", "FINAGENT_INTENT_MODEL": "qwen3.5:9b"}):
                self.assertFalse(load_settings(path).intent.enabled)
        for endpoint in ("file:///tmp/model", "http://user:pass@localhost", "http://localhost/?secret=yes"):
            with self.assertRaises(ValueError):
                IntentSettings(endpoint=endpoint)


class IntentHostTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.settings.intent.enabled = True
        self.settings.harness.memory_enabled = False
        self.classifier, self.seen = mock_classifier()
        self.hosts = []

    def host(self, model=None, **kwargs):
        host = SessionHost(self.settings, "route" + str(len(self.hosts)),
            model=model or ScriptedModel(ModelReply({"role": "assistant", "content": "已记录"})),
            service=self.service, intent_classifier=self.classifier, **kwargs)
        self.hosts.append(host)
        return host

    def tearDown(self):
        for host in self.hosts:
            host.close()
        super().tearDown()

    def names(self, host):
        return {t["function"]["name"] for t in host.schemas(host.ctx)}

    def test_classification_precedes_main_and_reduces_schemas(self):
        captured = []
        class Main:
            def complete(_, messages, tools, timeout):
                captured.append(({t["function"]["name"] for t in tools}, messages))
                return ModelReply({"role": "assistant", "content": "测试"})
        host = self.host(Main())
        result = host.submit("茅台股价")
        self.assertEqual(result.status, "completed")
        self.assertIn("get_quote", captured[0][0])
        self.assertNotIn("get_financial_statements", captured[0][0])
        self.assertNotIn("write_file", captured[0][0])
        self.assertIn("select_tool_groups", captured[0][0])
        self.assertEqual(host.state["last_ledger"]["operations"]["model"], 2)
        self.assertEqual(host.state["usage"]["intent"]["prompt_tokens"], 100)
        self.assertIn('"tool_routing"', captured[0][1][1]["content"])

    def test_expand_then_execute_without_widening_role_filter(self):
        host = self.host(ScriptedModel(
            call_reply(("expand", "select_tool_groups", {"groups": ["fundamentals"]})),
            ModelReply({"role": "assistant", "content": "已补选"})))
        host.submit("茅台股价")
        self.assertIn("get_financial_statements", self.names(host))
        narrow = self.host(tool_filter={"get_quote", "select_tool_groups"})
        narrow.submit("股价")
        narrow.select_tool_groups(["workspace", "fundamentals"])
        self.assertEqual(self.names(narrow), {"get_quote", "select_tool_groups"})
        denied = narrow.execute(ToolCall("w", "write_file", '{}'), narrow.ctx, Ledger(self.settings.limits))
        self.assertEqual(denied["error"], "TOOL_DENIED")

    def test_unselected_tool_cannot_execute_and_selection_is_not_approval(self):
        host = self.host()
        host.submit("股价")
        called = Mock()
        host.pool.register(RuntimeTool("sensitive_extension", "external", {"type": "object"}, called, "ask"))
        call = ToolCall("x", "sensitive_extension", "{}")
        self.assertEqual(host.execute(call, host.ctx, Ledger(self.settings.limits))["error"], "TOOL_GROUP_NOT_SELECTED")
        host.select_tool_groups(["extensions"])
        self.assertEqual(host.execute(call, host.ctx, Ledger(self.settings.limits))["error"], "PERMISSION_REQUIRED")
        called.assert_not_called()

    def test_each_user_turn_reclassifies_and_notifications_clear_old_route(self):
        host = self.host()
        host.submit("股价")
        host.submit("你好")
        self.assertNotIn("get_quote", self.names(host))
        self.assertEqual(len(self.seen), 1)
        host.submit()
        self.assertIn("read_research_report", self.names(host))
        self.assertIsNone(host.tool_route)

    def test_steering_reclassifies_and_history_excludes_current_message(self):
        host = self.host()
        host.submit("股价")
        user = json.loads(self.seen[0]["messages"][1]["content"])
        self.assertEqual(user["context"]["history"], [])
        host.enqueue("你好")
        host.prepare(host.messages, host.ctx)
        self.assertEqual(host.tool_route.intent, "conversation")
        self.assertNotIn("get_quote", self.names(host))

    def test_model_failure_keeps_main_running_and_does_not_start_research(self):
        self.classifier.classify = Mock(side_effect=AgentError("INTENT_UNAVAILABLE", "offline"))
        host = self.host()
        self.assertEqual(host.submit("不要启动研究，只看股价").status, "completed")
        self.assertEqual(host.tool_route.source, "fallback")
        self.assertFalse(host.store.records("work", host.session_id))

    def test_all_registered_tools_are_mapped_and_disabled_restores_all(self):
        host = self.host()
        known = COMMON_TOOLS.union(*GROUP_TOOLS.values())
        self.assertFalse(set(host.pool.tools) - known)
        host.settings.intent.enabled = False
        host.submit("股价")
        self.assertEqual(self.names(host), set(host.pool.tools))
        self.assertFalse(self.seen)

    def test_legacy_hint_history_contract(self):
        host = self.host()
        host.settings.intent.enabled = False
        host.settings.nlu.enabled = True
        captured = []
        host.hint_parser = lambda text, ctx: captured.append(ctx) or {}
        host.submit("第一条")
        host.submit("第二条")
        self.assertTrue(all(isinstance(v, str) for v in captured[-1]["history"]))
        self.assertLessEqual(len(captured[-1]["history"]), 3)


if __name__ == "__main__":
    unittest.main()
