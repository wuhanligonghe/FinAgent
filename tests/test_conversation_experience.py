"""Offline dialogue/presentation regressions; no live model or market calls."""
import json

from finagent.agent import AgentLoop, RunRequest
from finagent.config import Limits
from finagent.presentation import ProgressReporter, format_answer, format_sources
from finagent.session import Conversation, compact_messages, recent_dialogue

from .support import ServiceCase, ScriptedModel, call_reply, final_reply
from .test_agent import cite_last
from . import test_chat


class DialogueTests(ServiceCase):
    def test_history_survives_restart_without_granting_evidence_permissions(self):
        session = Conversation(self.store)
        first = AgentLoop(ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519"})), cite_last),
                          self.registry, self.store).run(RunRequest("查茅台行情"), session.context("查茅台行情", Limits()))
        session.remember(first)
        restored = Conversation(self.store)
        ctx = restored.context("那现在呢", Limits())
        self.assertEqual(ctx.conversation_history[-1]["user"], "查茅台行情")
        self.assertTrue(ctx.conversation_history[-1]["assistant"])
        self.assertFalse(ctx.evidence_ids)
        self.assertFalse(ctx.result_tools)
        model = ScriptedModel(final_reply(first.evidence_ids, answer="沿用历史证据"))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("那现在呢"), ctx)
        self.assertNotEqual(result.status, "completed")
        self.assertIn("DATA_UNAVAILABLE", result.reason)
        users = [m["content"] for m in model.messages_seen[0] if m["role"] == "user"]
        memory = json.loads(users[0])
        self.assertIn("conversation_history", memory)
        self.assertEqual(users[-1], "那现在呢")
        self.assertNotIn(first.evidence_ids[0], users[0])

    def test_recent_history_is_bounded_and_has_no_old_tool_ids(self):
        turns = [{"user": "短一点" + "x" * 2000,
                  "assistant": "result_" + "a" * 32 + "y" * 3000,
                  "status": "completed"} for _ in range(9)]
        excerpt = recent_dialogue(turns)
        self.assertEqual(len(excerpt), 4)
        self.assertLessEqual(len(excerpt[0]["user"]), 1200)
        self.assertLessEqual(len(excerpt[0]["assistant"]), 1800)
        self.assertIn("[历史引用]", excerpt[0]["assistant"])

    def test_forget_removes_dialogue_and_scope(self):
        session = Conversation(self.store)
        result = AgentLoop(ScriptedModel(final_reply(answer="你好！")), self.registry, self.store).run(RunRequest("你好"))
        session.remember(result)
        self.assertTrue(Conversation(self.store).context("你好", Limits()).conversation_history)
        session.forget()
        ctx = Conversation(self.store).context("你好", Limits())
        self.assertEqual(ctx.conversation_history, [])
        self.assertIsNone(ctx.symbol)
        self.assertTrue((result.run_dir / "result.json").is_file())

    def test_unverified_answers_are_not_recalled_as_conclusions(self):
        model = ScriptedModel(final_reply(answer="未经取证的财务结论"))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("查茅台股价"))
        session = Conversation(self.store)
        session.remember(result)
        turn = session.state["history"][-1]
        self.assertEqual(turn["assistant"], "")
        self.assertEqual(turn["user"], "查茅台股价")

    def test_user_clarification_is_retained_and_remember_is_idempotent(self):
        model = ScriptedModel(final_reply(status="needs_clarification", answer="希望详细还是简短？"),
                              final_reply(answer="明白，我会简短说明。"))
        result = AgentLoop(model, self.registry, self.store).run(
            RunRequest("概念解释", require_data=False), clarification_handler=lambda _: "简短一些")
        session = Conversation(self.store)
        session.remember(result)
        session.remember(result)
        self.assertEqual(len(session.state["history"]), 1)
        self.assertIn("简短一些", session.state["history"][0]["user"])

    def test_compaction_keeps_dialogue_and_current_goal_with_complete_pairs(self):
        prefix = [{"role": "system", "content": "rules"}, {"role": "system", "content": "scope"},
                  {"role": "user", "content": json.dumps({"conversation_history": [{"user": "短一点"}]})},
                  {"role": "user", "content": "当前问题"}]
        messages = list(prefix)
        for index in range(8):
            messages.append(call_reply((str(index), "unknown", {})).message)
            messages.append({"role": "tool", "tool_call_id": str(index), "content": "error"})
        ctx = Conversation(self.store).context("当前问题", Limits())
        compacted, changed = compact_messages(messages, ctx, self.store)
        self.assertTrue(changed)
        self.assertEqual(compacted[:4], prefix)
        requested = {c["id"] for m in compacted for c in m.get("tool_calls", [])}
        self.assertEqual(requested, {m["tool_call_id"] for m in compacted if m["role"] == "tool"})


class PresentationTests(ServiceCase):
    run_chat = test_chat.ChatTests.run_chat

    def test_session_hides_protocol_diagnostics_until_details_requested(self):
        from finagent.models import ModelReply
        _, output, records = self.run_chat(ScriptedModel(ModelReply({"role":"assistant","content":""})), ["解释概念", "/exit"])
        self.assertNotIn("EMPTY_RESPONSE", "\n".join(output))
        self.assertIn("EMPTY_RESPONSE", records[0]["reason"])
        _, output, _ = self.run_chat(ScriptedModel(ModelReply({"role":"assistant","content":""})), ["解释概念", "/details", "/exit"])
        self.assertIn("EMPTY_RESPONSE", "\n".join(output))

    def test_citation_labels_preserve_saved_answer_and_full_sources(self):
        def answer(messages):
            raw = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))["snapshot_id"]
            return final_reply([raw], answer="本轮已取得行情，来源 " + raw)
        model = ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519"})), answer)
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("查茅台行情"))
        raw = result.evidence_ids[0]
        self.assertIn(raw, result.answer)
        self.assertIn("[来源1]", format_answer(result))
        self.assertNotIn(raw, format_answer(result))
        self.assertIn(raw, format_sources(result, self.store))

    def test_commands_do_not_call_model_and_new_session_is_separate(self):
        from finagent.models import ModelReply
        model = ScriptedModel(final_reply(answer="你好！"), ModelReply({"role": "assistant", "content": "[]"}))
        _, output, _ = self.run_chat(model, ["/sources", "你好", "/sources", "/new fresh", "/details", "/exit"])
        self.assertEqual(model.index, 2)  # Greeting and discovery; commands add no calls.
        shown = "\n".join(output)
        self.assertIn("尚未引用外部数据", shown)
        self.assertIn('"last_result": null', shown)

    def test_report_leads_with_answer_and_folds_audit_details(self):
        result = AgentLoop(ScriptedModel(final_reply(answer="你好，今天想了解什么？")),
                           self.registry, self.store).run(RunRequest("你好"))
        report = (result.run_dir / "report.md").read_text(encoding="utf-8")
        main, audit = report.split("<details>", 1)
        self.assertIn(result.answer, main)
        self.assertNotIn("状态：", main)
        self.assertNotIn("范围详情", main)
        self.assertIn("来源与核验记录", audit)
        self.assertIn("状态：completed", audit)

    def test_failed_tool_does_not_claim_that_useful_data_was_obtained(self):
        def repeated(messages):
            index = len([m for m in messages if m["role"] == "tool"])
            return call_reply((f"q{index}", "unknown", {}))
        result = AgentLoop(ScriptedModel(repeated), self.registry, self.store).run(RunRequest("查行情"))
        self.assertIn("NO_PROGRESS", result.reason)
        self.assertNotIn("已取得的资料", result.answer)

    def test_progress_is_visible_before_model_receives_observation(self):
        output = []
        def finish(messages):
            self.assertTrue(any("正在查询行情" in line for line in output))
            return cite_last(messages)
        result = AgentLoop(ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519"})), finish),
                           self.registry, self.store).run(RunRequest("查茅台行情"), progress_handler=output.append)
        self.assertEqual(result.status, "completed")
        self.assertEqual(sum("正在查询行情" in line for line in output), 1)

    def test_progress_deduplicates_stages_and_does_not_expose_payloads(self):
        output = []
        reporter = ProgressReporter(output.append)
        for name in ("get_quote", "get_daily_bars", "get_quote"):
            reporter({"kind": "tool_started", "name": name, "arguments": "private-input"})
        for _ in range(3):
            reporter({"kind": "tool_result", "name": "get_quote",
                      "result": {"status": "error", "error": "private-error"}})
        self.assertEqual(len(output), 2)
        self.assertNotIn("private", "\n".join(output))

    def test_display_failure_does_not_interrupt_execution(self):
        def disconnected(_):
            raise RuntimeError("display disconnected")
        model = ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519"})), cite_last)
        result = AgentLoop(model, self.registry, self.store).run(
            RunRequest("查茅台行情"), progress_handler=disconnected)
        self.assertEqual(result.status, "completed")
