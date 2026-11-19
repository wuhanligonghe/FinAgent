"""Synthetic parser/resolver fixtures: application contracts, not model quality."""
import copy
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from finagent.config import Settings, NLUSettings
from finagent.data.evidence import EvidenceStore
from finagent.errors import AgentError
from finagent.runtime import TZ
from finagent.session import Conversation
from finagent.nlu.contracts import NLUResult, ROLES
from finagent.nlu.parser import prepare
from finagent.nlu.qwen3 import Qwen3Parser


def prediction(text, *, task="stock_analysis", act="new_request", binding="explicit_single", spans=(), **dims):
    entities = []
    for kind, value, role in spans:
        start = text.index(value)
        entities.append(dict(type=kind, text=value, start=start, end=start + len(value), score=.99, **({"role": role} if role else {})))
    return NLUResult(model_id="synthetic-parser", bundle_sha256="a" * 64, task_type=task, dialogue_act=act,
                     entity_binding=binding,
                     dimension_state={r: dims.get(r, "unspecified") for r in ROLES}, entities=entities, admission="accept")


class Parser:
    def __init__(self, *results):
        self.results, self.calls = iter(results), []

    def parse(self, text, context):
        self.calls.append((text, context))
        return next(self.results)


class NLUHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temp.name), nlu=NLUSettings(model_dir=Path(self.temp.name) / "missing-model"))
        self.conversation = Conversation(EvidenceStore(self.settings.data_dir), "nlu")
        self.service = Mock()
        def resolve(query, ctx):
            ctx.source_request()
            symbol = {"中电鑫龙": "002298.SZ", "兴业银行": "601166.SH", "002298": "002298.SZ"}.get(query)
            return Mock(data={"selected": {"symbol": symbol}} if symbol else {"candidates": [{"symbol": "002298.SZ"}]})
        self.service.resolve_entity.side_effect = resolve

    def tearDown(self):
        self.temp.cleanup()

    def parse(self, text, result, **kwargs):
        return prepare(text, self.settings, self.conversation, parser=Parser(result), service=self.service, **kwargs)

    def test_default_and_explicit_legacy(self):
        self.assertEqual(self.settings.nlu.backend, "qwen3_ollama")
        with self.assertRaises(ValueError):
            NLUSettings(backend="legacy_regex")
        self.assertEqual(NLUSettings(backend="legacy_regex", allow_legacy_regex=True).backend, "legacy_regex")

    def test_news_exact_entity_one_parse(self):
        text = "分析一下中电鑫龙最近的新闻面"
        parser = Parser(prediction(text, spans=[("COMPANY", "中电鑫龙", "target"), ("LOOKBACK_WINDOW", "最近", None)], news="include"))
        with patch("finagent.session.Conversation.context", side_effect=AssertionError("legacy forbidden")), patch("finagent.research.routing.research_route", side_effect=AssertionError("legacy forbidden")):
            p = prepare(text, self.settings, self.conversation, parser=parser, service=self.service)
        self.assertEqual(len(parser.calls), 1)
        self.assertEqual(self.service.resolve_entity.call_args.args[0], "中电鑫龙")
        self.assertEqual((p.mode, p.roles, p.pipeline, p.ctx.symbol), ("specialist", ["news"], "research", "002298.SZ"))
        self.assertEqual(p.research_request(self.settings).resolution_source_requests, 1)

    def test_correction_excluded_company(self):
        text = "不是兴业银行，换成中电鑫龙看看新闻"
        p = self.parse(text, prediction(text, act="switch_entity", spans=[("COMPANY", "兴业银行", "excluded"), ("COMPANY", "中电鑫龙", "target")], news="include"))
        self.assertEqual(p.ctx.symbol, "002298.SZ")
        self.service.resolve_entity.assert_called_once()

    def test_no_full_text_entity_crop(self):
        text = "分析一下中电鑫龙最近的新闻面"
        with self.assertRaises(AgentError) as caught:
            self.parse(text, prediction(text, binding="absent", news="include"))
        self.assertEqual(caught.exception.code, "NEEDS_CLARIFICATION")
        self.service.resolve_entity.assert_not_called()

    def test_unselected_deep_words_require_dimensions(self):
        text = "深入研究中电鑫龙，不看政策，不要辩论"
        with self.assertRaisesRegex(AgentError, "NEEDS_CLARIFICATION"):
            self.parse(text, prediction(text, spans=[("COMPANY", "中电鑫龙", "target")], policy="exclude"))
        self.service.resolve_entity.assert_not_called()

    def test_conflicting_explicit_flag_before_lookup(self):
        text = "只分析中电鑫龙新闻，不要辩论"
        with self.assertRaises(AgentError):
            self.parse(text, prediction(text, spans=[("COMPANY", "中电鑫龙", "target")], news="include"), research_options={"pipeline": "trading"})
        self.service.resolve_entity.assert_not_called()

    def test_full_decision_words_never_grant_execution(self):
        text = "完整研究中电鑫龙并进行风险讨论"
        with self.assertRaisesRegex(AgentError, "NEEDS_CLARIFICATION"):
            self.parse(text, prediction(text, spans=[("COMPANY", "中电鑫龙", "target")]))

    def test_concept_never_resolves_or_researches(self):
        text = "什么是多空辩论"
        p = self.parse(text, prediction(text, task="concept_qa", binding="absent"))
        self.assertEqual(p.mode, "direct")
        self.service.resolve_entity.assert_not_called()
        with self.assertRaises(AgentError):
            p.research_request(self.settings)

    def test_concept_hint_does_not_block_financial_lookup(self):
        from finagent.tools.registry import ToolRegistry, Tool
        from finagent.tools.perception import QuoteArgs
        from finagent.data.contracts import Observation
        p = self.parse("什么是多空辩论", prediction("什么是多空辩论", task="concept_qa", binding="absent"))
        handler = Mock(return_value=Observation(status="ok", source="synthetic", data={"price": 10}))
        registry = ToolRegistry()
        registry.register(Tool("get_quote", "fixture", QuoteArgs, handler))
        value = registry.execute("get_quote", {"symbol": "002298.SZ"}, p.ctx)
        self.assertEqual(value.status, "ok")
        handler.assert_called_once()

    def test_candidate_selection_preserves_original_task(self):
        text = "分析未知公司的新闻"
        parser = Parser(prediction(text, spans=[("COMPANY", "未知公司", "target")], news="include"))
        with self.assertRaises(AgentError) as error:
            prepare(text, self.settings, self.conversation, parser=parser, service=self.service)
        pending = error.exception.pending
        p = prepare("1", self.settings, self.conversation, parser=parser, service=self.service, pending=pending)
        self.assertEqual(p.text, text)
        self.assertEqual((p.ctx.symbol, p.roles, p.ctx.source_requests), ("002298.SZ", ["news"], 1))
        self.assertEqual(len(parser.calls), 1)

    def test_entity_answer_merges_and_counts(self):
        text, reply = "分析新闻", "中电鑫龙"
        parser = Parser(prediction(text, binding="absent", news="include"), prediction(reply, task="other", act="clarification_answer", binding="explicit_single", spans=[("COMPANY", reply, "target")]))
        with self.assertRaises(AgentError) as error:
            prepare(text, self.settings, self.conversation, parser=parser, service=self.service)
        p = prepare(reply, self.settings, self.conversation, parser=parser, service=self.service, pending=error.exception.pending)
        self.assertEqual(p.text, text + "\n" + reply)
        self.assertEqual(p.roles, ["news"])
        p.result.check_text(p.text)

    def test_reference_vs_absent_and_negative(self):
        self.conversation.state["symbol"] = "002298.SZ"
        text = "继续分析它的新闻"
        p = self.parse(text, prediction(text, act="follow_up", binding="reference", news="include"))
        self.assertEqual(p.ctx.symbol, "002298.SZ")
        with self.assertRaises(AgentError):
            self.parse("分析新闻", prediction("分析新闻", binding="absent", news="include"))

    def test_tail_overflow_no_lookup(self):
        text = "中电鑫龙" * 100 + "不要辩论"
        p = prediction(text, news="include").model_copy(update={"input_status": "too_long", "admission": "clarify", "entities": []})
        with self.assertRaises(AgentError):
            self.parse(text, p)
        self.service.resolve_entity.assert_not_called()

    def test_cutoff_window_horizon_separate(self):
        text = "截至2025-01-01分析中电鑫龙最近两周新闻，未来三个月"
        p = self.parse(text, prediction(text, spans=[("TIME_CUTOFF", "2025-01-01", None), ("COMPANY", "中电鑫龙", "target"), ("LOOKBACK_WINDOW", "最近两周", None), ("DECISION_HORIZON", "未来三个月", None)], news="include"))
        self.assertEqual(p.ctx.as_of.date().isoformat(), "2025-01-01")
        self.assertEqual((p.news_days, p.horizon), (14, "未来三个月"))

    def test_relative_report_period_inherits_once(self):
        self.conversation.state.update(symbol="002298.SZ", scope={"report_period": "2024-06-30"})
        text = "那上一年呢"
        p = self.parse(text, prediction(text, task="data_query", act="follow_up", binding="reference", spans=[("REPORT_PERIOD", "上一年", None)]))
        self.assertEqual(p.ctx.scope["report_period"], "2023-06-30")

    def test_failed_switch_invalidates_old_identity(self):
        self.conversation.state.update(symbol="601166.SH", last_research_id="b" * 32, scope={"research_id": "b" * 32})
        text = "换成未知公司看新闻"
        with self.assertRaises(AgentError):
            self.parse(text, prediction(text, act="switch_entity", spans=[("COMPANY", "未知公司", "target")], news="include"))
        self.assertIsNone(self.conversation.state["symbol"])
        self.assertNotIn("last_research_id", self.conversation.state)

    def test_candidate_expiry_and_invalid_index(self):
        text = "分析未知公司新闻"
        with self.assertRaises(AgentError) as error:
            self.parse(text, prediction(text, spans=[("COMPANY", "未知公司", "target")], news="include"))
        pending = error.exception.pending
        with self.assertRaises(AgentError):
            prepare("2", self.settings, self.conversation, pending=pending)
        pending.created -= 901
        with self.assertRaises(AgentError) as expired:
            prepare("1", self.settings, self.conversation, pending=pending)
        self.assertEqual(expired.exception.code, "NLU_CLARIFICATION_EXPIRED")

    def test_multiple_targets_and_same_security_aliases(self):
        text = "分析中电鑫龙和兴业银行新闻"
        with self.assertRaises(AgentError):
            self.parse(text, prediction(text, binding="explicit_multiple", spans=[("COMPANY", "中电鑫龙", "target"), ("COMPANY", "兴业银行", "target")], news="include"))
        text = "分析中电鑫龙002298新闻"
        p = self.parse(text, prediction(text, spans=[("COMPANY", "中电鑫龙", "target"), ("STOCK_CODE", "002298", "target")], news="include"))
        self.assertEqual(p.ctx.symbol, "002298.SZ")

    def test_financial_period_metric_comparison(self):
        text = "看去年年报的归母净利润同比"
        self.conversation.state["symbol"] = "002298.SZ"
        p = self.parse(text, prediction(text, task="data_query", binding="reference", act="follow_up", spans=[("REPORT_PERIOD", "去年年报", None), ("FINANCIAL_METRIC", "归母净利润", None), ("COMPARISON_MODE", "同比", None)]), as_of=datetime(2025, 5, 8, tzinfo=TZ))
        self.assertEqual(p.ctx.scope_constraints["allowed_report_periods"], ["2023-12-31", "2024-12-31"])
        self.assertEqual(p.ctx.scope_constraints["profit_metrics"], ["attributable_net_profit"])

    def test_removed_run_command_never_initializes_models(self):
        import io
        from contextlib import redirect_stdout
        from finagent.cli import main
        with patch("finagent.harness.host.CompatibleChatModel", side_effect=AssertionError("no remote model")), redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(main(["run", "分析中电鑫龙新闻"]), 2)
        self.assertIn("入口已移除", stream.getvalue())

    def test_session_help_and_exit_without_nlu_weights_or_model(self):
        import io
        from contextlib import redirect_stdout
        from finagent.cli import main
        from finagent.harness.terminal import run_terminal
        entries = iter(["/help", "/exit"])
        with patch("finagent.cli.load_settings", return_value=self.settings), \
             patch("finagent.cli.run_terminal", side_effect=lambda settings, host: run_terminal(settings, host=host, read=lambda _: next(entries))), \
             patch("finagent.harness.host.CompatibleChatModel", side_effect=AssertionError("no remote model")), redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(main([]), 0)
        self.assertIn("已退出", stream.getvalue())
        self.assertIn("/research", stream.getvalue())

    def test_natural_history_commands_reuse_saved_research(self):
        from finagent.nlu.commands import artifact_request
        self.conversation.state["last_research_id"] = "b" * 32
        for task in ("research_status", "research_resume"):
            text = "上次研究"
            p = self.parse(text, prediction(text, task=task, binding="reference"))
            state = {"id": "b" * 32, "report": None}
            with patch("finagent.research.store.ResearchStore.snapshot", return_value=state) as snapshot, patch("finagent.research.selection.resume_selected", return_value=state) as resume, patch.object(self.conversation, "remember_research"):
                if task == "research_resume":
                    with self.assertRaisesRegex(AgentError, "NEEDS_CLARIFICATION"):
                        artifact_request(p, self.settings, self.conversation)
                else:
                    self.assertEqual(artifact_request(p, self.settings, self.conversation), state)
            if task == "research_resume":
                resume.assert_not_called()
            else:
                snapshot.assert_called_once_with("b" * 32)

    def test_inner_clarification_uses_nlu_and_preserves_period(self):
        text, reply = "中电鑫龙的净利润", "2024年报"
        parser = Parser(prediction(text, task="data_query", spans=[("COMPANY", "中电鑫龙", "target")]),
                        prediction(reply, task="data_query", act="clarification_answer", binding="absent", spans=[("REPORT_PERIOD", reply, None)]))
        p = prepare(text, self.settings, self.conversation, parser=parser, service=self.service)
        with patch("finagent.session.Conversation.context", side_effect=AssertionError("legacy forbidden")):
            updated = p.ctx.nlu_clarifier(reply)
        self.assertEqual(updated.ctx.scope["report_period"], "2024-12-31")
        self.assertEqual(updated.ctx.symbol, "002298.SZ")
        self.assertEqual(len(parser.calls), 2)

    def test_wrong_offsets_rejected(self):
        text = "分析中电鑫龙新闻"
        result = prediction(text, spans=[("COMPANY", "中电鑫龙", "target")], news="include")
        result.entities[0].start = 0
        with self.assertRaises(ValueError):
            self.parse(text, result)

    def test_missing_model_does_not_fallback(self):
        self.settings.nlu.model_dir = Path(self.temp.name) / "missing"
        with self.assertRaises(AgentError) as error:
            Qwen3Parser(self.settings)
        self.assertEqual(error.exception.code, "NLU_MODEL_MISSING")

    def test_logs_do_not_store_raw_text(self):
        text = "分析中电鑫龙新闻"
        p = self.parse(text, prediction(text, spans=[("COMPANY", "中电鑫龙", "target")], news="include"))
        log = (self.settings.data_dir / "nlu" / (p.request_id + ".json")).read_text(encoding="utf-8")
        self.assertNotIn(text, log)
        self.assertIn("bundle_sha256", log)

    def test_ordinary_query_cannot_delegate(self):
        from finagent.tools.perception import build_registry
        from finagent.data.service import DataService
        service = DataService(self.settings)
        try:
            text = "中电鑫龙股价"
            p = self.parse(text, prediction(text, task="data_query", spans=[("COMPANY", "中电鑫龙", "target")]))
            registry = build_registry(service)
            import json
            with patch("finagent.research.coordinator.ResearchCoordinator.run", side_effect=AssertionError("should not start")):
                value = registry.execute("start_stock_research", json.dumps(dict(goal=text, parsed_request_id=p.request_id)), p.ctx)
            self.assertEqual(value.status, "error")
            self.assertEqual(value.error.code, "RESEARCH_DENIED")
        finally:
            service.close()

    def test_delegation_reuses_host_scope_without_reparse(self):
        from finagent.tools.perception import build_registry
        from finagent.data.service import DataService
        import json
        service = DataService(self.settings)
        try:
            text = "只分析中电鑫龙新闻"
            p = self.parse(text, prediction(text, spans=[("COMPANY", "中电鑫龙", "target")], news="include"))
            registry = build_registry(service)
            payload = dict(goal=text, parsed_request_id=p.request_id, analysts=["news"], pipeline="research")
            result = dict(id="b" * 32, content="complete", report=None, budget={})
            with patch("finagent.research.entry.request_from_goal", side_effect=AssertionError("no second parse")), patch("finagent.research.coordinator.ResearchCoordinator.run", return_value=result) as run:
                value = registry.execute("start_stock_research", json.dumps(payload), p.ctx)
            self.assertEqual(value.status, "ok")
            request = run.call_args.args[0]
            self.assertEqual((request.goal, request.symbol, request.analysts), (text, "002298.SZ", ["news"]))
            self.assertEqual(run.call_args.kwargs["prior_usage"]["source"], 1)
            payload["goal"] = "改成深度研究其他公司"
            with patch("finagent.research.coordinator.ResearchCoordinator.run", side_effect=AssertionError("no tampered run")):
                rejected = registry.execute("start_stock_research", json.dumps(payload), p.ctx)
            self.assertEqual(rejected.status, "error")
        finally:
            service.close()

    def test_host_acceptance_smoke_can_never_pass_quality(self):
        import json
        from finagent.nlu.acceptance import evaluate
        from finagent.nlu.semantic import messages
        text = "concept fixture"
        result = prediction(text, task="concept_qa", binding="absent")
        frame = {k: getattr(result, k) for k in ("schema_version", "task_type", "dialogue_act", "entity_binding", "dimension_state")}
        frame["entities"] = []
        row = {"id": "test", "messages": messages(text, {}) + [{"role": "assistant", "content": json.dumps(frame)}],
               "metadata": {"annotation_status": "candidate", "synthetic": True}}
        root = self.settings.data_dir
        test = root / "test.jsonl"; test.write_text(json.dumps(row) + "\n", encoding="utf-8")
        expected = dict(outcome="accept", symbol=None, mode="direct", pipeline=None, analysts=[], identity_queries=[], news_days=7, policy_days=90, as_of_date="2025-05-08", scope_constraints={})
        oracle = root / "oracle.jsonl"
        oracle.write_text(json.dumps(dict(id="test", annotation_status="human_verified", clock="2025-05-08T23:59:59+08:00", identity_replay={}, expected=expected)) + "\n", encoding="utf-8")
        predictor = Mock(fingerprint="a" * 64)
        predictor.parse.return_value = result
        report = evaluate(self.settings, root, test, oracle, root / "qualification.json", parser=predictor)
        self.assertFalse(report["host_gates_passed"])
        self.assertEqual(report["independent_human_samples"], 0)
        self.assertTrue(report["cases"][0]["matched"])
        self.assertEqual(report["accepted_accuracy"], 1)



if __name__ == "__main__":
    unittest.main()
