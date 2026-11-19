"""Observed tool progress is visible, bounded, and independent of model narration."""
import json
from pathlib import Path
import unittest

from pydantic import SecretStr

from finagent.config import HarnessSettings
from finagent.harness.host import SessionHost
from finagent.harness.progress import ToolProgressReporter
from finagent.harness.tools import RuntimeTool
from finagent.models import ModelReply
from tests.support import ServiceCase, ScriptedModel, call_reply


class ToolProgressTests(ServiceCase):
    def test_real_tool_events_and_todo_updates_reach_writer_in_order(self):
        self.settings.harness = HarnessSettings(workspace=self.root, memory_enabled=False, auto_memory=False)
        self.settings.model.api_key = SecretStr("synthetic-secret")
        todo = [{"content": "核对行情", "status": "in_progress"}]
        done = [{"content": "核对行情", "status": "completed"}]
        model = ScriptedModel(
            call_reply(("plan", "todo_write", {"todos": todo})),
            call_reply(("quote", "get_quote", {"symbol": "600519.SH"}),
                       ("bad", "fixture_error", {"query": "合成失败请求"})),
            call_reply(("done", "todo_write", {"todos": done})),
            ModelReply({"role": "assistant", "content": "查询完成"}))
        output = []
        host = SessionHost(self.settings, service=self.service, model=model, write=output.append)
        try:
            host.pool.register(RuntimeTool("fixture_error", "synthetic", {"type": "object"},
                lambda *_: {"error": {"code": "SOURCE_FAILED", "message": "synthetic-secret hidden-key private-result " * 10000}}))
            result = host.submit("查询茅台行情")
            self.assertEqual(result.status, "completed", result.reason)
            visible = "\n".join(output)
            self.assertIn("[工具] 调用 get_quote（symbol=600519.SH）", visible)
            self.assertIn("[工具] get_quote：", visible)
            self.assertIn("[工具] fixture_error：失败（SOURCE_FAILED）", visible)
            self.assertIn("[>] 核对行情", visible)
            self.assertIn("[x] 核对行情", visible)
            self.assertLess(visible.index("[>] 核对行情"), visible.index("调用 get_quote"))
            self.assertLess(visible.index("调用 get_quote"), visible.index("get_quote："))
            self.assertLess(visible.index("get_quote："), visible.index("[x] 核对行情"))
            for hidden in ("synthetic-secret", "hidden-key", "private-result"):
                self.assertNotIn(hidden, visible)
            events = host.store.load(host.session_id)
            self.assertEqual(events["state"]["todos"], done)
            full_error = events["state"]["failed_attempts"][-1]["error"]
            self.assertGreater(len(json.dumps(full_error)), 60000)
            state = next(json.loads(m["content"])["session"] for m in reversed(model.messages_seen[-1])
                         if m.get("role") == "user" and '"runtime_state"' in m.get("content", ""))
            failure = state["failed_attempts"][-1]["error"]
            self.assertEqual(json.loads(Path(failure["full_output"]).read_text()), full_error)
            self.assertLessEqual(len(failure["preview"]), 500)
        finally:
            host.close()

    def test_display_failure_does_not_fail_tool_execution(self):
        self.settings.harness = HarnessSettings(workspace=self.root, memory_enabled=False, auto_memory=False)
        def broken_write(_):
            raise OSError("closed terminal")
        host = SessionHost(self.settings, service=self.service, write=broken_write, model=ScriptedModel(
            call_reply(("todo", "todo_write", {"todos": []})),
            ModelReply({"role": "assistant", "content": "完成"})))
        try:
            self.assertEqual(host.submit("整理计划").status, "completed")
        finally:
            host.close()


class ProgressFormattingTests(unittest.TestCase):
    def test_arguments_are_allowlisted_redacted_and_bounded(self):
        reporter = ToolProgressReporter(lambda _: None, ("test-secret",))
        preview = reporter.parameters(json.dumps({
            "query": "test-secret token=private-token Bearer private-bearer\n\x1b[2J",
            "api_key": "private-key", "headers": {"Authorization": "private-header"},
            "content": "private-body", "command": "private-command", "path": "x" * 1000}))
        for hidden in ("test-secret", "private-token", "private-bearer", "private-key",
                       "private-header", "private-body", "private-command", "\n", "\x1b"):
            self.assertNotIn(hidden, preview)
        self.assertLess(len(preview), 250)
        for invalid in ("not-json", "[]", "null"):
            self.assertEqual(reporter.parameters(invalid), "")

    def test_results_distinguish_data_gaps_failures_and_unexecuted_calls(self):
        reporter = ToolProgressReporter(lambda _: None)
        for value, expected in (({"status": "ok"}, "完成"),
                                ({"status": "partial", "stale": True}, "返回部分数据"),
                                ({"status": "empty"}, "未查到可用数据"),
                                ({"error": "PERMISSION_REQUIRED"}, "失败"),
                                ({"error": "RUN_STOPPED", "executed": False}, "未执行"),
                                ({"returncode": 2, "stdout": "private-output"}, "执行失败"),
                                ({"exit_code": 2, "stdout": "private-output"}, "执行失败"),
                                ({"exit_code": 0, "stdout": "private-output"}, "完成"),
                                ({"status": "running"}, "正在后台运行"),
                                ("private-output", "已返回")):
            with self.subTest(value=value):
                result = reporter.format("tool_result", {"name": "tool", "call_id": "x", "result": value})
                self.assertIn(expected, result)
                self.assertNotIn("private-output", result)
        self.assertIsNone(reporter.format("model_started", {}))
