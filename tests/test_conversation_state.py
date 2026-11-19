"""Conversation continuity uses TODOs and checkpoints without Task Frame tools."""
from copy import deepcopy
import json
from unittest.mock import patch

from finagent.config import HarnessSettings
from finagent.harness.host import SessionHost
from finagent.harness.kernel import Ledger
from finagent.harness.legacy_session import migrate_session
from finagent.models import ModelReply, ToolCall

from .support import ServiceCase, ScriptedModel, call_reply


class ConversationStateTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.settings.harness.memory_enabled = False
        self.settings.harness.auto_memory = False
        self.settings.fact_check.enabled = False
        self.settings.intent.enabled = False
        self.hosts = []

    def tearDown(self):
        for host in self.hosts:
            host.close()
        super().tearDown()

    def host(self, model=None, session="conversation"):
        host = SessionHost(self.settings, session, service=self.service,
            model=model or ScriptedModel(ModelReply({"role":"assistant", "content":"合成答复"})))
        self.hosts.append(host)
        return host

    def execute(self, host, name, args):
        return host.pool.execute(ToolCall("test", name, json.dumps(args, ensure_ascii=False)),
                                 host.ctx, Ledger(self.settings.limits))

    def test_direct_financial_query_needs_no_frame_and_retired_tools_are_unavailable(self):
        host = self.host(ScriptedModel(
            call_reply(("quote", "get_quote", {"symbol":"600519.SH"})),
            ModelReply({"role":"assistant", "content":"已读取合成快照。"})))
        result = host.submit("查询茅台行情")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("task_frames", host.state)
        for name in ("update_task_frame", "read_task_frames"):
            self.assertNotIn(name, host.pool.tools)
            self.assertNotIn(name, {t["function"]["name"] for t in host.schemas(host.ctx)})

    def test_todo_survives_restart_and_is_in_runtime_context(self):
        host = self.host()
        todos = [{"content":"比较两家公司2023年现金流", "status":"in_progress"},
                 {"content":"核验口径并整理结论", "status":"pending"}]
        self.assertEqual(self.execute(host, "todo_write", {"todos":todos})["todos"], todos)
        host.checkpoint()
        host.close()
        resumed = self.host()
        view = json.loads(resumed.model_messages([], resumed.ctx)[1]["content"])
        self.assertEqual(view["session"]["todos"], todos)
        self.assertNotIn("task_understanding", view)
        self.assertNotIn("active_task_frame_id", view["session"])

    def test_reminder_still_triggers_for_existing_todos_only(self):
        host = self.host()
        host.ctx.model_attempts = 3
        host.prepare(host.messages, host.ctx)
        self.assertFalse(any("宿主计划提醒" in str(m.get("content")) for m in host.messages))
        self.execute(host, "todo_write", {"todos":[{"content":"核验现金流", "status":"pending"}]})
        host.ctx.model_attempts += 3
        host.prepare(host.messages, host.ctx)
        self.assertEqual(sum("宿主计划提醒" in str(m.get("content")) for m in host.messages), 1)
        host.prepare(host.messages, host.ctx)
        self.assertEqual(sum("宿主计划提醒" in str(m.get("content")) for m in host.messages), 1)

    def test_legacy_checkpoint_cannot_restore_removed_scope_or_tool_definitions(self):
        host = self.host()
        todos = [{"content":"按用户新要求查询2023年", "status":"pending"}]
        host.state.update(todos=todos, task_frames={"old":{"revision":1}}, active_task_frame_id="old")
        host.state["_tool_catalog"]={"update_task_frame":{"function":{"name":"update_task_frame"}}}
        host.ctx.scope.update(task_frame_id="old", report_period="2024-12-31", metric="revenue", trade_date="2026-11-01")
        host.ctx.scope_constraints.update(allowed_report_periods=["2024-12-31"], task_exclude=["股价"])
        host.messages.append({"role":"user", "_finagent_origin":"runtime_context", "content":json.dumps({
            "session":{"task_frames":{"old":{}}, "active_task_frame_id":"old", "todos":todos},
            "task_understanding":{"active_task_id":"old"}, "scope":dict(host.ctx.scope),
            "scope_constraints":dict(host.ctx.scope_constraints)})})
        host.checkpoint()
        host.close()
        resumed = self.host()
        self.assertNotIn("task_frames", resumed.state)
        self.assertNotIn("report_period", resumed.ctx.scope)
        self.assertNotIn("allowed_report_periods", resumed.ctx.scope_constraints)
        self.assertEqual(resumed.ctx.scope["trade_date"], "2026-11-01")
        self.assertEqual(resumed.state["todos"], todos)
        for message in resumed.messages:
            if message.get("_finagent_origin") == "runtime_context":
                self.assertNotIn("task_understanding", json.loads(message["content"]))
        self.assertNotIn("update_task_frame", {t["function"]["name"] for t in resumed.schemas(resumed.ctx)})

    def test_research_handoff_freezes_todos_and_keeps_explicit_request_guard(self):
        host = self.host()
        host.ctx.symbol = "600519.SH"
        todos = [{"content":"研究2024年现金流", "status":"in_progress"}]
        self.execute(host, "todo_write", {"todos":todos})
        source = host._register_user("请给茅台做一份深度研究报告")
        with patch("finagent.harness.research.ResearchService.start", return_value={
                "id":"e" * 32, "status":"completed", "content":"partial"}) as start:
            result = self.execute(host, "start_deep_research", {"source_id":source,
                "request_quote":"请给茅台做一份深度研究报告", "analysts":["fundamentals"]})
            for thread, _ in list(host.background.handles.values()):
                thread.join(5)
            self.assertNotIn("error", result)
            start.assert_called_once()
        self.execute(host, "todo_write", {"todos":[]})
        handoff = host.store.records("work", host.session_id)[0]["resources"]["handoff"]
        self.assertEqual(handoff["todos"], todos)
        self.assertNotIn("task_frame", handoff)
        source = host._register_user("深度研究是什么")
        denied = self.execute(host, "start_deep_research", {"source_id":source, "request_quote":"深度研究是什么"})
        self.assertEqual(denied["error"], "RESEARCH_DENIED")


class LegacySessionMigrationTests(ServiceCase):
    def test_existing_configs_accept_but_do_not_retain_removed_flag(self):
        config = HarnessSettings.model_validate({"task_frames_required":True})
        self.assertNotIn("task_frames_required", config.model_dump())

    def test_non_frame_scope_survives_and_migration_is_idempotent(self):
        saved = {"state":{"todos":[{"content":"继续查询", "status":"pending"}]},
                 "context":{"scope":{"report_period":"2023-12-31"},
                            "scope_constraints":{"allowed_report_periods":["2023-12-31"]}}, "messages":[]}
        original = deepcopy(saved)
        migrate_session(saved)
        migrate_session(saved)
        self.assertEqual(saved, original)
