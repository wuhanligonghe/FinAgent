"""Dedicated reranking contracts, scope boundaries, and failure fallback."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from finagent.config import MemorySettings, load_settings
from finagent.errors import AgentError
from finagent.harness.memory import MemoryStore
from finagent.harness.memory_search import HTTPEmbedder, HTTPReranker
from finagent.reranker import create_app
from finagent.security import SecurityGuard


class DedicatedRerankerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = MemorySettings(embedding_backend="disabled", rerank="always", user_id="alice", project_id="a")
        self.records = [{"id": "mem_a", "description": "现金流", "body": "经营现金流分析"},
                        {"id": "mem_b", "description": "资金", "body": "企业收付款方法"}]

    def http(self, body, status=200, handler=None):
        self.seen = []
        def dispatch(request):
            self.seen.append(request)
            return handler(request) if handler else httpx.Response(status, json=body)
        client = httpx.Client(transport=httpx.MockTransport(dispatch))
        self.addCleanup(client.close)
        return patch("finagent.harness.memory_search.httpx.Client", return_value=client)

    def test_http_scores_rank_candidates_and_filter_below_cutoff(self):
        with self.http({"model": self.settings.rerank_model, "results": [
                {"index": 0, "relevance_score": .2}, {"index": 1, "relevance_score": .95}]}) as factory:
            ranked = HTTPReranker(self.settings).rank("现金流", self.records, 10)
        self.assertEqual(ranked, ["mem_b"])
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])
        request = self.seen[0]
        self.assertEqual(request.url.path, "/v1/rerank")
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], "Qwen/Qwen3-Reranker-8B")
        self.assertEqual(payload["documents"], ["现金流\n经营现金流分析", "资金\n企业收付款方法"])

    def test_invalid_scores_and_indices_fail_without_exposing_provider_data(self):
        cases = [[{"index": 0, "relevance_score": .9}],
            [{"index": 0, "relevance_score": .9}, {"index": 0, "relevance_score": .8}],
            [{"index": -1, "relevance_score": .9}, {"index": 1, "relevance_score": .8}],
            [{"index": True, "relevance_score": .9}, {"index": 0, "relevance_score": .8}],
            [{"index": 0, "relevance_score": "secret"}, {"index": 1, "relevance_score": .8}],
            [{"index": 0, "relevance_score": 1.1}, {"index": 1, "relevance_score": .8}]]
        for rows in cases:
            with self.subTest(rows=rows), self.http({"results": rows}):
                with self.assertRaises(AgentError) as error:
                    HTTPReranker(self.settings).rank("private query", self.records, 10)
                self.assertNotIn("secret", str(error.exception))
                self.assertNotIn("private query", str(error.exception))
        with self.http({"model": "wrong", "results": []}):
            with self.assertRaises(AgentError):
                HTTPReranker(self.settings).rank("query", self.records, 10)

    def test_http_reranking_works_without_chat_and_respects_scope(self):
        store = MemoryStore(self.root, settings=self.settings)
        wanted = store.write("现金流", "经营现金流分析", "project", "a1")
        other = MemoryStore(self.root, settings=self.settings.model_copy(update={"project_id": "b"}))
        other.write("现金流", "其他项目的私密现金流", "project", "b1")
        bob = MemoryStore(self.root, settings=self.settings.model_copy(update={"user_id": "bob"}))
        bob.write("现金流", "其他用户的私密现金流", "project", "b2")
        chat = Mock()
        with self.http({"results": [{"index": 0, "relevance_score": .95}]}):
            result = store.select("现金流", chat)
        self.assertEqual([r["id"] for r in result], [wanted["id"]])
        self.assertEqual(store.last_recall["rerank_status"], "ok")
        self.assertEqual(store.last_recall["rerank_model"], "Qwen/Qwen3-Reranker-8B")
        self.assertNotIn("私密", self.seen[0].content.decode())
        chat.complete.assert_not_called()

    def test_outage_keeps_keyword_candidates_without_chat_fallback(self):
        store = MemoryStore(self.root, settings=self.settings)
        wanted = store.write("现金流", "经营现金流分析", "project", "a1")
        chat = Mock()
        with self.http({"error": "private-provider-error"}, status=503):
            result = store.select("现金流", chat)
        self.assertEqual([r["id"] for r in result], [wanted["id"]])
        self.assertEqual(store.last_recall["rerank_status"], "fallback")
        chat.complete.assert_not_called()

    def test_forgetting_during_scoring_cannot_reintroduce_record(self):
        scorer = Mock()
        store = MemoryStore(self.root, settings=self.settings, reranker=scorer)
        record = store.write("现金流", "经营现金流分析", "project", "a1")
        def rank(*args):
            store.forget(record["id"])
            return [record["id"]]
        scorer.rank.side_effect = rank
        self.assertEqual(store.select("现金流"), [])

    def test_core_preferences_are_not_sent_for_pair_scoring(self):
        scorer = Mock()
        store = MemoryStore(self.root, settings=self.settings, reranker=scorer)
        core = store.write("风格", "先给结论", source="a1")
        self.assertEqual(store.select("现金流"), [core])
        scorer.rank.assert_not_called()

    def test_8b_embedding_uses_a_different_cache_identity(self):
        current = HTTPEmbedder(MemorySettings())
        old = HTTPEmbedder(MemorySettings(embedding_model="qwen3-embedding:0.6b"))
        self.assertEqual(current.settings.embedding_model, "qwen3-embedding:8b")
        self.assertEqual(current.format, "qwen3")
        self.assertNotEqual(current.fingerprint, old.fingerprint)

    def test_reranker_configuration_and_credentials(self):
        config = self.root / "finagent.toml"
        config.write_text("[memory]\n")
        (self.root / ".env").write_text("FINAGENT_RERANK_ENDPOINT=https://rank.example/v1\n"
            "FINAGENT_RERANK_MODEL=Qwen/Qwen3-Reranker-8B\nFINAGENT_RERANK_API_KEY=private-rerank-key\n"
            "FINAGENT_RERANK_MIN_SCORE=0.4\nFINAGENT_MEMORY_RERANK=always\n")
        settings = load_settings(config)
        self.assertEqual(settings.memory.rerank_endpoint, "https://rank.example/v1")
        self.assertEqual(settings.memory.rerank_min_score, .4)
        self.assertEqual(settings.memory.rerank, "always")
        self.assertNotIn("private-rerank-key", settings.model_dump_json())
        self.assertNotIn("private-rerank-key", SecurityGuard(settings).redact("private-rerank-key")[0])
        for endpoint in ("http://remote.example/v1", "https://user:secret@example.com", "https://example.com?key=secret"):
            with self.assertRaises(ValidationError):
                MemorySettings(rerank_endpoint=endpoint)


class Scorer:
    device, quantization, max_length = "synthetic", "none", 8192
    def score(self, query, documents, *, deadline):
        return [.2, .9][:len(documents)], 20


class RerankerServiceTests(unittest.TestCase):
    def test_service_contract_authentication_and_bounded_inputs(self):
        with TestClient(create_app(Scorer, api_key="service-key")) as client:
            body = {"model": "Qwen/Qwen3-Reranker-8B", "query": "现金流", "documents": ["无关", "现金流方法"]}
            self.assertEqual(client.get("/health").json()["model"], body["model"])
            self.assertEqual(client.post("/v1/rerank", json=body).status_code, 401)
            headers = {"Authorization": "Bearer service-key"}
            response = client.post("/v1/rerank", json=body, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["results"][0], {"index": 1, "relevance_score": .9})
            self.assertEqual(response.json()["usage"]["prompt_tokens"], 20)
            self.assertEqual(client.post("/v1/rerank", json={**body, "model": "wrong"}, headers=headers).status_code, 422)
            oversized = {**body, "documents": ["x" * 12000] * 3}
            self.assertEqual(client.post("/v1/rerank", json=oversized, headers=headers).status_code, 413)

    def test_inference_failure_is_sanitized_and_releases_the_slot(self):
        scorer = Mock(device="synthetic", quantization="none", max_length=8192)
        scorer.score.side_effect = [RuntimeError("private prompt and service key"), ([.8], 10)]
        with TestClient(create_app(lambda: scorer)) as client:
            body = {"model": "Qwen/Qwen3-Reranker-8B", "query": "query", "documents": ["document"]}
            response = client.post("/v1/rerank", json=body)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private", response.text)
            self.assertEqual(client.post("/v1/rerank", json=body).status_code, 200)
