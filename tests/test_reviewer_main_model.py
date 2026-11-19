"""Main-provider reviewer transport and isolation; synthetic responses."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from finagent.answer_review import AnswerReviewer
from finagent.config import AnswerReviewSettings, ModelSettings, load_settings
from finagent.errors import AgentError
from finagent.harness.reviewer import ReviewBudget, ReviewerRuntime
from tests.test_reviewer_agent import decision, entry, packet


def native_response(protocol, name="submit_review", arguments=None, *, reason=None, identifier="review_call"):
    arguments = decision() if arguments is None else arguments
    if protocol == "anthropic_messages":
        return httpx.Response(200, json={"role": "assistant", "model": "same-main-model",
            "content": [{"type": "tool_use", "id": identifier, "name": name, "input": arguments}],
            "stop_reason": reason or "tool_use", "usage": {"input_tokens": 80, "output_tokens": 20}})
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
        "tool_calls": [{"type": "function", "id": identifier,
            "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        "finish_reason": reason or "tool_calls"}], "usage": {"prompt_tokens": 80, "completion_tokens": 20}})


class MainProviderReviewerTests(unittest.TestCase):
    def settings(self, protocol="anthropic_messages"):
        return ModelSettings(base_url="https://main.example/anthropic" if protocol == "anthropic_messages"
            else "https://main.example/v1", name="same-main-model", api_key=SecretStr("synthetic-main-key"),
            protocol=protocol, tool_loading="deferred" if protocol == "anthropic_messages" else "eager",
            max_output_tokens=7000)

    def reviewer(self, handler, *, protocol="anthropic_messages", **config):
        return AnswerReviewer(AnswerReviewSettings(backend="main_model", agentic=True, **config),
            model_settings=self.settings(protocol), transport=httpx.MockTransport(handler))

    def test_both_protocols_use_main_endpoint_identity_and_key_with_isolated_policy(self):
        for protocol in ["anthropic_messages", "chat_completions"]:
            with self.subTest(protocol=protocol):
                requests, usage = [], []
                def respond(request):
                    requests.append(request)
                    return native_response(protocol)
                main = self.settings(protocol)
                original = main.model_copy(deep=True)
                config = AnswerReviewSettings(backend="main_model", agentic=True)
                reviewer = AnswerReviewer(config, model_settings=main, transport=httpx.MockTransport(respond))
                audit, budget = {}, ReviewBudget()
                result = reviewer.review("你好", packet(), audit=audit, budget=budget, on_usage=usage.append)
                request = requests[0]; body = json.loads(request.content)
                self.assertEqual(request.url.host, "main.example")
                self.assertNotIn("/api/chat", request.url.path)
                self.assertEqual(request.headers["authorization"], "Bearer synthetic-main-key")
                if protocol == "anthropic_messages":
                    self.assertEqual(request.headers["x-api-key"], "synthetic-main-key")
                self.assertEqual(body["model"], main.name)
                self.assertEqual(body["max_tokens"], config.output_tokens)
                self.assertEqual(body["thinking"], {"type": "disabled"})
                self.assertEqual(main, original)
                schemas = body["tools"]
                names = {t["name"] if protocol == "anthropic_messages" else t["function"]["name"] for t in schemas}
                self.assertEqual(names, {"list_review_evidence", "read_review_evidence", "search_review_evidence", "submit_review"})
                self.assertTrue(all(not t.get("defer_loading") for t in schemas))
                expected = ReviewerRuntime(config, "你好", packet()).model_messages([], None)
                actual_prompt = body["system"] if protocol == "anthropic_messages" else body["messages"][0]["content"]
                self.assertEqual(actual_prompt, expected[0]["content"])
                self.assertNotIn("主模型断言无需审核", json.dumps(body, ensure_ascii=False))
                self.assertEqual((result["verdict"], result["model"], audit["model_backend"]),
                                 ("pass", main.name, "main_model"))
                self.assertEqual(budget.model_calls, 1)
                self.assertEqual(sum(v["prompt_tokens"] for v in usage), 80)

    def test_original_json_action_envelope_still_works(self):
        def respond(request):
            return httpx.Response(200, json={"role": "assistant", "content": [{"type": "text",
                "text": json.dumps({"action": "submit_review", "arguments": decision()})}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 30, "output_tokens": 10}})
        self.assertEqual(self.reviewer(respond).review("你好", packet())["verdict"], "pass")

    def test_text_submission_arguments_are_validated_without_filling_missing_fields(self):
        for valid in [True, False]:
            value = decision()
            if not valid:
                value.pop("checks")
            def respond(request):
                return httpx.Response(200, json={"role": "assistant", "content": [{"type": "text", "text": json.dumps(value)}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 30, "output_tokens": 10}})
            if valid:
                self.assertEqual(self.reviewer(respond).review("你好", packet())["verdict"], "pass")
            else:
                with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_INVALID"):
                    self.reviewer(respond).review("你好", packet())

    def test_native_retrieval_batch_runs_sequentially_and_charges_each_retrieval(self):
        calls = []
        def respond(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, json={"role": "assistant", "content": [
                    {"type": "tool_use", "id": "find_price", "name": "search_review_evidence",
                     "input": {"evidence_id": "result_a", "path": "/body/price", "query": "120"}},
                    {"type": "tool_use", "id": "find_unit", "name": "search_review_evidence",
                     "input": {"evidence_id": "result_a", "path": "/body/unit", "query": "元"}}],
                    "stop_reason": "tool_use", "usage": {"input_tokens": 30, "output_tokens": 10}})
            return native_response("anthropic_messages", arguments=decision(evidence_required=True,
                citations=[{"evidence_id": "result_a", "path": "/body/price"}]))
        audit = {}; value = self.reviewer(respond).review("资料价格120元。", packet(entry()), audit=audit)
        self.assertEqual(value["verdict"], "pass")
        self.assertEqual(audit["budget_after"]["model_calls"], 2)
        self.assertEqual(audit["budget_after"]["retrieval_calls"], 2)

    def test_native_submission_cannot_be_mixed_with_a_read_batch(self):
        response = {"role": "assistant", "content": [
            {"type": "tool_use", "id": "submit", "name": "submit_review", "input": decision()},
            {"type": "tool_use", "id": "read", "name": "read_review_evidence", "input": {"evidence_id": "result_a"}}],
            "stop_reason": "tool_use", "usage": {"input_tokens": 30, "output_tokens": 10}}
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_INVALID"):
            self.reviewer(lambda _: httpx.Response(200, json=response)).review("你好", packet())

    def test_read_then_submit_uses_original_evidence_and_same_citation_validation(self):
        calls = []
        def respond(request):
            calls.append(json.loads(request.content))
            if len(calls) == 1:
                return native_response("anthropic_messages", "read_review_evidence",
                    {"evidence_id": "result_a", "path": "/body/price"}, identifier="read")
            return native_response("anthropic_messages", arguments=decision(evidence_required=True,
                citations=[{"evidence_id": "result_a", "path": "/body/price"}]), identifier="submit")
        audit = {}; value = self.reviewer(respond).review("资料中价格是120元。", packet(entry()), audit=audit)
        self.assertEqual(value["verdict"], "pass")
        self.assertIn("120", value["citations"][0]["quote"])
        self.assertEqual(audit["budget_after"]["model_calls"], 2)
        self.assertEqual(audit["budget_after"]["retrieval_calls"], 1)

    def test_failure_and_timeout_do_not_fall_back_to_ollama_or_pass_draft(self):
        for expected, handler in [
            ("ANSWER_REVIEW_UNAVAILABLE", lambda _: httpx.Response(401, text="private-response-token")),
            ("ANSWER_REVIEW_TIMEOUT", lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("timeout", request=request)))] :
            with self.subTest(expected=expected):
                calls = []
                def respond(request):
                    calls.append(request.url.host)
                    return handler(request)
                with self.assertRaises(AgentError) as caught:
                    self.reviewer(respond).review("你好", packet())
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(calls, ["main.example"])
                self.assertNotIn("private-response-token", str(caught.exception))

    def test_incomplete_output_and_unauthorized_tools_are_rejected_and_accounted(self):
        for options in [{"reason": "max_tokens"}, {"name": "write_file", "arguments": {"path": "/tmp/no-write"}}]:
            with self.subTest(options=options):
                budget, audit = ReviewBudget(), {}
                with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_INVALID"):
                    self.reviewer(lambda _: native_response("anthropic_messages", **options)).review(
                        "你好", packet(), budget=budget, audit=audit)
                self.assertEqual(budget.model_calls, 1)
                self.assertEqual(audit["status"], "not_verified")

    def test_main_backend_requires_explicit_independent_agent_and_model_configuration(self):
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_CONFIG"):
            AnswerReviewer(AnswerReviewSettings(backend="main_model", agentic=True))
        with self.assertRaisesRegex(AgentError, "ANSWER_REVIEW_CONFIG"):
            AnswerReviewer(AnswerReviewSettings(backend="main_model"), model_settings=self.settings())

    def test_environment_resolves_same_model_and_key_without_a_second_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "finagent.toml"
            path.write_text('[fact_check.review]\nbackend="main_model"\nagentic=true\nmodel="ignored-old-model"\n')
            with patch.dict("os.environ", {"FINAGENT_MODEL": "same-main-model", "FINAGENT_API_KEY": "synthetic-main-key",
                "FINAGENT_BASE_URL": "https://main.example/anthropic", "FINAGENT_ANSWER_REVIEW_MODEL": "also-ignored"}, clear=True):
                settings = load_settings(path)
            self.assertEqual(settings.fact_check.review.model, settings.model.name)
            reviewer = AnswerReviewer(settings.fact_check.review, model_settings=settings.model)
            self.assertEqual(reviewer.model_settings.api_key.get_secret_value(), "synthetic-main-key")
            self.assertNotIn("synthetic-main-key", settings.model_dump_json())
