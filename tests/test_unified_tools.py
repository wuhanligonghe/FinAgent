"""Cross-scenario registration, authorization, budget and evidence contracts."""
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from pydantic import Field

from finagent.config import HarnessSettings, StrictModel
from finagent.data.contracts import Observation
from finagent.errors import AgentError
from finagent.harness.host import SessionHost
from finagent.harness.kernel import Ledger
from finagent.harness.tools import RuntimeTool, ToolPool
from finagent.models import ModelReply, ToolCall
from finagent.research.runner import DataBroker
from finagent.runtime import ExecutionContext
from finagent.tools.perception import build_registry
from finagent.tools.registry import Tool, ToolExecutionPolicy, ToolRegistry
from tests.support import ScriptedModel, ServiceCase


class NumberArgs(StrictModel):
    value: int = Field(ge=0, strict=True)


class UnifiedRegistryTests(unittest.TestCase):
    def test_typed_and_schema_tools_share_dispatch_and_keep_their_results(self):
        ctx, registry = ExecutionContext(), ToolRegistry()
        typed = Mock(side_effect=lambda a, c: Observation(status="ok", data={"value": a.value}))
        control = Mock(side_effect=lambda a, c: {"accepted": a["value"]})
        registry.register(Tool("typed", "typed", NumberArgs, typed))
        registry.register(Tool("control", "control", NumberArgs.model_json_schema(), control,
                               perception=False, output_model=None))
        self.assertEqual(registry.execute("typed", '{"value":2}', ctx).data, {"value": 2})
        self.assertEqual(registry.execute("control", {"value": 3}, ctx), {"accepted": 3})
        self.assertIsInstance(typed.call_args.args[0], NumberArgs)
        self.assertIsInstance(control.call_args.args[0], dict)
        for name in ("typed", "control"):
            self.assertEqual(registry.tools[name].schema()["function"]["parameters"], NumberArgs.model_json_schema())
        for arguments in ({"value": -1}, {"value": "3"}, {"value": 1, "extra": True}, "not-json"):
            for name in ("typed", "control"):
                self.assertEqual(registry.execute(name, arguments, ctx).status, "error")
        typed.assert_called_once()
        control.assert_called_once()

    def test_invalid_domain_output_is_rejected_and_error_text_is_sanitized(self):
        registry = ToolRegistry()
        registry.register(Tool("bad_output", "bad", NumberArgs, lambda a, c: {"status": "ok", "data": []}))
        registry.register(Tool("bad_handler", "bad", NumberArgs,
            Mock(side_effect=RuntimeError("private-token-and-url"))))
        self.assertEqual(registry.execute("bad_output", {"value": 1}, ExecutionContext()).error.code, "VALIDATION_ERROR")
        error = registry.execute("bad_handler", {"value": 1}, ExecutionContext())
        self.assertEqual(error.error.code, "INTERNAL_ERROR")
        self.assertNotIn("private-token-and-url", error.model_dump_json())

    def test_runtime_io_errors_keep_the_previous_code_without_exposing_paths(self):
        registry = ToolRegistry(policy=ToolExecutionPolicy(error_format="dict"))
        registry.register(Tool("file", "file", {}, Mock(side_effect=FileNotFoundError("private-path")),
                               perception=False, output_model=None))
        value = registry.execute("file", {}, ExecutionContext())
        self.assertEqual(value["error"], "TOOL_ERROR")
        self.assertEqual(value["message"], "FileNotFoundError")
        self.assertNotIn("private-path", json.dumps(value))

    def test_duplicate_and_external_schema_references_are_rejected_for_all_tools(self):
        registry = ToolRegistry()
        registry.register(Tool("one", "one", NumberArgs, lambda a, c: None))
        with self.assertRaises(ValueError):
            registry.register(Tool("one", "duplicate", NumberArgs, lambda a, c: None))
        for reference in ("file:///etc/passwd", "https://example.invalid/schema"):
            with self.assertRaises(ValueError):
                registry.register(Tool("external", "external", {"$ref": reference}, lambda a, c: None))
        class ExternalArgs(StrictModel):
            value: str

            @classmethod
            def model_json_schema(cls, **kwargs):
                return {"type": "object", "properties": {"value": {"$ref": "file:///etc/passwd"}}}
        with self.assertRaises(ValueError):
            registry.register(Tool("external_typed", "external", ExternalArgs, lambda a, c: None))

    def test_role_view_preserves_permissions_and_cannot_call_omitted_tools(self):
        called = Mock(return_value={"ok": True})
        permission = Mock()
        permission.check.side_effect = AgentError("PERMISSION_REQUIRED", "denied")
        registry = ToolRegistry(policy=ToolExecutionPolicy(permissions=permission))
        registry.register(Tool("restricted", "restricted", {}, called, permission="ask", output_model=None))
        registry.register(Tool("omitted", "omitted", {}, called, output_model=None))
        view = registry.view(["restricted"])
        self.assertEqual(view.execute("restricted", {}, ExecutionContext()).error.code, "PERMISSION_REQUIRED")
        self.assertEqual(view.execute("omitted", {}, ExecutionContext()).error.code, "UNKNOWN_TOOL")
        called.assert_not_called()
        view.tools["restricted"].permission = "read"
        self.assertEqual(registry.tools["restricted"].permission, "ask")

    def test_no_permission_manager_does_not_authorize_sensitive_tools(self):
        registry, handler = ToolRegistry(), Mock()
        for permission in ("ask", "workspace"):
            registry.register(Tool(permission, "restricted", {}, handler,
                                   permission=permission, output_model=None))
            result = registry.execute(permission, {}, ExecutionContext())
            self.assertEqual(result.error.code, "PERMISSION_REQUIRED")
        handler.assert_not_called()

    def test_approval_wait_is_excluded_from_context_and_ledger_time(self):
        permissions = Mock()
        registry = ToolRegistry(policy=ToolExecutionPolicy(permissions=permissions))
        registry.register(Tool("ask", "ask", {}, lambda a, c: {"ok": True}, permission="ask", output_model=None))
        ctx = ExecutionContext()
        ledger = Ledger(ctx.limits)
        started, budget_started = ctx.started, ledger.started
        with patch("finagent.tools.registry.monotonic", side_effect=[10, 15]):
            self.assertEqual(registry.execute("ask", {}, ctx, ledger), {"ok": True})
        self.assertEqual(ctx.started, started + 5)
        self.assertEqual(ledger.started, budget_started + 5)

    def test_research_reservation_is_once_and_settled_on_error_and_interrupt(self):
        registry = ToolRegistry()
        handler = Mock(return_value=Observation(status="ok"))
        registry.register(Tool("read", "read", NumberArgs, handler))
        for arguments, failure in (({"value": 1}, None), ({}, None), ({"value": 1}, KeyboardInterrupt())):
            with self.subTest(arguments=arguments, failure=type(failure).__name__):
                budget = Mock()
                budget.remaining.return_value = 360
                budget.reserve.return_value = "reservation"
                ctx = ExecutionContext(research_budget=budget, research_task="analyst")
                ledger = Ledger(ctx.limits)
                handler.side_effect = failure
                if failure:
                    with self.assertRaises(KeyboardInterrupt):
                        registry.execute("read", arguments, ctx, ledger)
                else:
                    registry.execute("read", arguments, ctx, ledger)
                budget.reserve.assert_called_once_with("tool", "analyst", False)
                budget.settle.assert_called_once_with("reservation")
                self.assertIsNone(ctx.operation_ledger)

    def test_legacy_constructor_and_call_use_the_same_authorized_dispatch(self):
        permissions = Mock()
        registry = ToolPool(policy=ToolExecutionPolicy(permissions=permissions, error_format="dict"))
        handler = Mock(return_value={"ok": True})
        tool = RuntimeTool("legacy", "legacy", {"type": "object"}, handler, "ask")
        registry.register(tool)
        ctx = ExecutionContext()
        ledger = Ledger(ctx.limits)
        call = ToolCall("legacy-call", "legacy", "{}")
        self.assertEqual(registry.execute(call, ctx, ledger, False), {"ok": True})
        permissions.check.assert_called_once_with("legacy", {}, "ask", False)
        handler.assert_called_once_with({}, ctx, ledger)
        self.assertIsInstance(tool, Tool)
        self.assertIs(ToolPool, ToolRegistry)

    def test_data_broker_wrapping_keeps_tool_authorization_and_output_contract(self):
        registry = ToolRegistry()
        registry.register(Tool("get_company_profile", "profile", NumberArgs,
            lambda a, c: Observation(status="ok"), permission="ask"))
        original = registry.tools["get_company_profile"]
        DataBroker().wrap(registry, Mock(scope_hash="frozen"))
        wrapped = registry.tools["get_company_profile"]
        self.assertEqual(wrapped.permission, original.permission)
        self.assertIs(wrapped.output_model, original.output_model)
        self.assertEqual(registry.execute("get_company_profile", {"value": 1}, ExecutionContext()).error.code,
                         "PERMISSION_REQUIRED")


class UnifiedHostTests(ServiceCase):
    def host(self, registry=None):
        self.settings.harness = HarnessSettings(workspace=self.root, memory_enabled=False, auto_memory=False)
        self.settings.intent.enabled = False
        self.settings.model.tool_loading = "deferred"
        with patch("finagent.harness.host.build_registry", return_value=registry or build_registry(self.service)):
            host = SessionHost(self.settings, model=ScriptedModel(ModelReply({"role": "assistant", "content": "完成"})),
                               service=self.service)
        self.addCleanup(host.close)
        return host

    def execute(self, host, name, args):
        return host.execute(ToolCall("direct", name, json.dumps(args)), host.ctx, Ledger(self.settings.limits))

    def test_finance_definition_executes_once_with_chat_and_research_contracts(self):
        registry = build_registry(self.service)
        original = registry.tools["get_quote"]
        handler = Mock(wraps=original.handler)
        original.handler = handler
        host = self.host(registry)
        value = self.execute(host, "get_quote", {"symbol": "600519.SH"})
        self.assertIs(host.registry, host.pool)
        self.assertIs(host.registry, host.finance)
        self.assertIs(host.registry.tools["get_quote"], original)
        handler.assert_called_once()
        self.assertIn(value["status"], {"ok", "partial"})
        self.assertIn(value["result_id"], host.ctx.evidence_ids)
        self.assertEqual(host.ctx.result_tools[value["result_id"]], "get_quote")
        self.assertEqual(len(self.requests), 1)
        research = ToolRegistry()
        research.register(original)
        self.assertEqual(research.tools["get_quote"].schema(), host.registry.tools["get_quote"].schema())
        observation = research.execute("get_quote", {"symbol": "600519.SH"}, ExecutionContext())
        self.assertIsInstance(observation, Observation)
        self.assertEqual(observation.data, value["data"])

    def test_domain_handler_runs_without_task_frame_submission(self):
        host = self.host()
        handler = Mock(wraps=host.registry.tools["get_quote"].handler)
        host.registry.tools["get_quote"].handler = handler
        self.assertIsNone(self.execute(host, "get_quote", {"symbol": "600519.SH"}).get("error"))
        handler.assert_called_once()
        self.assertEqual(len(self.requests), 1)

    def test_large_domain_result_is_saved_and_can_be_read_in_full(self):
        host = self.host()
        large = "evidence" * 5000
        tool = host.registry.tools["get_company_profile"]
        host.registry.tools[tool.name] = replace(tool, handler=lambda a, c: Observation(status="ok", data={"text": large}))
        value = self.execute(host, tool.name, {"symbol": "600519.SH"})
        self.assertEqual(value["read_full_result_with"], value["result_id"])
        self.assertTrue(Path(value["data"]["full_output"]).is_file())
        self.assertEqual(host.service.store.load_result(value["result_id"]).data["text"], large)

    def test_control_results_and_discovery_do_not_become_financial_observations(self):
        host = self.host()
        control = self.execute(host, "todo_write", {"todos": []})
        self.assertEqual(control["todos"], [])
        self.assertNotIn("result_id", control)
        discovered = host.search_tools("get_quote")
        self.assertIn("get_quote", discovered["discovered_tools"])
        self.assertNotIn("write_file", host.finance_tool_names)
        self.assertNotIn("start_deep_research", host.finance_tool_names)
        self.assertNotIn("start_stock_research", host.registry.tools)
        self.assertTrue(host.finance_tool_names.issubset(host.registry.tools))

    def test_domain_failure_remains_a_saved_observation(self):
        host = self.host()
        tool = host.registry.tools["get_company_profile"]
        host.registry.tools[tool.name] = replace(tool, handler=Mock(side_effect=AgentError("SOURCE_FAILED", "source unavailable")))
        value = self.execute(host, tool.name, {"symbol": "600519.SH"})
        self.assertEqual(value["error"]["code"], "SOURCE_FAILED")
        self.assertIn(value["result_id"], host.ctx.evidence_ids)
