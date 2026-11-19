"""Comparative scoring and seed isolation; fixtures are not model scores."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from finagent import memory_benchmark
from finagent.config import MemorySettings, Settings
from pydantic import SecretStr
from scripts import evaluate_resume_comparisons as experiment


class ResumeComparisonTests(unittest.TestCase):
    def test_recording_accepts_default_none_transport_and_omits_auth_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"fixture": True}))
            with patch("httpx.HTTPTransport", return_value=transport):
                with experiment.record_http(Path(directory), 2) as calls:
                    with httpx.Client(transport=None, trust_env=False) as client:
                        response = client.post("http://127.0.0.1:11435/api/chat", json={"synthetic": True},
                                               headers={"Authorization": "Bearer synthetic-secret"})
                        self.assertTrue(response.json()["fixture"])
                    self.assertEqual(calls[0], 1)
            request = experiment.read(Path(directory) / "call_00001.request.json")
            self.assertNotIn("synthetic-secret", json.dumps(request))

    def test_keyword_source_is_not_automatically_scored_wrong(self):
        labels = [{"route_required": ["market"], "route_optional": []}]
        prediction = {"route": {"source": "fallback", "tool_groups": ["market"], "fallback_reason": "RULES_ONLY"}}
        result = experiment.group_metrics([prediction], labels)
        self.assertEqual(result["set_match"], 1)
        self.assertEqual(result["strict_nonfallback_matched"], 0)
        self.assertEqual(result["macro_f1_supported"], 1)

    def test_failures_and_overselecting_do_not_get_credit(self):
        labels = [{"route_required": ["market"], "route_optional": ["news"]}] * 3
        predictions = [
            {"route": {"tool_groups": ["market", "news"]}},
            {"route": {"tool_groups": ["market", "policy"]}},
            {"route": {"tool_groups": ["market"]}, "error": "failed"},
        ]
        result = experiment.group_metrics(predictions, labels)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["set_match"], 1 / 3)
        self.assertEqual(result["missing_or_errors"], 1)
        self.assertEqual(result["per_class"]["market"]["fn"], 1)
        with self.assertRaises(ValueError):
            experiment.group_metrics(predictions[:1], labels)

    def test_real_seed_owner_filter_and_forget_replay(self):
        cases, memories, _, _ = memory_benchmark.load_dataset("evaluation/memory_v1")
        config = MemorySettings(embedding_backend="disabled", rerank="off")
        with tempfile.TemporaryDirectory() as directory:
            for category in ("user_isolation", "project_isolation", "deleted_memory", "core_residency"):
                case = next(c for c in cases if c["category"] == category)
                store = experiment.seed_store(Path(directory) / category, case, memories, config)
                visible = store.records()
                self.assertTrue(all(memory_benchmark.in_scope(r, case["scope"]) for r in visible))
                self.assertTrue(all(r["source_role"] == memories[r["id"]]["source_role"] for r in visible))
                deleted = {e["memory_id"] for e in case["setup_events"]}
                self.assertFalse(deleted & {r["id"] for r in visible})
                for key in case["corpus_ids"]:
                    path = store.root / "users" / memories[key]["user_id"] / (key + ".json")
                    self.assertEqual(path.exists(), key not in deleted)
                if category == "core_residency":
                    self.assertTrue(store.core_records())

    def test_prepare_freezes_inputs_implementation_and_script_and_detects_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "bundle"
            settings = Settings(memory=MemorySettings(embedding_api_key=SecretStr("synthetic-embedding-secret"),
                                                     rerank_api_key=SecretStr("synthetic-rerank-secret")))
            experiment.prepare(output, settings)
            protocol = experiment.verify(output, settings)
            self.assertEqual(protocol["routing"]["primary_split"], "acceptance")
            self.assertEqual(protocol["memory"]["primary_split"], "holdout")
            self.assertNotIn("synthetic-embedding-secret", json.dumps(protocol))
            self.assertNotIn("synthetic-rerank-secret", json.dumps(protocol))
            self.assertNotIn("embedding_api_key", protocol["memory_config"])
            with self.assertRaises(ValueError):
                experiment.prepare(output, settings)
            target = output / "frozen/memory_v1/inputs.jsonl"
            target.write_text(target.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "摘要"):
                experiment.verify(output, settings)


if __name__ == "__main__":
    unittest.main()
