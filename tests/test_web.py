"""Local Web integration tests use synthetic models and evidence only."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

from finagent.config import HarnessSettings, Settings
from finagent.data.contracts import Observation
from finagent.data.evidence import EvidenceStore, write_json
from finagent.errors import AgentError
from finagent.harness.host import SessionHost
from finagent.models import ModelReply
from finagent.research.contracts import ResearchRequest, ResearchScope
from finagent.research.store import ResearchStore
from finagent.runtime import TZ, now
from tests.support import ScriptedModel, call_reply, tencent_body
from tests.auth_support import login, PASSWORD


@unittest.skipIf(TestClient is None, "Install finagent[web] to test the HTTP adapter")
class WebTests(unittest.TestCase):
    def setUp(self):
        from tests.mysql_support import isolated_database
        isolated_database(self)
        from finagent.web.app import create_app
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=self.root, harness=HarnessSettings(workspace=self.root, memory_enabled=False))
        self.model = ScriptedModel(ModelReply({"role": "assistant", "content": "合成研究答复，不是投资建议。"}))
        self.hosts = []
        def factory(settings, session_id):
            host = SessionHost(settings, session_id, model=self.model)
            self.hosts.append(host)
            return host
        self.app = create_app(self.settings, host_factory=factory, testing=True)
        self.client = TestClient(self.app)
        self.client.__enter__()
        user = self.app.state.auth.create_user('alice', PASSWORD)
        login(self.client)
        context = self.app.state.users.for_user(user['id'])
        # Fixture aliases only; production routes always resolve request identity.
        self.app.state.store, self.app.state.manager = context.store, context.manager
        self.settings, self.root = context.settings, context.settings.data_dir
        self.session = self.client.post("/api/v1/sessions", json={}).json()["id"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def submit(self, text="分析现金流", key="request-1", session=None):
        return self.client.post(f"/api/v1/sessions/{session or self.session}/messages", json={"text": text}, headers={"Idempotency-Key": key})

    def wait(self, run_id):
        for _ in range(250):
            value = self.client.get(f"/api/v1/runs/{run_id}").json()
            if value["status"] not in {"queued", "running", "cancelling"}:
                return value
            time.sleep(.02)
        self.fail("Synthetic Web run did not finish")

    def seed_report(self):
        store = ResearchStore(self.root)
        scope = ResearchScope(**ResearchRequest(goal="合成研究", symbol="600519.SH", as_of=datetime(2025, 5, 1, tzinfo=TZ), session_id=self.session).model_dump())
        research_id = store.create(scope, self.settings.research, "synthetic")
        evidence = EvidenceStore(self.root)
        raw = evidence.save_raw(b"synthetic", {"synthetic": True})
        observation = evidence.save_result(Observation(status="partial", source="合成年报", source_url="https://example.com/report.pdf", snapshot_id=raw, fetched_at=now(),
            data={"blocks": [{"locator": "page:3", "text": "合成原文，不是真实财务数据"}]}))
        report = {"report_markdown": "# 合成报告\n\n## 现金流\n\n合成分析。\n\n## 证据缺口\n\n待核对。", "content_status": "partial", "review_status": "issues_found",
            "outcome_verified": False, "artifacts": [{"artifact_id": "artifact_a", "claims": [{"claim_id": "claim_a", "text": "合成主张", "kind": "fact"}], "limitations": ["缺少完整年报"]}]}
        directory = store.directory(research_id)
        write_json(directory / "report.json", report)
        (directory / "report.md").write_text(report["report_markdown"], encoding="utf-8")
        write_json(directory / "evidence_manifest.json", {"references": [{"claim": "artifact_a:claim_a", "source": "合成年报", "source_url": "https://example.com/report.pdf", "locator": "page:3", "result_id": observation.result_id, "snapshot_id": raw}]})
        with store.connect() as database:
            database.change("research_runs", {"status":"completed", "content":"partial", "report":str(directory / "report.md")}, id=research_id)
        self.app.state.manager.runtime.put("work", self.session, {"id": "work_synthetic", "kind": "research", "status": "completed", "resources": {"research_id": research_id, "symbol": "600519.SH", "goal": "合成研究"}, "result": {"research_id": research_id, "report_available": True, "content_status": "partial"}})
        return research_id

    def test_home_and_safe_settings(self):
        self.assertEqual(self.client.get("/api/v1/health").json()["token_streaming"], False)
        self.assertEqual(len(self.client.get("/api/v1/workspace").json()["sessions"]), 1)
        body = self.client.get("/api/v1/settings").text
        self.assertNotIn("api_key", body)
        self.assertNotIn("base_url", body)

    def test_memory_write_cross_session_recall_and_http_forgetting(self):
        manager = self.app.state.manager
        manager.settings.harness.memory_enabled = True
        manager.settings.harness.auto_memory = False
        manager.settings.memory.embedding_backend = "disabled"

        def save_preference(messages):
            contexts = [json.loads(m["content"]) for m in messages
                        if m.get("role") == "user" and '"latest_user_source"' in m.get("content", "")]
            source = contexts[-1]["latest_user_source"][0]["id"]
            return call_reply(("memory_1", "memory_write", {"description": "回答风格", "body": "先给结论",
                                "type": "user", "source": source, "core": True}))

        self.model = ScriptedModel(save_preference,
            ModelReply({"role": "assistant", "content": "已记住回答风格。"}),
            ModelReply({"role": "assistant", "content": "合成解释。"}))
        first = self.wait(self.submit("以后回答先给结论", key="remember").json()["id"])
        self.assertEqual(first["status"], "completed", first)
        listed = self.client.get(f"/api/v1/sessions/{self.session}/memories")
        self.assertEqual(listed.status_code, 200)
        record = listed.json()["items"][0]
        self.assertTrue(record["core"])
        second = self.client.post("/api/v1/sessions", json={}).json()["id"]
        self.wait(self.submit("解释债券", key="recall", session=second).json()["id"])
        host = manager.host(second)
        self.assertEqual(host.recalled[0]["id"], record["id"])
        self.assertEqual(self.client.delete(f"/api/v1/sessions/{second}/memories/{record['id']}").status_code, 200)
        self.wait(self.submit("解释股票", key="after-forget", session=second).json()["id"])
        contexts = [json.loads(m["content"]) for m in self.model.messages_seen[-1]
                    if m.get("role") == "user" and '"memories"' in m.get("content", "")]
        self.assertTrue(contexts)
        self.assertTrue(all(not c["memories"] for c in contexts))
        self.assertEqual(self.client.get(f"/api/v1/sessions/{second}/memories").json()["items"], [])
        self.assertEqual(self.client.delete(f"/api/v1/sessions/{second}/memories/mem_unknown").status_code, 404)

    def test_memory_switch_remains_effective_for_web(self):
        body = self.client.get(f"/api/v1/sessions/{self.session}/memories").json()
        self.assertFalse(body["enabled"])
        self.assertNotIn("memory_write", self.app.state.manager.host(self.session).tool_filter)

    def test_model_connection_failure_keeps_safe_diagnostics_in_http_history(self):
        import httpx
        from finagent.config import ModelSettings
        from finagent.models import CompatibleChatModel
        from pydantic import SecretStr
        def unavailable(request):
            raise httpx.ConnectError("private-network-secret", request=request)
        self.model = CompatibleChatModel(ModelSettings(base_url="https://model.example/v1", name="synthetic", api_key=SecretStr("synthetic-key")),
                                        httpx.Client(transport=httpx.MockTransport(unavailable)))
        self.app.state.manager.settings.harness.model_retries = 0
        result = self.wait(self.submit().json()["id"])
        self.assertEqual(result["status"], "failed")
        diagnostic = result["result"]["diagnostic"]
        self.assertEqual(diagnostic["code"], "MODEL_UNAVAILABLE")
        self.assertEqual(diagnostic["transport_error"], "ConnectError")
        self.assertIn("FINAGENT_MODEL_PROXY", diagnostic["hint"])
        self.assertNotIn("/details", result["result"]["answer"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private-network-secret", serialized)
        self.assertNotIn("synthetic-key", serialized)
        history = self.client.get(f"/api/v1/sessions/{self.session}/messages").json()
        self.assertEqual(history["items"][0]["result"]["diagnostic"], diagnostic)

    def test_diagnostic_does_not_echo_provider_payload_or_credential(self):
        from finagent.web.diagnostics import failure_diagnostic
        value = failure_diagnostic("MODEL_HTTP_ERROR: HTTP 401 private-key provider-payload", False)
        self.assertEqual(value["http_status"], 401)
        self.assertNotIn("private-key", json.dumps(value))
        self.assertNotIn("provider-payload", json.dumps(value))

    def test_message_finishes_and_survives_readback(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202)
        result = self.wait(response.json()["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertIn("合成研究答复", result["result"]["answer"])
        history = self.client.get(f"/api/v1/sessions/{self.session}/messages").json()
        self.assertEqual(history["items"][0]["text"], "分析现金流")
        self.assertNotIn("fingerprint", history["items"][0])

    def test_bank_sector_message_uses_local_index_proxy_and_existing_quote_tool(self):
        import httpx
        from finagent.data.http import SourceHTTP
        from finagent.data.service import DataService
        self.model = ScriptedModel(call_reply(("bank_resolve", "resolve_entity", {"query":"银行板块"})),
            call_reply(("bank_quote", "get_quote", {"symbol": "399986.SZ"})),
            ModelReply({"role": "assistant", "content": "按中证银行指数代表口径，这是合成快照，最终收盘状态未知。"}))
        # Capture before submit establishes its cutoff. Generating the mock
        # timestamp inside respond can cross a second and trigger source fallback.
        snapshot_stamp = now().strftime("%Y%m%d%H%M%S")
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, content=tencent_body(code="399986", vendor="sz399986", stamp=snapshot_stamp))
        def factory(settings, session_id):
            service = DataService(settings, SourceHTTP(EvidenceStore(settings.data_dir), retries=0, client=httpx.Client(transport=httpx.MockTransport(respond))))
            host = SessionHost(settings, session_id, model=self.model, service=service)
            self.hosts.append(host)
            return host
        self.app.state.manager.host_factory = factory
        result = self.wait(self.submit("今天银行板块行情怎么样").json()["id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertIn("中证银行指数代表口径", result["result"]["answer"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.params["q"], "sz399986")
        self.assertEqual(self.hosts[0].ctx.symbol, "399986.SZ")

    def test_idempotency_and_conflicting_payload(self):
        first = self.submit().json()
        second = self.submit().json()
        self.assertEqual(first["id"], second["id"])
        self.wait(first["id"])
        self.assertEqual(self.model.index, 1)
        self.assertEqual(self.submit("不一样的问题").status_code, 409)

    def test_requires_idempotency_key_and_rejects_blank_text(self):
        path = f"/api/v1/sessions/{self.session}/messages"
        self.assertEqual(self.client.post(path, json={"text": "问题"}).status_code, 422)
        self.assertEqual(self.submit("   ").status_code, 422)

    def test_unknown_sessions_and_cli_sessions_are_not_exposed(self):
        self.assertEqual(self.submit(session="cli_private").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/sessions/cli_private/messages").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/research/" + "a" * 32 + "/report").status_code, 404)

    def test_local_origin_and_dns_rebinding_protection(self):
        self.assertEqual(self.client.get("/api/v1/workspace", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/workspace", headers={"Host": "evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/v1/workspace", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)

    def test_web_tool_filter_rejects_shell(self):
        result = self.wait(self.submit().json()["id"])
        host = self.hosts[0]
        from finagent.models import ToolCall
        from finagent.harness.kernel import Ledger
        value = host.execute(ToolCall("bad", "bash", '{"command":"touch forbidden"}'), host.ctx, Ledger(self.settings.limits))
        self.assertEqual(value["error"], "TOOL_DENIED")
        self.assertNotIn("task", host.tool_filter)
        self.assertIn("select_tool_groups", host.tool_filter)
        host.select_tool_groups(["workspace", "market"])
        names = {tool["function"]["name"] for tool in host.schemas(host.ctx)}
        self.assertIn("get_quote", names)
        self.assertNotIn("bash", names)
        self.assertFalse((self.root / "forbidden").exists())

    def test_same_session_serial_execution(self):
        gate = Event()
        started = Event()
        def slow(messages):
            started.set()
            gate.wait(3)
            return ModelReply({"role": "assistant", "content": "第一条"})
        self.model = ScriptedModel(slow, ModelReply({"role": "assistant", "content": "第二条"}))
        first = self.submit().json()
        self.assertTrue(started.wait(2))
        second = self.submit("继续", "request-2").json()
        self.assertEqual(self.client.get(f"/api/v1/runs/{second['id']}").json()["status"], "queued")
        gate.set()
        self.wait(first["id"])
        self.assertEqual(self.wait(second["id"])["result"]["answer"], "第二条")

    def test_cancel_reply_does_not_cancel_background_research(self):
        gate, started, research_stop = Event(), Event(), Event()
        def slow(messages):
            started.set()
            gate.wait(3)
            return ModelReply({"role": "assistant", "content": "合成答复"})
        self.model = ScriptedModel(slow)
        run_id = self.submit().json()["id"]
        self.assertTrue(started.wait(2))
        host = self.hosts[0]
        host.background.handles["work_unrelated"] = (None, research_stop)
        try:
            self.client.post(f"/api/v1/runs/{run_id}/cancel")
            self.assertFalse(research_stop.is_set())
            gate.set()
            self.assertEqual(self.wait(run_id)["status"], "cancelled")
        finally:
            host.background.handles.pop("work_unrelated")
            gate.set()

    def test_projection_is_replayable_and_does_not_ack_runtime_notifications(self):
        runtime = self.app.state.manager.runtime
        runtime.emit(self.session, "research_progress", {"text": "合成进度"}, notify=True)
        rows = runtime.events(self.session)
        store = self.app.state.store
        store.project(self.session, rows, self.app.state.manager.transform)
        store.project(self.session, rows, self.app.state.manager.transform)
        events = store.events()
        self.assertEqual(sum(event["type"] == "research.progress" for event in events), 1)
        self.assertEqual(len(runtime.pending(self.session)), 1)
        self.assertEqual(store.events(after=events[-1]["sequence"]), [])

    def test_report_status_citations_and_export_are_real_artifact_projections(self):
        research_id = self.seed_report()
        response = self.client.get(f"/api/v1/research/{research_id}/report")
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report["content_status"], "partial")
        self.assertFalse(report["outcome_verified"])
        self.assertEqual(report["key_findings"][0]["citation_ids"], ["1"])
        self.assertEqual(report["charts"], [])
        citation = self.client.get(f"/api/v1/research/{research_id}/citations/1").json()
        self.assertEqual(citation["excerpt"], "合成原文，不是真实财务数据")
        exported = self.client.get(f"/api/v1/research/{research_id}/export")
        self.assertIn("合成分析", exported.text)

    def test_report_question_is_owned_and_cannot_start_new_research(self):
        research_id = self.seed_report()
        with patch.object(self.model, "complete", wraps=self.model.complete) as complete:
            response = self.client.post(f"/api/v1/research/{research_id}/questions", json={"text": "这个结论有什么证据？"}, headers={"Idempotency-Key": "question"})
            result = self.wait(response.json()["id"])
            names = [tool["function"]["name"] for tool in complete.call_args.args[1]]
            self.assertNotIn("start_deep_research", names)
        self.assertEqual(result["status"], "completed", result)
        self.assertIn("start_deep_research", self.hosts[0].tool_filter)

    def test_document_library_routes_are_removed(self):
        self.assertEqual(self.client.get("/api/v1/documents").status_code, 404)
        response = self.client.post("/api/v1/documents", data={"metadata": "{}"},
            files={"file": ("report.md", b"retired upload")})
        self.assertIn(response.status_code, (404, 405))
        self.assertFalse((self.settings.data_dir / "library").exists())
        self.assertNotIn("max_upload_mb", self.client.get("/api/v1/settings").json())

    def test_explicit_research_is_idempotent(self):
        with patch("finagent.harness.research.ResearchService.start", return_value={"id": "b" * 32, "status": "completed", "content": "partial"}) as start:
            body = {"session_id": self.session, "text": "分析现金流质量", "symbol": "600519.SH"}
            first = self.client.post("/api/v1/research", json=body, headers={"Idempotency-Key": "research"})
            self.assertEqual(first.status_code, 202, first.text)
            second = self.client.post("/api/v1/research", json=body, headers={"Idempotency-Key": "research"})
            self.assertEqual(first.json()["id"], second.json()["id"])
            self.wait(first.json()["id"])
            for thread, _ in list(self.hosts[0].background.handles.values()):
                thread.join(3)
            start.assert_called_once()

    def test_restart_does_not_retry_unknown_execution(self):
        store = self.app.state.store
        value, _ = store.create_run(self.session, "interrupted", {"kind": "message", "text": "不自动重放"})
        store.recover()
        self.assertEqual(store.run(value["id"])["status"], "interrupted")

    def test_rejects_index_research_and_future_cutoff(self):
        body = {"session_id": self.session, "text": "分析走势", "symbol": "000300.SH"}
        self.assertEqual(self.client.post("/api/v1/research", json=body, headers={"Idempotency-Key": "index"}).status_code, 422)
        body.update(symbol="600519.SH", as_of="2099-01-01T00:00:00+08:00")
        self.assertEqual(self.client.post("/api/v1/research", json=body, headers={"Idempotency-Key": "future"}).status_code, 422)

    def test_background_research_admission_is_bounded(self):
        manager = self.app.state.manager
        for index in range(3):
            manager.runtime.put("work", self.session, {"id": f"work_{index}", "kind": "research", "status": "running", "resources": {}})
        with self.assertRaises(AgentError) as error:
            manager.launch_bounded(lambda: self.fail("Must not launch a fourth research"))
        self.assertEqual(error.exception.code, "RESEARCH_LIMIT")

    def test_internal_responses_do_not_leak_into_web_events(self):
        transform = self.app.state.manager.transform
        self.assertIsNone(transform({"kind": "model_response", "payload": {"message": {"reasoning_content": "private"}}}))
        kind, payload = transform({"kind": "tool_result", "payload": {"name": "get_quote", "result": {"data": {"private": "secret"}, "status": "partial"}}})
        self.assertEqual(kind, "tool.completed")
        self.assertNotIn("secret", json.dumps(payload))

    def test_native_research_pipeline_report_is_usable_via_http(self):
        from finagent.research.coordinator import ResearchCoordinator
        from tests.research21_support import native_factory
        coordinator = ResearchCoordinator(self.settings, runner_factory=native_factory(self.settings))
        with patch("finagent.research.coordinator.ResearchCoordinator", return_value=coordinator):
            response = self.client.post("/api/v1/research", json={"session_id": self.session, "text": "合成现金流研究", "symbol": "600519.SH", "as_of": "2025-05-01T00:00:00+08:00"}, headers={"Idempotency-Key": "native"})
            result = self.wait(response.json()["id"])
            self.assertEqual(result["status"], "completed", result)
            for thread, _ in list(self.hosts[0].background.handles.values()):
                thread.join(15)
                self.assertFalse(thread.is_alive())
        job = self.client.get("/api/v1/research").json()["items"][0]
        self.assertTrue(job["report_available"], job)
        report = self.client.get(f"/api/v1/research/{job['research_id']}/report")
        self.assertEqual(report.status_code, 200, report.text)
        self.assertTrue(report.json()["key_findings"][0]["citation_ids"])
        self.assertFalse(report.json()["outcome_verified"])


if __name__ == "__main__":
    unittest.main()
