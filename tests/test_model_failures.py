"""Model failures stay actionable through the session and terminal boundary."""
import json
from unittest.mock import patch
import httpx
from pydantic import SecretStr

from finagent.config import HarnessSettings, ModelSettings
from finagent.harness.host import SessionHost
from finagent.harness.terminal import run_terminal
from finagent.models import CompatibleChatModel
from finagent.presentation import failure_answer

from tests.support import ServiceCase


class ModelFailureTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.settings.harness = HarnessSettings(workspace=self.root, auto_memory=False,
                                                memory_enabled=False)

    def test_http_failure_reaches_terminal_and_snapshot_without_response_secrets(self):
        for protocol in ("chat_completions", "anthropic_messages"):
            for status, advice in ((401, "鉴权"), (402, "计费状态"), (403, "权限"),
                                   (404, "FINAGENT_BASE_URL")):
                with self.subTest(protocol=protocol, status=status):
                    requests = []

                    def respond(request):
                        requests.append(request)
                        return httpx.Response(status, text="private-provider-payload synthetic-key")

                    model = CompatibleChatModel(ModelSettings(
                        base_url="https://model.example/v1", name="synthetic-model",
                        api_key=SecretStr("synthetic-key"), protocol=protocol),
                        httpx.Client(transport=httpx.MockTransport(respond)))
                    session = f"failure_{protocol}_{status}"
                    host = SessionHost(self.settings, session, model=model, service=self.service)
                    inputs = iter(("你能帮我做些什么", "/exit"))
                    output = []
                    run_terminal(self.settings, host=host, read=lambda _: next(inputs), write=output.append)
                    answer = "\n".join(output)
                    self.assertIn(f"HTTP {status}", answer)
                    self.assertIn(advice, answer)
                    self.assertIn("[工具] 本轮未调用工具。", answer)
                    self.assertNotIn("无法给出可靠结论", answer)
                    snapshot = host.store.load(session)
                    result = snapshot["state"]["last_result"]
                    self.assertEqual(result["status"], "failed")
                    self.assertIn("MODEL_HTTP_ERROR", result["reason"])
                    self.assertFalse(result["error_retryable"])
                    self.assertEqual(result["tool_calls"], 0)
                    self.assertEqual(len(requests), 1)
                    stored = json.dumps(snapshot)
                    for secret in ("private-provider-payload", "synthetic-key"):
                        self.assertNotIn(secret, answer)
                        self.assertNotIn(secret, stored)

    def test_failure_display_only_uses_status_from_reason(self):
        for status, advice in ((400, "请求参数"), (429, "限流"), (503, "稍后重试"),
                               (418, "账户状态")):
            with self.subTest(status=status):
                answer = failure_answer("MODEL_HTTP_ERROR", reason=f"HTTP {status}: private-secret")
                self.assertIn(str(status), answer)
                self.assertIn(advice, answer)
                self.assertNotIn("private-secret", answer)
        self.assertIn("拒绝了请求", failure_answer("MODEL_HTTP_ERROR", reason="private-secret"))
        for code, config in (("MODEL_NOT_CONFIGURED", "FINAGENT_MODEL"),
                             ("MODEL_KEY_MISSING", "api_key_env"),
                             ("INVALID_MODEL_URL", "FINAGENT_BASE_URL")):
            with self.subTest(code=code):
                self.assertIn(config, failure_answer(code))

    def test_explicit_proxy_is_passed_to_httpx_for_both_protocols(self):
        for protocol in ("chat_completions", "anthropic_messages"):
            with self.subTest(protocol=protocol), patch("finagent.models.httpx.Client") as client:
                model = CompatibleChatModel(ModelSettings(base_url="https://model.example/v1", name="synthetic",
                    api_key=SecretStr("synthetic-key"), protocol=protocol, proxy_url="http://127.0.0.1:17897"))
                client.assert_called_once_with(follow_redirects=False, proxy="http://127.0.0.1:17897")
                model.close()

    def test_proxy_configuration_rejects_credentials_and_paths(self):
        for value in ("socks5://localhost:1080", "http://user:secret@localhost:8080", "http://localhost:8080/path", "http://localhost:99999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ModelSettings(proxy_url=value)

    def test_transport_hints_do_not_echo_private_error_payloads(self):
        for error, expected in (("ConnectError", "FINAGENT_MODEL_PROXY"), ("ProxyError", "端口"), ("ReadTimeout", "超时")):
            answer = failure_answer("MODEL_UNAVAILABLE", reason=f"{error}: secret-key private-payload")
            self.assertIn(expected, answer)
            self.assertNotIn("secret-key", answer)
            self.assertNotIn("private-payload", answer)
