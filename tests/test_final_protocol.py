"""Natural-language completion and opt-in legacy structured-parser regressions."""
import json
import unittest

from pydantic import ValidationError

from finagent.agent import AgentLoop, RunRequest
from finagent.finish import parse_final_answer
from finagent.models import ModelReply
from finagent.presentation import format_sources
from finagent.runtime import ExecutionContext
from finagent.session import Conversation
from finagent.config import Limits

from .support import ServiceCase, ScriptedModel, final_reply, call_reply


def multiline_reply(**fields):
    body = {"status": "completed", "answer": "支持财报检索。\n支持程序计算。", **fields}
    text = json.dumps(body, ensure_ascii=False).replace("\\n", "\n")
    return ModelReply({"role": "assistant", "content": text})


class FinalParserTests(unittest.TestCase):
    def test_valid_escapes_quotes_backslashes_and_whitespace_are_preserved(self):
        answer = '路径 C:\\new\\tab；引号 "字段"；换行\n制表\t回车\r'
        text = json.dumps({"status": "completed", "answer": answer}, ensure_ascii=False, indent=2)
        final, normalized = parse_final_answer(text)
        self.assertFalse(normalized)
        self.assertEqual(final.answer, answer)

    def test_literal_string_whitespace_preserves_answer_exactly(self):
        answer = '第1行："引用"，C:\\new\r\n\t第2行'
        literal = answer.replace("\\", "\\\\").replace('"', '\\"')
        text = '{"status":"completed","answer":"' + literal + '"}'
        final, normalized = parse_final_answer(text)
        self.assertTrue(normalized)
        self.assertEqual(final.answer, answer)

    def test_other_syntax_and_schema_errors_remain_rejected(self):
        valid = '{"status":"completed","answer":"第一行\n第二行"}'
        invalid = [valid[:-1], valid + '正文', '```json\n' + valid + '\n```',
                   valid.replace('completed', 'success'),
                   valid[:-1] + ',"unexpected":true}',
                   valid[:-1] + ',"evidence_ids":"raw_fake"}',
                   valid.replace('第二行', '第二行\x00'),
                   valid.replace('第二行', '第二行\\q'),
                   '{"status":"completed"}']
        for text in invalid:
            with self.subTest(text=repr(text)), self.assertRaises(ValidationError):
                parse_final_answer(text)


class FinalProtocolLoopTests(ServiceCase):
    def run_reply(self, reply, goal="你有哪些功能"):
        return AgentLoop(ScriptedModel(reply), self.registry, self.store).run(RunRequest(goal))

    def test_capability_answer_completes_once_and_retains_raw_message(self):
        reply = multiline_reply()
        original = reply.message["content"]
        result = self.run_reply(reply)
        self.assertEqual((result.status, result.model_calls, result.tool_calls), ("completed", 1, 0))
        self.assertEqual(self.requests, [])
        messages = json.loads((result.run_dir / "messages.json").read_text(encoding="utf-8"))
        self.assertEqual(next(m["content"] for m in messages if m["role"] == "assistant"), original)
        body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(body["terminal_response_validated"])
        self.assertFalse(body["final_protocol_verified"])
        self.assertEqual(body["final_answer"]["answer"], "支持财报检索。\n支持程序计算。")
        events = [json.loads(line) for line in (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(e["kind"] == "terminal_response" for e in events), 1)
        self.assertFalse(any(e["kind"] == "finish_rejected" for e in events))

    def test_no_data_is_a_coverage_warning_not_a_format_retry(self):
        result = self.run_reply(multiline_reply(), "查询茅台股价")
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.model_calls, 1)
        self.assertIn("DATA_UNAVAILABLE", result.reason)
        self.assertEqual(result.answer, "支持财报检索。\n支持程序计算。")

    def test_json_syntax_is_not_a_requirement_for_visible_output(self):
        bad = ModelReply({"role": "assistant", "content": '{"status":"completed","answer":"未闭合'})
        model = ScriptedModel(bad, final_reply(answer="支持财报检索"))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("你有哪些功能"))
        self.assertEqual((result.status, result.model_calls), ("completed", 1))
        self.assertEqual(result.answer, bad.message["content"])

    def test_extra_legacy_fields_and_wrong_optional_types_do_not_reject_answer(self):
        reply = multiline_reply(extra="accepted", facts="not-required", evidence_ids="not-required")
        result = self.run_reply(reply)
        self.assertEqual((result.status, result.model_calls), ("completed", 1))
        self.assertEqual(result.answer, "支持财报检索。\n支持程序计算。")

    def test_markdown_text_completes_and_survives_session_restart(self):
        text = "可以查财报。\n\n- 营业收入\n- 利润率\n\n也可以按你需要列成表格。"
        result = self.run_reply(ModelReply({"role": "assistant", "content": text}))
        self.assertEqual((result.status, result.model_calls), ("completed", 1))
        self.assertEqual(result.answer, text)
        body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(body["final_validation_mode"], "natural_language")
        self.assertEqual(body["response_format"], "text")
        self.assertFalse(body["outcome_verified"])
        Conversation(self.store).remember(result)
        history = Conversation(self.store).context("简短一些", Limits()).conversation_history
        self.assertEqual(history[-1]["assistant"], text)
        prompt = (result.run_dir / "messages.json").read_text(encoding="utf-8")
        self.assertNotIn("只输出一个JSON对象", prompt)
        self.assertNotIn("最终 JSON 协议", prompt)
        self.assertNotIn("在 evidence_ids 中登记", prompt)

    def test_data_reply_needs_no_facts_or_reference_arrays(self):
        text = "合成行情快照价格为10元。来源：腾讯；数据时间以本轮快照为准。"
        model = ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519"})),
                              ModelReply({"role": "assistant", "content": text}))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("查询茅台行情"))
        self.assertEqual((result.status, result.model_calls), ("completed", 2))
        self.assertEqual(result.answer, text)
        body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(body["cited_evidence_ids"], [])
        self.assertTrue(body["available_evidence_ids"])
        self.assertIn(body["available_evidence_ids"][0], format_sources(result, self.store))
        self.assertIn("不代表正文已逐句核验", format_sources(result, self.store))
        report = (result.run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn(text, report)

    def test_empty_replies_fail_without_silent_success_or_format_retries(self):
        for content in (None, "", " \n\t", '{"status":"completed","answer":""}'):
            with self.subTest(content=content):
                result = self.run_reply(ModelReply({"role": "assistant", "content": content}))
                self.assertEqual((result.status, result.model_calls), ("failed", 1))
                self.assertIn("EMPTY_RESPONSE", result.reason)
                body = json.loads((result.run_dir / "result.json").read_text(encoding="utf-8"))
                self.assertFalse(body["terminal_response_validated"])
                self.assertIsNone(body["final_answer"])

    def test_empty_response_after_tool_retains_evidence_without_becoming_success(self):
        model = ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519"})),
                              ModelReply({"role": "assistant", "content": None}))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("查询茅台行情"))
        self.assertEqual((result.status, result.model_calls), ("partial", 2))
        self.assertIn("EMPTY_RESPONSE", result.reason)
        self.assertTrue(result.evidence_ids)
        self.assertIn("没有返回答复", result.answer)

    def test_truncated_text_is_not_accepted_as_final_output(self):
        result = self.run_reply(ModelReply({"role": "assistant", "content": "生成到一半"}, finish_reason="length"))
        self.assertNotEqual(result.status, "completed")
        self.assertIn("MODEL_TRUNCATED", result.reason)

    def test_fenced_json_requested_by_user_is_preserved_as_content(self):
        text = '```json\n{"revenue":100,"unit":"CNY"}\n```'
        result = AgentLoop(ScriptedModel(ModelReply({"role": "assistant", "content": text})),
                           self.registry, self.store).run(RunRequest("请展示JSON示例", require_data=False))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, text)

    def test_old_correction_setting_does_not_affect_natural_reply(self):
        for limit in (0, 5):
            ctx = ExecutionContext()
            ctx.limits.finish_corrections = limit
            result = AgentLoop(ScriptedModel(ModelReply({"role": "assistant", "content": "你好。"})),
                               self.registry, self.store).run(RunRequest("你好"), ctx)
            self.assertEqual((result.status, result.model_calls), ("completed", 1))

    def test_tool_clarification_continues_to_natural_reply_in_same_run(self):
        model = ScriptedModel(call_reply(("ask", "ask_user", {"question": "希望详细还是简短？"})),
                              ModelReply({"role": "assistant", "content": "明白，我会简短说明。"}))
        result = AgentLoop(model, self.registry, self.store).run(
            RunRequest("解释概念", require_data=False), clarification_handler=lambda _: "简短一些")
        self.assertEqual((result.status, result.model_calls, result.tool_calls), ("completed", 2, 1))
        self.assertEqual(result.answer, "明白，我会简短说明。")
        Conversation(self.store).remember(result)
        history = Conversation(self.store).context("继续", Limits()).conversation_history
        self.assertIn("简短一些", history[-1]["user"])
        self.assertEqual(history[-1]["assistant"], result.answer)
