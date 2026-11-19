from collections import Counter
from importlib.resources import files
import json
import unittest
from unittest.mock import patch

from finagent.config import Settings
from finagent.evaluation import run_replay

from .support import ServiceCase


class EvaluationTests(ServiceCase):
    def test_frozen_manifest_and_every_case_pass(self):
        definition = json.loads(files("finagent").joinpath("resources/phase1_cases.json").read_text(encoding="utf-8"))
        self.assertEqual(len(definition["cases"]), 30)
        self.assertEqual(Counter(c["category"] for c in definition["cases"]), {"loop": 8, "data": 10, "boundary": 8, "export": 4})
        self.assertEqual(len({c["id"] for c in definition["cases"]}), 30)
        # Even accidental HTTP-client use outside the fixture must be blocked.
        with patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("Replay must not reach the network")):
            summary = run_replay(Settings(data_dir=self.root))
        self.assertEqual(summary["passed_cases"], 30)
        self.assertEqual(summary["executed_variants"], 32)
        self.assertEqual(summary["passed_variants"], 32)
        for case in summary["cases"]:
            self.assertTrue(case["passed"], case)
            self.assertTrue(case["assertions"])
            self.assertTrue(case["run_dir"])
        self.assertEqual(len(summary["case_manifest_sha256"]), 64)

    def test_checker_reads_complete_result_instead_of_bounded_preview(self):
        from finagent.data.contracts import Observation
        from finagent.evaluation import tool_observations
        saved = self.store.save_result(Observation(status="partial", data={"reports": [{"report_period": "2024-03-31", "value": 123}]}))
        preview = {**saved.model_dump(mode="json"), "data": {"preview": "truncated", "truncated": True}}
        messages = [{"role": "assistant", "content": None, "tool_calls": [
                    {"id": "finance", "function": {"name": "get_financial_statements", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "finance", "content": json.dumps(preview)}]
        full = tool_observations(messages, self.store)
        self.assertEqual(full[0][1]["data"]["reports"][0]["value"], 123)

    def test_audit_without_evidence_does_not_claim_completion(self):
        from finagent.evaluation import run_audit
        summary = run_audit(Settings(data_dir=self.root))
        self.assertFalse(summary["passed"])
        self.assertFalse(summary["current_scope_passed"])
        self.assertEqual(next(g for g in summary["gates"] if g["id"] == "S1-13")["status"], "deferred_by_user")
        self.assertEqual(len(summary["gates"]), 15)
        self.assertFalse(any(g["passed"] for g in summary["gates"]))
        self.assertIsNone(summary["model_attempt_success_rate"])
