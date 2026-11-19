"""Memory retrieval behavior; synthetic embeddings do not establish model quality."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
from pydantic import ValidationError

from finagent.config import MemorySettings, Settings, load_settings
from finagent.errors import AgentError
from finagent.harness.context import MemoryStore
from finagent.harness.host import SessionHost
from finagent.harness.kernel import KernelResult
from finagent.harness.memory_search import HTTPEmbedder, QWEN3_MEMORY_INSTRUCTION
from finagent.harness.state import atomic_json, encode
from finagent.models import ModelReply
from tests.support import ScriptedModel


def reply(content):
    return ModelReply({"role": "assistant", "content": content})


class Embeddings:
    fingerprint = "synthetic-v1"

    def __init__(self, mapping):
        self.mapping, self.calls = mapping, []

    def embed(self, texts, timeout):
        self.calls.append(list(texts))
        return [self.mapping[text] for text in texts]


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def store(self, user="alice", project="project_a", embedder=None, **options):
        options.setdefault("rerank_backend", "chat")  # Explicit legacy chat-ranking regressions.
        settings = MemorySettings(user_id=user, embedding_backend="disabled", **options)
        return MemoryStore(self.root / "memory", settings=settings, project_id=project, embedder=embedder)

    def test_core_preferences_are_present_without_similarity_and_obey_budgets(self):
        store = self.store(core_limit=1)
        store.write("旧偏好", "喜欢长回答", source="u1")
        newest = store.write("新偏好", "先给结论", source="u2")
        store.write("历史事件", "曾看过某家公司的年报", source="u3", core=False)
        model = Mock()
        self.assertEqual([r["id"] for r in store.select("How do bonds work?", model)], [newest["id"]])
        model.complete.assert_not_called()
        store.settings.core_chars = 0
        self.assertEqual(store.select("How do bonds work?"), [])

    def test_user_and_project_filter_before_embedding_or_reranking(self):
        a = self.store()
        shared = a.write("风格", "先给结论", source="u1")
        own = a.write("项目甲", "研究现金流", "project", "u2")
        other = self.store(project="project_b")
        foreign = other.write("项目乙", "现金流中的专有方案", "project", "u3")
        bob = self.store(user="bob")
        private = bob.write("秘密项目", "现金流资料", "project", "u4")
        embedder = Embeddings({"现金流": [1, 0], "项目甲\n研究现金流": [1, 0]})
        a.embedder = embedder
        selected = a.select("现金流")
        self.assertEqual({r["id"] for r in selected}, {shared["id"], own["id"]})
        self.assertNotIn(foreign["id"], {r["id"] for r in a.records()})
        self.assertNotIn(private["id"], {r["id"] for r in a.records()})
        self.assertEqual({r["id"] for r in other.records()}, {shared["id"], foreign["id"]})
        self.assertEqual(embedder.calls, [["现金流"], ["项目甲\n研究现金流"]])
        with self.assertRaises(AgentError):
            a.forget(foreign["id"])

    def test_semantic_recall_without_keyword_overlap_and_cache_survives_restart(self):
        embedder = Embeddings({"我承受不了太大亏损": [1, 0], "投资方式\n偏好保守配置": [.9, .1],
                               "出行安排\n周末去杭州": [0, 1]})
        store = self.store(embedder=embedder)
        wanted = store.write("投资方式", "偏好保守配置", source="u1", core=False)
        store.write("出行安排", "周末去杭州", "reference", "u2")
        self.assertEqual([r["id"] for r in store.select("我承受不了太大亏损")], [wanted["id"]])
        self.assertEqual(store.last_recall["keyword_count"], 0)
        self.assertEqual(store.last_recall["semantic_status"], "ok")
        restored = self.store(embedder=embedder)
        restored.select("我承受不了太大亏损")
        self.assertEqual(embedder.calls[-1], ["我承受不了太大亏损"])
        self.assertEqual(len(embedder.calls), 3)
        embedder.fingerprint = "synthetic-v2"
        restored.select("我承受不了太大亏损")
        self.assertEqual(len(embedder.calls[-1]), 2)

    def test_chinese_overlapping_keywords_and_exact_stock_identifiers(self):
        store = self.store()
        wanted = store.write("现金流", "600519.SH 的现金流分析方法", "project", "u1")
        store.write("参考", "000858.SZ 的研究笔记", "reference", "u2")
        self.assertEqual(store.select("企业的现金流")[0]["id"], wanted["id"])
        self.assertEqual(store.select("600519.SH")[0]["id"], wanted["id"])
        self.assertEqual(store.select("债券久期"), [])

    def test_hybrid_fusion_and_reranking_only_when_channels_compete(self):
        embedder = Embeddings({"现金流": [1, 0], "现金流\n现金流分析": [0, 1],
                               "资金\n企业收付款方法": [1, 0]})
        store = self.store(embedder=embedder, recall_limit=1, candidate_limit=2)
        lexical = store.write("现金流", "现金流分析", "project", "u1")
        semantic = store.write("资金", "企业收付款方法", "project", "u2")
        model = ScriptedModel(reply(json.dumps([semantic["id"]])))
        result = store.select("现金流", model)
        self.assertEqual([r["id"] for r in result], [semantic["id"]])
        self.assertEqual(store.last_recall["rerank_status"], "ok")
        prompt = json.loads(model.messages_seen[0][-1]["content"])
        self.assertEqual({r["id"] for r in prompt["candidates"]}, {lexical["id"], semantic["id"]})
        store.settings.recall_limit = 2
        model = Mock()
        self.assertEqual(len(store.select("现金流", model)), 2)
        model.complete.assert_not_called()

    def test_invalid_reranking_keeps_retrieved_candidates_and_cannot_inject_foreign_id(self):
        store = self.store(rerank="always")
        wanted = store.write("现金流", "现金流方法", "project", "u1")
        result = store.select("现金流", ScriptedModel(reply('["mem_foreign"]')))
        self.assertEqual([r["id"] for r in result], [wanted["id"]])
        self.assertEqual(store.last_recall["rerank_status"], "fallback")
        self.assertEqual(store.select("现金流", ScriptedModel(reply('[]'))), [])

    def test_embedding_outage_and_invalid_vectors_fall_back_to_keywords(self):
        store = self.store(embedder=Mock())
        wanted = store.write("现金流", "经营现金流分析", "project", "u1")
        store.embedder.embed.side_effect = AgentError("UNAVAILABLE", "synthetic")
        self.assertEqual(store.select("经营现金流")[0]["id"], wanted["id"])
        self.assertEqual(store.last_recall["semantic_status"], "unavailable")
        store.embedder = Embeddings({"经营现金流": [float("nan"), 0]})
        self.assertEqual(store.select("经营现金流")[0]["id"], wanted["id"])
        self.assertEqual(store.last_recall["semantic_status"], "unavailable")

    def test_forgetting_removes_vectors_and_tombstones_are_scoped(self):
        embedder = Embeddings({"现金流": [1, 0], "现金流\n现金流方法": [1, 0]})
        store = self.store(embedder=embedder)
        record = store.write("现金流", "现金流方法", "project", "u1")
        store.select("现金流")
        store.forget(record["id"])
        database, scope = store.vectors.backend()
        with database.transaction(scope) as db:
            self.assertEqual(db.count("memory_vectors", memory_user_id=store.user_id), 0)
        self.assertEqual(store.write("现金流", "现金流方法", "project", "u1")["status"], "forgotten")
        self.assertIn("id", self.store(project="project_b").write("现金流", "现金流方法", "project", "u1"))
        self.assertIn("id", self.store(user="bob").write("现金流", "现金流方法", "project", "u1"))
        self.assertEqual(store.select("现金流"), [])

    def test_concurrent_forgetting_during_embedding_does_not_reintroduce_memory(self):
        store = self.store()
        record = store.write("现金流", "现金流方法", "project", "u1")
        embedder = Embeddings({"现金流": [1, 0], "现金流\n现金流方法": [1, 0]})
        original = embedder.embed

        def forget(texts, timeout):
            if texts == ["现金流"]:
                store.forget(record["id"])
            return original(texts, timeout)

        embedder.embed = forget
        store.embedder = embedder
        self.assertEqual(store.select("现金流"), [])

    def test_consolidation_cannot_merge_project_scopes_and_keeps_transitive_sources(self):
        store = self.store()
        shared = store.write("偏好", "回答详细", source="u1")
        local = store.write("偏好", "本项目回答详细", source="u2", project_scoped=True)
        proposal = [{"keep_id": shared["id"], "remove_ids": [local["id"]], "body": "无效跨范围合并"}]
        self.assertEqual(store.consolidate(ScriptedModel(reply(json.dumps(proposal)))), [])
        second = store.write("偏好", "回答稍详细", source="u3")
        proposal = [{"keep_id": second["id"], "remove_ids": [shared["id"]], "body": "回答稍详细"}]
        store.consolidate(ScriptedModel(reply(json.dumps(proposal))))
        third = store.write("偏好", "回答更详细", source="u4")
        proposal = [{"keep_id": third["id"], "remove_ids": [second["id"]], "body": "回答更详细"}]
        store.consolidate(ScriptedModel(reply(json.dumps(proposal))))
        merged = next(r for r in store.records() if r["id"] == third["id"])
        self.assertEqual(set(merged["sources"]), {"u1", "u3", "u4"})

    def test_legacy_migration_claims_original_project_once(self):
        root = self.root / "memory"
        root.mkdir()
        atomic_json(root / "mem_old.json", dict(id="mem_old", description="旧偏好", body="回答简短",
                    type="user", source="u1", scope="persistent", created_at="2026-01-01"))
        self.assertEqual(self.store(user="bob").records(), [])
        local = self.store(user="local")
        self.assertEqual(local.records()[0]["project_id"], "project_a")
        self.assertFalse((root / "mem_old.json").exists())
        self.assertEqual(self.store(user="local", project="project_b").records(), [])

    def test_result_character_budget_and_candidates_are_bounded(self):
        store = self.store(recall_chars=1000, candidate_limit=5)
        for index in range(12):
            store.write("现金流", "现金流" + str(index) + "资料" * 250, "reference", "u" + str(index))
        records = store.select("现金流")
        self.assertLessEqual(sum(len(encode(r)) for r in records), 1000)

    def test_http_embedding_adapters_validate_protocol_and_hide_provider_errors(self):
        for backend in ("ollama", "openai"):
            settings = MemorySettings(embedding_backend=backend, embedding_model="synthetic")
            seen = []

            def handler(request):
                seen.append(request)
                if backend == "ollama":
                    return httpx.Response(200, json={"embeddings": [[3, 4], [4, 3]]})
                return httpx.Response(200, json={"data": [{"index": 1, "embedding": [4, 3]},
                                                          {"index": 0, "embedding": [3, 4]}]})

            client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch("finagent.harness.memory_search.httpx.Client", return_value=client):
                self.assertEqual(HTTPEmbedder(settings).embed(["a", "b"], 1), [[.6, .8], [.8, .6]])
            self.assertTrue(str(seen[0].url).endswith("/api/embed" if backend == "ollama" else "/embeddings"))
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(500, text="private-provider-secret")))
        with patch("finagent.harness.memory_search.httpx.Client", return_value=client):
            with self.assertRaises(AgentError) as raised:
                HTTPEmbedder(settings).embed(["a"], 1)
            self.assertNotIn("private-provider-secret", str(raised.exception))

    def test_embeddinggemma_uses_distinct_query_and_document_formats(self):
        settings = MemorySettings(embedding_model="embeddinggemma")
        embedder = HTTPEmbedder(settings)
        self.assertEqual(embedder.query_text("风险"), "task: search result | query: 风险")
        self.assertEqual(embedder.document_text({"description": "投资", "body": "偏好保本"}),
                         "title: 投资 | text: 偏好保本")
        plain = HTTPEmbedder(settings.model_copy(update={"embedding_format": "plain"}))
        self.assertNotEqual(plain.fingerprint, embedder.fingerprint)
        self.assertEqual(plain.query_text("风险"), "风险")

    def test_qwen3_query_instruction_and_unprefixed_documents(self):
        record = {"description": "投资", "body": "偏好保本"}
        for name in ("qwen3-embedding:0.6b", "Qwen/Qwen3-Embedding-0.6B"):
            with self.subTest(model=name):
                embedder = HTTPEmbedder(MemorySettings(embedding_model=name))
                self.assertEqual(embedder.query_text("风险"),
                                 f"Instruct: {QWEN3_MEMORY_INSTRUCTION}\nQuery:风险")
                self.assertEqual(embedder.document_text(record), "投资\n偏好保本")
        settings = MemorySettings(embedding_model="custom-alias", embedding_format="qwen3")
        embedder = HTTPEmbedder(settings)
        plain = HTTPEmbedder(settings.model_copy(update={"embedding_format": "plain"}))
        self.assertNotEqual(embedder.fingerprint, plain.fingerprint)
        with patch("finagent.harness.memory_search.QWEN3_MEMORY_INSTRUCTION", "changed task"):
            self.assertNotEqual(embedder.fingerprint, HTTPEmbedder(settings).fingerprint)

    def test_configuration_env_and_secret_redaction(self):
        config = self.root / "finagent.toml"
        config.write_text('[memory]\nuser_id="alice"\n', encoding="utf-8")
        (self.root / ".env").write_text('FINAGENT_EMBEDDING_BACKEND=openai\n'
            'FINAGENT_EMBEDDING_ENDPOINT=https://embed.example/v1\n'
            'FINAGENT_EMBEDDING_MODEL=synthetic\nFINAGENT_EMBEDDING_API_KEY=private-embedding-key\n', encoding="utf-8")
        settings = load_settings(config)
        self.assertEqual(settings.memory.embedding_backend, "openai")
        self.assertNotIn("private-embedding-key", settings.model_dump_json())
        from finagent.security import SecurityGuard
        self.assertNotIn("private-embedding-key", SecurityGuard(settings).redact("private-embedding-key")[0])
        with self.assertRaises(ValidationError):
            MemorySettings(user_id="../bob")

    def test_host_cross_session_recall_and_scope_change_rejects_old_session(self):
        settings = Settings(data_dir=self.root / "data")
        settings.harness.workspace = self.root
        settings.harness.auto_memory = False
        settings.memory.embedding_backend = "disabled"
        first = SessionHost(settings, "first", model=ScriptedModel(reply("完成")))
        self.addCleanup(first.close)
        first.memory.write("输出风格", "先给结论", source="user_1")
        first.submit("解释债券")
        first.close()
        model = ScriptedModel(reply("完成"))
        second = SessionHost(settings, "second", model=model)
        self.addCleanup(second.close)
        second.submit("解释市盈率")
        self.assertEqual(len(model.messages_seen), 1)  # Core preferences need no selector call.
        contexts = [json.loads(m["content"]) for m in model.messages_seen[0]
                    if m.get("role") == "user" and '"memories"' in m.get("content", "")]
        self.assertEqual(contexts[-1]["memories"][0]["body"], "先给结论")
        settings.memory.user_id = "bob"
        with self.assertRaises(AgentError) as raised:
            SessionHost(settings, "first", model=ScriptedModel(reply("完成")))
        self.assertEqual(raised.exception.code, "MEMORY_SCOPE_MISMATCH")

    def test_auto_extraction_preserves_source_and_project_scope(self):
        settings = Settings(data_dir=self.root / "data")
        settings.harness.workspace = self.root
        settings.memory.embedding_backend = "disabled"

        def extract(messages):
            source = host.state["user_updates"][-1]["id"]
            return reply(json.dumps([{"description": "项目表达偏好", "body": "本项目回答先给结论",
                    "type": "user", "scope": "persistent", "source": source,
                    "core": True, "project_scoped": True}], ensure_ascii=False))

        host = SessionHost(settings, "automatic", model=ScriptedModel(reply("已记住"), extract))
        self.addCleanup(host.close)
        self.assertEqual(host.submit("本项目的回答请先给结论").status, "completed")
        record = host.memory.records()[0]
        self.assertEqual(record["source"], host.state["user_updates"][-1]["id"])
        self.assertEqual(record["project_id"], host.memory.project_id)
        self.assertTrue(record["core"])

    def test_discovery_runs_once_after_each_user_turn_and_allows_empty_results(self):
        settings = Settings(data_dir=self.root / "data")
        settings.harness.workspace = self.root
        settings.memory.embedding_backend = "disabled"
        model = ScriptedModel(reply("现金流是资金的流入和流出。"), reply("[]"),
                              reply("例如销售收款。"), reply("[]"))
        host = SessionHost(settings, "every_turn", model=model)
        self.addCleanup(host.close)
        with patch.object(host.memory, "consolidate") as consolidate:
            self.assertEqual(host.submit("现金流是什么").answer, "现金流是资金的流入和流出。")
            self.assertEqual(host.submit("举个例子").answer, "例如销售收款。")
            consolidate.assert_not_called()
        self.assertEqual(len(model.messages_seen), 4)
        self.assertEqual(host.state["usage"]["memory_extraction"]["calls"], 2)
        self.assertEqual(host.state["last_ledger"]["operations"]["model"], 2)
        self.assertEqual(host.memory.records(), [])
        payload = json.loads(model.messages_seen[-1][-1]["content"])
        self.assertEqual([m["content"] for m in payload["messages"]], ["现金流是什么", "举个例子"])
        self.assertEqual(payload["existing_memory_catalog"], [])

    def test_discovery_respects_switches_and_skips_background_and_incomplete_turns(self):
        cases = [("memory_off", False, True, "正常输入", "completed"),
                 ("auto_off", True, False, "正常输入", "completed"),
                 ("background", True, True, None, "completed"),
                 ("failed", True, True, "正常输入", "failed"),
                 ("cancelled", True, True, "正常输入", "cancelled"),
                 ("clarification", True, True, "正常输入", "needs_clarification")]
        for name, enabled, automatic, text, status in cases:
            with self.subTest(name=name):
                settings = Settings(data_dir=self.root / name)
                settings.harness.workspace = self.root
                settings.harness.memory_enabled = enabled
                settings.harness.auto_memory = automatic
                settings.memory.embedding_backend = "disabled"
                host = SessionHost(settings, name, model=Mock())
                self.addCleanup(host.close)
                with patch("finagent.harness.host.AgentKernel.run", return_value=KernelResult(status=status)), \
                     patch.object(host.memory, "extract") as extract:
                    self.assertEqual(host.submit(text).status, status)
                    extract.assert_not_called()

    def test_discovery_failure_does_not_replace_completed_answer(self):
        for index, failure in enumerate((reply("invalid json"), reply("{}"),
                ModelReply({"role": "assistant", "content": "[]"}, finish_reason="length"),
                AgentError("MODEL_UNAVAILABLE", "synthetic failure"))):
            with self.subTest(failure=index):
                settings = Settings(data_dir=self.root / str(index))
                settings.harness.workspace = self.root
                settings.memory.embedding_backend = "disabled"

                def discover(_messages):
                    if isinstance(failure, AgentError):
                        raise failure
                    return failure

                host = SessionHost(settings, "discovery_failure", model=ScriptedModel(reply("正常答复"), discover))
                self.addCleanup(host.close)
                result = host.submit("解释现金流")
                self.assertEqual((result.status, result.answer), ("completed", "正常答复"))
                self.assertEqual(host.memory.records(), [])

    def test_extraction_catalog_is_scoped_and_duplicate_or_forgotten_records_are_not_added(self):
        store = self.store()
        own = store.write("回答风格", "先给结论", source="u1")
        deleted = store.write("输出形式", "用表格比较", source="u2")
        store.forget(deleted["id"])
        foreign_project = self.store(project="project_b").write("内部项目乙", "项目乙背景", "project", "u3")
        self.store(user="bob").write("其他用户", "其他用户背景", source="u4")
        candidates = [{"description": "回答风格", "body": "先给结论", "type": "user", "source": "u1", "scope": "persistent"},
                      {"description": "输出形式", "body": "用表格比较", "type": "user", "source": "u2", "scope": "persistent"}]
        model = ScriptedModel(reply(json.dumps(candidates, ensure_ascii=False)))
        self.assertEqual(store.extract([{"id": "u1", "content": "回答先给结论"},
                                        {"id": "u2", "content": "用表格比较"}], model, lambda _: None), [])
        payload = json.loads(model.messages_seen[0][-1]["content"])
        self.assertEqual([r["id"] for r in payload["existing_memory_catalog"]], [own["id"]])
        self.assertNotIn(foreign_project["id"], encode(payload))
        self.assertEqual([r["id"] for r in store.records()], [own["id"]])

    def test_extraction_rejects_foreign_sources_temporary_scopes_and_current_task_constraints(self):
        store = self.store()
        candidates = [{"description": "风格", "body": "简短回答", "type": "user", "source": "invented", "scope": "persistent"},
                      {"description": "风格", "body": "简短回答", "type": "user", "source": "u1", "scope": "current_task"},
                      {"description": "风格", "body": "简短回答", "type": "user", "source": "u2", "scope": "persistent"}]
        self.assertEqual(store.extract([{"id": "u1", "content": "回答简短一点"},
                                        {"id": "u2", "content": "这次回答简短一点"}],
                                       ScriptedModel(reply(json.dumps(candidates))), lambda _: None), [])
        self.assertEqual(store.records(), [])

    def test_extraction_payload_keeps_valid_source_ids_and_bounds_long_messages(self):
        store = self.store()
        model = ScriptedModel(reply("[]"))
        store.extract([{"id": f"u{i}", "content": "长输入" * 6000} for i in range(6)], model, lambda _: None)
        payload = json.loads(model.messages_seen[0][-1]["content"])
        self.assertEqual([m["id"] for m in payload["messages"]], ["u2", "u3", "u4", "u5"])
        self.assertTrue(all(len(m["content"]) <= 3500 for m in payload["messages"]))


if __name__ == "__main__":
    unittest.main()
