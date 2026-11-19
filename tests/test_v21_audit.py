"""Offline audit scoring rules; all annotations here are synthetic test inputs."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from finagent.research.token_envelope import TokenEnvelope, TokenEnvelopeModel
from finagent.config import ModelSettings
from finagent.errors import AgentError
from tests.support import call_reply
from scripts.audit_v21 import score


class AuditRuleTests(unittest.TestCase):
    def labels(self):
        return {"fact_annotations": [{"human_support": "supported", "critical_error": False} for _ in range(80)],
                "response_annotations": [{"human_on_target": True} for _ in range(20)],
                "decision_annotations": [{"human_consistent": True, "critical_error": False} for _ in range(12)]}

    def test_unlabelled_records_never_pass(self):
        labels = {"fact_annotations": [{"human_support": None, "critical_error": None}] * 80,
                  "response_annotations": [], "decision_annotations": []}
        result = score(labels)
        self.assertFalse(result["passed"])
        self.assertIsNone(result["support_rate"])

    def test_unable_to_judge_is_not_removed_from_denominator(self):
        labels = self.labels()
        for x in labels["fact_annotations"][:5]:
            x["human_support"] = "unable_to_judge"
        result = score(labels)
        self.assertEqual(result["support_rate"], 75 / 80)
        self.assertFalse(result["passed"])

    def test_critical_error_blocks_otherwise_high_scores(self):
        labels = self.labels()
        self.assertTrue(score(labels)["passed"])
        labels["decision_annotations"][0]["critical_error"] = True
        self.assertFalse(score(labels)["passed"])


class TokenEnvelopeTests(unittest.TestCase):
    def test_concurrent_admission_cannot_share_final_slot(self):
        envelope = TokenEnvelope(5000)
        def take(_):
            try:
                envelope.reserve([], [], 128)
                return 1
            except AgentError:
                return 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(take, range(16))), 1)

    def test_unknown_usage_retains_conservative_charge(self):
        envelope = TokenEnvelope(5000)
        estimate = envelope.reserve([], [], 128)
        envelope.settle(estimate)
        value = envelope.snapshot()
        self.assertEqual(value["charged"], estimate)
        self.assertEqual(value["unknown_requests"], 1)
        self.assertEqual(value["pending"], 0)

    def test_budget_denies_before_model_and_flags_provider_overrun(self):
        class Model:
            calls = 0
            def complete(self, *args):
                self.calls += 1
                reply = call_reply()
                reply.usage = {"prompt_tokens": 99999, "completion_tokens": 1}
                return reply
        model = Model()
        wrapped = TokenEnvelopeModel(model, TokenEnvelope(1), ModelSettings(max_output_tokens=128))
        with self.assertRaisesRegex(AgentError, "COMPARISON_TOKEN_BUDGET"):
            wrapped.complete([], [], 1)
        self.assertEqual(model.calls, 0)
        wrapped = TokenEnvelopeModel(model, TokenEnvelope(5000), ModelSettings(max_output_tokens=128))
        with self.assertRaisesRegex(AgentError, "COMPARISON_TOKEN_OVERRUN"):
            wrapped.complete([], [], 1)
        self.assertTrue(wrapped.envelope.snapshot()["provider_estimate_overrun"])
