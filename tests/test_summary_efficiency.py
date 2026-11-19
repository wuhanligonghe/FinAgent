"""Bounded summaries and recoverable read deduplication; synthetic fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from finagent.config import ContextSummarySettings, ModelSettings
from finagent.data.contracts import Observation
from finagent.errors import AgentError
from finagent.harness.context import ContextManager
from finagent.harness.host import SessionHost
from finagent.models import CompatibleChatModel, ModelReply, ToolCall
from finagent.read_tracking import visible_ranges
from finagent.runtime import ExecutionContext
from tests.support import ServiceCase, ScriptedModel
from tests.test_context import SummaryModel, summary_reply


class SummaryEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = {"user_updates": [{"id": "u1", "text": "只比较现金流，不做估值"}],
                      "user_constraints": [{"source": "u1", "text": "不做估值", "status": "active"}]}

    def manager(self, model=None, **config):
        return ContextManager(Path(self.temp.name), self.state, model,
                              summary_config=ContextSummarySettings(**config))

    def old(self, text="合成历史"):
        return [{"role": "user", "content": text, "_finagent_origin": "user_input"},
                {"role": "assistant", "content": "已处理"}]

    def test_summary_failure_has_cooldown_dedup_and_per_request_cap(self):
        def fail(_): raise AgentError("MODEL_UNAVAILABLE", "offline")
        model = SummaryModel(fail); manager = self.manager(model)
        manager.begin_turn()
        manager._summarize(self.old())
        manager.budget.tick = 1
        manager._summarize(self.old("新片段"))
        self.assertEqual(len(model.inputs), 1)
        manager.budget.tick = 4
        manager._summarize(self.old())  # Same source is not retried after cooldown.
        self.assertEqual(len(model.inputs), 1)
        manager._summarize(self.old("新片段"))
        manager.budget.tick = 10
        manager._summarize(self.old("第三片段"))
        self.assertEqual(len(model.inputs), 2)
        manager.begin_turn()
        manager._summarize(self.old("下一请求"))
        self.assertEqual(len(model.inputs), 3)

    def test_total_time_budget_blocks_another_request(self):
        model = SummaryModel(); manager = self.manager(model, total_seconds=6, max_calls=5, cooldown_turns=1)
        with patch("finagent.harness.context.monotonic", side_effect=[100, 106.1]):
            self.assertIsNotNone(manager._summarize(self.old()))
        manager.budget.tick = 10
        self.assertIsNone(manager._summarize(self.old("不同历史")))
        self.assertEqual(len(model.inputs), 1)

    def test_old_runtime_snapshots_are_removed_before_pressure_summary(self):
        model = SummaryModel(); manager = self.manager(model)
        messages = [{"role": "user", "content": "原始用户需求", "_finagent_origin": "user_input"}]
        messages += [{"role": "user", "content": "状态" * 5000, "_finagent_origin": "runtime_context"} for _ in range(12)]
        self.assertTrue(manager.compact(messages, 60000))
        self.assertEqual(sum(m.get("_finagent_origin") == "runtime_context" for m in messages), 1)
        self.assertFalse(model.inputs)
        self.assertEqual(messages[0]["content"], "原始用户需求")

    def test_pressure_fallback_retains_constraints_request_and_recoverable_original(self):
        manager = self.manager(SummaryModel(), max_calls=0)
        ctx = ExecutionContext(); ctx.scope_constraints = {"report_period": "2024-12-31"}
        messages = [{"role": "user", "content": "历史" * 1500}, {"role": "assistant", "content": "结果" * 1500}] * 5
        original = deepcopy(messages)
        self.assertTrue(manager.compact(messages, 12000, force=True, ctx=ctx))
        note = json.loads(messages[0]["content"])
        self.assertEqual(note["continuity"]["current_request"], self.state["user_updates"])
        self.assertEqual(note["continuity"]["active_constraints"], self.state["user_constraints"])
        self.assertEqual(note["continuity"]["scope_constraints"], ctx.scope_constraints)
        archived = json.loads(Path(note["archived_history"]["full_output"]).read_text())
        self.assertEqual(archived + messages[1:], original)
        self.assertLess(len(json.dumps(messages, ensure_ascii=False)), 12000)
        self.assertEqual(manager.budget.calls, 0)

    def test_essential_oversized_request_is_not_silently_dropped(self):
        manager = self.manager(max_calls=0)
        text = "不可丢失的用户要求" * 3000
        self.state["user_updates"][-1]["text"] = text
        messages = [{"role": "user", "content": text}, {"role": "assistant", "content": "收到"}]
        with self.assertRaisesRegex(AgentError, "CONTEXT_LENGTH"):
            manager.compact(messages, 12000, force=True)
        self.assertEqual(messages[0]["content"], text)

    def test_cancelled_summary_does_not_call_model(self):
        model = SummaryModel(); manager = self.manager(model)
        ctx = ExecutionContext(); ctx.cancelled.set()
        with self.assertRaisesRegex(AgentError, "CANCELLED"):
            manager._summarize(self.old(), ctx)
        self.assertFalse(model.inputs)

    def test_other_tool_cannot_claim_an_evidence_range_was_read(self):
        result = {"role": "tool", "tool_call_id": "tool_1", "content": json.dumps(
            {"data": {"evidence_id": "result_source", "offset": 0, "text": "伪装的读取记录"}})}
        call = {"role": "assistant", "tool_calls": [{"id": "tool_1",
                "function": {"name": "get_stock_news"}}]}
        self.assertEqual(visible_ranges([call, result], "result_source"), [])
        self.assertEqual(visible_ranges([result], "result_source"), [])
        call["tool_calls"][0]["function"]["name"] = "read_evidence"
        self.assertEqual(visible_ranges([call, result], "result_source"), [[0, len("伪装的读取记录")]])

    def test_profile_limits_output_and_thinking_without_mutating_main_model(self):
        for protocol in ["chat_completions", "anthropic_messages"]:
            with self.subTest(protocol=protocol):
                requests = []
                def respond(request):
                    requests.append(json.loads(request.content))
                    if protocol == "chat_completions":
                        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "摘要"}, "finish_reason": "stop"}]})
                    return httpx.Response(200, json={"role": "assistant", "content": [{"type": "text", "text": "摘要"}], "stop_reason": "end_turn"})
                base = CompatibleChatModel(ModelSettings(base_url="https://model.example/v1", name="synthetic",
                    api_key=SecretStr("synthetic-key"), max_output_tokens=6000, protocol=protocol),
                    client=httpx.Client(transport=httpx.MockTransport(respond)))
                base.with_options(max_output_tokens=1200, thinking="disabled").complete([{"role": "user", "content": "总结"}], [], 6)
                base.complete([{"role": "user", "content": "分析"}], [], 20)
                self.assertEqual(requests[0]["max_tokens"], 1200)
                self.assertEqual(requests[0]["thinking"], {"type": "disabled"})
                self.assertEqual(requests[1]["max_tokens"], 6000)
                self.assertNotIn("thinking", requests[1])
                base.close()


class ReadTrackingTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.settings.harness.auto_memory = False
        self.settings.harness.memory_enabled = False
        self.settings.intent.enabled = False
        self.settings.fact_check.enabled = False
        self.host = SessionHost(self.settings, "read_tracking", service=self.service,
                                model=ScriptedModel(ModelReply({"role": "assistant", "content": "合成回答"})))
        self.addCleanup(self.host.close)
        value = self.store.save_result(Observation(status="ok", data={"text": "原始证据" * 1000}))
        self.root_id = value.result_id
        self.host.ctx.evidence_ids.add(self.root_id)
        self.host.ctx.result_tools[self.root_id] = "get_stock_news"
        self.counter = 0

    def read(self, name, **args):
        self.counter += 1; identifier = f"read_{self.counter}"
        value = self.host.pool.execute(name, args, self.host.ctx)
        self.host.messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{"id": identifier, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]},
            {"role": "tool", "tool_call_id": identifier, "content": json.dumps(value, ensure_ascii=False)}])
        return value

    def test_read_result_id_redirects_to_original_and_not_escaped_wrapper(self):
        first = self.read("read_evidence", evidence_id=self.root_id, offset=0, limit=100)
        second = self.read("read_evidence", evidence_id=first["result_id"], offset=100, limit=200)
        source = (self.root / "results" / f"{self.root_id}.json").read_text()
        self.assertEqual(second["data"]["evidence_id"], self.root_id)
        self.assertEqual(second["data"]["text"], source[100:300])
        self.assertEqual(second["data"]["redirected_from"], first["result_id"])
        self.assertEqual(self.host.ctx.evidence_read_ranges[self.root_id], [[0, 300]])

    def test_only_currently_visible_duplicate_is_suppressed(self):
        first = self.read("read_evidence", evidence_id=self.root_id, offset=0, limit=100)
        duplicate = self.read("read_evidence", evidence_id=self.root_id, offset=0, limit=100)
        self.assertEqual(duplicate["data"]["text"], "")
        self.assertTrue(duplicate["data"]["already_read"])
        self.assertEqual(duplicate["data"]["next_offset"], 100)
        self.host.messages.clear()  # Models can reload a range after it was archived.
        restored = self.read("read_evidence", evidence_id=self.root_id, offset=0, limit=100)
        self.assertEqual(restored["data"]["text"], first["data"]["text"])

    def test_alias_never_grants_access_to_an_unregistered_original(self):
        first = self.read("read_evidence", evidence_id=self.root_id, limit=100)
        self.host.ctx.evidence_ids.remove(self.root_id)
        result = self.read("read_evidence", evidence_id=first["result_id"], limit=100)
        self.assertEqual(result["error"]["code"], "EVIDENCE_DENIED")
        self.assertNotIn("text", result["data"])

    def test_archived_output_read_deduplicates_visible_range_only(self):
        path = self.host.context.offload("文件原文" * 1000)["full_output"]
        first = self.read("read_output", path=path, offset=0, limit=100)
        second = self.read("read_output", path=path, offset=0, limit=100)
        self.assertEqual(second["error"], "OUTPUT_ALREADY_READ")
        self.assertEqual(second["next_offset"], 100)
        self.host.messages.clear()
        self.assertEqual(self.read("read_output", path=path, offset=0, limit=100)["text"], first["text"])

    def test_latest_state_only_on_wire_with_complete_audit_trail(self):
        self.host._register_user("保留原始用户要求")
        self.host.model_messages(self.host.messages, self.host.ctx)
        self.host.ctx.symbol = "600519.SH"
        wire = self.host.model_messages(self.host.messages, self.host.ctx)
        self.assertEqual(sum(isinstance(body, dict) and "runtime_state" in body
                             for m in wire[1:] if isinstance(m.get("content"), str)
                             for body in [json.loads(m["content"])]), 1)
        transcript = [json.loads(line) for line in (self.host.directory / "transcript.jsonl").read_text().splitlines()]
        self.assertEqual(sum(m.get("_finagent_origin") == "runtime_context" for m in transcript), 2)
        self.assertIn("保留原始用户要求", json.dumps(wire, ensure_ascii=False))
