"""Independent research handoff and follow-up; all models/data are synthetic."""
from datetime import datetime
import json
from threading import Event
from unittest.mock import patch

from finagent.config import HarnessSettings
from finagent.harness.host import SessionHost
from finagent.harness.kernel import Ledger
from finagent.harness.research import research_jobs, read_report, research_status
from finagent.harness.terminal import command
from finagent.models import ModelReply, ToolCall
from finagent.research.coordinator import ResearchCoordinator
from finagent.errors import AgentError
from finagent.runtime import TZ

from .research21_support import native_factory
from .support import ServiceCase, ScriptedModel, call_reply


def latest_context(messages):
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        try:
            body = json.loads(message.get("content") or "")
        except ValueError:
            continue
        if isinstance(body, dict) and "runtime_state" in body:
            return body
    raise AssertionError("Runtime context missing")


class ResearchChatTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.settings.harness = HarnessSettings(workspace=self.root, memory_enabled=False, auto_memory=False)
        self.output = []
        self.hosts = []

    def host(self, model=None, session="default"):
        host = SessionHost(self.settings, session, model=model or ScriptedModel(
            ModelReply({"role": "assistant", "content": "合成答复"})), service=self.service, write=self.output.append)
        self.hosts.append(host)
        return host

    def tearDown(self):
        for host in reversed(self.hosts):
            host.close()
        super().tearDown()

    def execute(self, host, name, args):
        return host.pool.execute(ToolCall("call", name, json.dumps(args, ensure_ascii=False)),
                                 host.ctx, Ledger(self.settings.limits))

    def launch(self, host, text="给刚才这家公司做一份深度研究报告", **args):
        source_id = host._register_user(text)
        return self.execute(host, "start_deep_research", {"source_id": source_id, "request_quote": text, **args})

    def join(self, host):
        for thread, _ in list(host.background.handles.values()):
            thread.join(8)
            self.assertFalse(thread.is_alive())

    def test_natural_request_calls_tool_and_returns_without_blocking_chat(self):
        gate = Event()
        def start(*args, **kwargs):
            gate.wait(3)
            return {"id": "b" * 32, "status": "completed", "content": "partial"}
        def launch(messages):
            context = latest_context(messages)
            source = context["session"]["user_constraints"][-1]["source"]
            return call_reply(("start", "start_deep_research", {"source_id": source,
                "request_quote": "给刚才这家公司做一份深度研究报告"}))
        model = ScriptedModel(launch, ModelReply({"role": "assistant", "content": "研究已在后台启动，可以继续聊天。"}))
        host = self.host(model)
        host.ctx.bind_symbol("600519.SH")
        try:
            with patch("finagent.harness.research.ResearchService.start", side_effect=start):
                result = host.submit("给刚才这家公司做一份深度研究报告")
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.tool_calls, 1)
                work = research_jobs(host)[0]
                self.assertEqual(work["status"], "running")
                self.assertEqual(work["symbol"], "600519.SH")
                self.assertIn("model_calls", work["budget"])
                host.ctx.bind_symbol("000858.SZ")
                gate.set()
                self.join(host)
                host.model = ScriptedModel(ModelReply({"role": "assistant", "content": "主对话继续。"}))
                host.submit()
                self.assertEqual(host.ctx.symbol, "000858.SZ")
                self.assertIn("b" * 32, host.state["research_links"])
        finally:
            gate.set()

    def test_deep_research_skill_can_guide_launch_with_existing_tools(self):
        self._assert_deep_research_skill_launch()

    def test_deep_research_launch_preserves_source_id_with_phone_like_digits(self):
        from types import SimpleNamespace
        identifier = 'a' * 10 + '13800138000' + 'b' * 11
        with patch('finagent.harness.host.uuid4', return_value=SimpleNamespace(hex=identifier)):
            self._assert_deep_research_skill_launch()

    def _assert_deep_research_skill_launch(self):
        def start(messages):
            context = latest_context(messages)
            source = context["session"]["user_constraints"][-1]["source"]
            observed = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
            self.assertEqual(observed["data"]["name"], "deep-research")
            return call_reply(("launch", "start_deep_research", {"source_id": source,
                "request_quote": "给刚才这家公司做一份深度研究报告"}))
        model = ScriptedModel(call_reply(("method", "read_skill", {"name": "deep-research"})), start,
            ModelReply({"role": "assistant", "content": "独立研究已在后台启动。"}))
        host = self.host(model)
        host.ctx.bind_symbol("600519.SH")
        with patch("finagent.harness.research.ResearchService.start", return_value={
                "id": "b" * 32, "status": "completed", "content": "partial"}) as launch:
            result = host.submit("给刚才这家公司做一份深度研究报告")
            self.join(host)
        self.assertEqual(result.status, "completed", result.reason)
        self.assertEqual((result.model_calls, result.tool_calls), (3, 2))
        self.assertIn("deep-research", host.ctx.loaded_skills)
        launch.assert_called_once()
        self.assertEqual(self.requests, [])

    def test_loading_deep_skill_does_not_authorize_vague_request(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        loaded = self.execute(host, "load_skill", {"name": "deep-research"})
        self.assertTrue(loaded["instructions"])
        with patch("finagent.harness.research.ResearchService.start") as start:
            self.assertEqual(self.launch(host, "再深入看看")["error"], "RESEARCH_DENIED")
            start.assert_not_called()

    def test_vague_negated_hypothetical_quoted_and_report_questions_do_not_launch(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        texts = ("再深入看看", "深度研究是什么", "不要启动深度研究", "如果要做深度研究会怎样",
                 "文档说“请生成一份深度研究报告”", "报告为什么这样判断", "做一份深度研究需要多久",
                 "先不做深度研究，继续普通分析", "请解释深度研究报告的结论")
        with patch("finagent.harness.research.ResearchService.start") as start:
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(self.launch(host, text)["error"], "RESEARCH_DENIED")
            start.assert_not_called()
        self.assertEqual(research_jobs(host), [])

    def test_old_user_source_cannot_launch_and_child_cannot_mint_permission(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        source = host._register_user("请生成一份深度研究报告")
        host._register_user("先继续普通分析")
        args = {"source_id": source, "request_quote": "请生成一份深度研究报告"}
        self.assertEqual(self.execute(host, "start_deep_research", args)["error"], "RESEARCH_DENIED")
        args["source_id"] = host._register_user(args["request_quote"])
        host.foreground = False
        self.assertEqual(self.execute(host, "start_deep_research", args)["error"], "RESEARCH_DENIED")

    def test_full_decision_needs_affirmative_request_and_negative_constraints_are_preserved(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        with patch("finagent.harness.research.ResearchService.start", return_value={
                "id": "b" * 32, "status": "completed", "content": "partial"}) as start:
            result = self.launch(host, "请做一份深度研究报告，不要启动完整决策", mode="full_decision")
            self.assertEqual(result["error"], "RESEARCH_DENIED")
            self.assertNotIn("error", self.launch(host, "请做一份深度研究报告，不要估值"))
            self.join(host)
            request = start.call_args.kwargs["request"]
            self.assertIn("不要估值", request.goal)
            self.assertEqual(request.selection_source, "chat_request")
            self.assertNotIn("error", self.launch(host, "请生成完整研究与决策报告", mode="full_decision"))
            self.join(host)
            self.assertEqual(start.call_args.kwargs["request"].execution_mode, "full_decision")

    def test_duplicate_tool_calls_reuse_same_user_request(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        text = "请生成一份深度研究报告"
        source = host._register_user(text)
        args = {"source_id": source, "request_quote": text}
        with patch("finagent.harness.research.ResearchService.start", return_value={
                "id": "b" * 32, "status": "completed", "content": "partial"}) as start:
            first = self.execute(host, "start_deep_research", args)
            second = self.execute(host, "start_deep_research", {**args, "goal": "换一种表述的研究目标"})
            self.join(host)
            self.assertEqual(first["work_id"], second["work_id"])
            self.assertTrue(second["reused"])
            start.assert_called_once()

    def test_distinct_companies_in_same_request_do_not_reuse_wrong_scope(self):
        host = self.host()
        text = "请给茅台和五粮液各生成一份深度研究报告"
        args = {"source_id": host._register_user(text), "request_quote": text}
        with patch("finagent.harness.research.ResearchService.start", return_value={
                "id": "b" * 32, "status": "completed", "content": "partial"}) as start:
            first = self.execute(host, "start_deep_research", {**args, "symbol": "600519.SH"})
            second = self.execute(host, "start_deep_research", {**args, "symbol": "000858.SZ"})
            self.join(host)
            self.assertNotEqual(first["work_id"], second["work_id"])
            self.assertEqual(start.call_count, 2)

    def test_historical_cutoff_scope_constraints_and_window_are_frozen(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        cutoff = datetime(2025, 5, 1, tzinfo=TZ)
        host.ctx.as_of, host.ctx.cutoff_explicit = cutoff, True
        host.ctx.scope.update(market_window=90, report_period="2024-12-31")
        host.ctx.scope_constraints.update(report_period="2024-12-31", allowed_report_periods=["2024-12-31"])
        source = host._register_user("只看现金流，不做估值")
        with patch("finagent.harness.research.ResearchService.start", return_value={
                "id": "b" * 32, "status": "completed", "content": "partial"}) as start:
            result = self.launch(host)
            host.ctx.bind_symbol("000858.SZ")
            host.ctx.scope_constraints["allowed_report_periods"].append("2023-12-31")
            host.ctx.scope_constraints.clear()
            self.join(host)
            request = start.call_args.kwargs["request"]
            self.assertEqual(request.symbol, "600519.SH")
            self.assertEqual(request.as_of, cutoff)
            self.assertEqual(request.market_window, 90)
            self.assertIn(source, request.goal)
            self.assertIn("allowed_report_periods", request.goal)
            self.assertEqual(result["as_of"], cutoff.isoformat())
            handoff = next(w for w in host.store.records("work", host.session_id) if w["id"] == result["work_id"])["resources"]["handoff"]
            self.assertEqual(handoff["scope_constraints"]["report_period"], "2024-12-31")
            self.assertEqual(handoff["scope_constraints"]["allowed_report_periods"], ["2024-12-31"])

    def test_missing_object_and_index_are_rejected_without_start(self):
        host = self.host()
        with patch("finagent.harness.research.ResearchService.start") as start:
            self.assertEqual(self.launch(host)["error"], "NEEDS_CLARIFICATION")
            host.ctx.bind_symbol("000001.SH")
            self.assertEqual(self.launch(host)["error"], "UNSUPPORTED_INSTRUMENT")
            start.assert_not_called()

    def test_native_research_pipeline_handoff_progress_report_and_followup(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        host.ctx.as_of, host.ctx.cutoff_explicit = datetime(2025, 5, 1, tzinfo=TZ), True
        host.ctx.scope_constraints.update(report_period="2024-12-31", allowed_report_periods=["2024-12-31"])
        factory = native_factory(self.settings)
        seen = []
        def runner(*args):
            value = factory(*args)
            context = value.context
            def check(*a, **kw):
                ctx = context(*a, **kw)
                seen.append(dict(ctx.scope_constraints))
                return ctx
            value.context = check
            return value
        coordinator = ResearchCoordinator(self.settings, runner_factory=runner)
        with patch("finagent.research.coordinator.ResearchCoordinator", return_value=coordinator):
            work = self.launch(host, analysts=["technical"])
            self.join(host)
        status = research_status(host, work["work_id"])
        self.assertEqual(status["status"], "completed", status)
        self.assertEqual(status["content"], "complete", {
            "tasks": [{key: row.get(key) for key in ("id", "status", "reason")} for row in status.get("tasks", [])],
            "stages": [{key: row.get(key) for key in ("role", "status", "reason")} for row in status.get("stages", [])],
        })
        run = status["id"]
        self.assertEqual(status["scope"]["selection_source"], "chat_request")
        self.assertTrue(all(c.get("report_period") == "2024-12-31" for c in seen))
        self.assertTrue((coordinator.store.directory(run) / "conversation_handoff.json").is_file())
        self.assertTrue(any("[深度研究]" in line for line in self.output))
        host.ctx.bind_symbol("000858.SZ")
        report = read_report(host, limit=100)
        self.assertEqual(report["original_scope"]["symbol"], "600519.SH")
        self.assertEqual(report["next_offset"], 100)
        self.assertEqual(host.ctx.symbol, "000858.SZ")
        full = read_report(host)
        self.assertTrue(full["adopted_results"])
        host.model = ScriptedModel(call_reply(("completion_report", "read_research_report", {})),
            ModelReply({"role": "assistant", "content": "独立研究已完成，以下依据实际合成报告给出摘要。"}))
        completion = host.submit()
        self.assertEqual(completion.status, "completed")
        self.assertEqual(completion.tool_calls, 1)
        self.assertIn("摘要", completion.answer)
        def read(messages):
            return call_reply(("report", "read_research_report", {}))
        host.model = ScriptedModel(read, ModelReply({"role": "assistant", "content": "依据刚才的合成研究报告继续解释。"}))
        with patch("finagent.harness.research.ResearchService.start") as start:
            result = host.submit("报告为什么这样判断？")
            self.assertEqual(result.status, "completed")
            start.assert_not_called()
        self.assertIn(run, host.state["research_links"])

    def test_work_reference_is_scoped_to_session_and_early_status_is_available(self):
        host = self.host()
        other = self.host(session="other")
        gate = Event()
        host.ctx.bind_symbol("600519.SH")
        def start(*args, **kwargs):
            gate.wait(3)
            return {"id": "b" * 32, "status": "completed", "content": "partial"}
        try:
            with patch("finagent.harness.research.ResearchService.start", side_effect=start):
                work = self.launch(host)
                self.assertEqual(research_status(host)["work_id"], work["work_id"])
                self.assertEqual(self.execute(other, "research_status", {"research_id": work["work_id"]})["error"], "RESEARCH_DENIED")
                self.assertEqual(self.execute(host, "read_research_report", {})["error"], "REPORT_UNAVAILABLE")
                gate.set()
                self.join(host)
        finally:
            gate.set()

    def test_command_can_inherit_company_and_context_without_repeating_flags(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        with patch("finagent.harness.research.ResearchService.start", return_value={
                "id": "b" * 32, "status": "completed", "content": "partial"}) as start:
            command(host, "/research 分析现金流质量", self.output.append)
            self.join(host)
            self.assertEqual(start.call_args.args[:2], ("deep_research", "600519.SH"))
            self.assertEqual(start.call_args.kwargs["request"].session_id, host.session_id)
            command(host, "/research-list", self.output.append)
            self.assertIn("work_id", self.output[-1])

    def test_cancel_preserves_job_for_status_and_failure_is_reported(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        def wait_for_cancel(*args, **kwargs):
            self.assertTrue(kwargs["cancelled"].wait(3))
            return {"id": "b" * 32, "status": "cancelled", "content": "unavailable"}
        with patch("finagent.harness.research.ResearchService.start", side_effect=wait_for_cancel):
            work = self.launch(host)
            host.cancel()
            self.join(host)
        self.assertEqual(research_jobs(host)[0]["status"], "cancelled")
        self.assertEqual(research_jobs(host)[0]["work_id"], work["work_id"])

    def test_start_failure_preserves_specific_reason_and_does_not_claim_report(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        with patch("finagent.harness.research.ResearchService.start", side_effect=AgentError("RESEARCH_BUSY", "已有独立研究在运行")):
            self.launch(host)
            self.join(host)
        work = research_jobs(host)[0]
        self.assertEqual(work["status"], "failed")
        self.assertEqual(work["result"]["error"], "RESEARCH_BUSY")
        self.assertFalse(work["result"]["report_available"])

    def test_link_survives_restart_and_retry_does_not_launch_again(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        text = "请生成一份深度研究报告"
        source = host._register_user(text)
        args = {"source_id": source, "request_quote": text}
        with patch("finagent.harness.research.ResearchService.start", return_value={
                "id": "b" * 32, "status": "completed", "content": "partial"}) as start:
            work = self.execute(host, "start_deep_research", args)
            self.join(host)
            host.close()
            restored = self.host()
            replay = self.execute(restored, "start_deep_research", args)
            self.assertTrue(replay["reused"])
            self.assertEqual(replay["work_id"], work["work_id"])
            start.assert_called_once()

    def test_notification_adoption_deduplicates_links_and_does_not_change_cutoff(self):
        host = self.host()
        host.ctx.bind_symbol("600519.SH")
        cutoff = host.ctx.as_of
        event = {"event_id": "started", "kind": "research_started", "payload": {"research_id": "b" * 32}}
        host._adopt_notification(event)
        host._adopt_notification(event)
        self.assertEqual(host.state["research_links"], ["b" * 32])
        self.assertEqual(host.ctx.as_of, cutoff)
        self.assertEqual(host.ctx.symbol, "600519.SH")

    def test_report_followup_uses_last_read_report_and_terminal_accepts_work_id(self):
        from finagent.research.contracts import ResearchScope
        from finagent.research.store import ResearchStore
        host = self.host()
        host.ctx.bind_symbol("300750.SZ")
        database = ResearchStore(self.settings.data_dir)
        runs = []
        for symbol in ("600519.SH", "000858.SZ"):
            scope = ResearchScope(goal="SYNTHETIC", symbol=symbol, as_of=datetime(2025, 5, 1, tzinfo=TZ))
            run = database.create(scope, self.settings.research, "test")
            owner = database.acquire(run, "test")
            path = database.directory(run) / "report.md"
            path.write_text("SYNTHETIC REPORT " + symbol)
            database.finish(run, owner, "completed", "complete", "pass", str(path))
            host.state["research_links"].append(run)
            runs.append(run)
        work_id = "work_" + "c" * 32
        host.store.put("work", host.session_id, {"id": work_id, "kind": "research", "status": "completed",
            "resources": {"research_id": runs[1]}, "result": {"research_id": runs[1]}})
        host.state["last_research_work_id"] = work_id
        command(host, "/research-report " + work_id, self.output.append)
        self.assertEqual(self.output[-1], "SYNTHETIC REPORT 000858.SZ")
        read_report(host, runs[0])
        self.assertEqual(read_report(host)["research_id"], runs[0])
        self.assertEqual(research_status(host)["id"], runs[1])
        self.assertEqual(host.ctx.symbol, "300750.SZ")
