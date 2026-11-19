"""Ollama Qwen3 parser. No encoder, torch, network download, or semantic fallback."""
from hashlib import sha256
from threading import RLock
import time

import httpx

from ..errors import AgentError
from .bundle import load_bundle, file_hash
from .contracts import NLUResult
from .semantic import SCHEMA, PROMPT_HASH, messages, render_prompt, strict_json, validate_frame


class Qwen3Parser:
    def __init__(self, settings, *, diagnostic=False, transport=None):
        self.config = settings.nlu.model_copy(deep=True)
        self.bundle = load_bundle(self.config, diagnostic=diagnostic)
        self.diagnostic = diagnostic
        self.unqualified_authorized = (not diagnostic and self.config.allow_unqualified
                                       and self.bundle.quality_status == "diagnostic")
        self.fingerprint = self.bundle.fingerprint()
        self.transport = transport
        try:
            from tokenizers import Tokenizer
            self.tokenizer = Tokenizer.from_file(str(self.config.model_dir / "tokenizer.json"))
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
        except Exception as exc:
            raise AgentError("NLU_TOKENIZER_INVALID", "无法加载部署包tokenizer；不允许跳过token预检") from exc

    def _request(self, client, method, path, deadline, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentError("NLU_TIMEOUT", "Ollama意图解析超过请求总时限")
        try:
            response = client.request(method, self.config.endpoint.rstrip("/") + path, timeout=remaining, **kwargs)
            if response.status_code == 404:
                raise AgentError("NLU_MODEL_MISSING", "Ollama模型或API不存在；请先导入配置中的Qwen3模型")
            response.raise_for_status()
            result = strict_json(response.text)
            if not isinstance(result, dict) or result.get("error"):
                raise ValueError("Ollama响应不是成功对象")
            if time.monotonic() > deadline:
                raise AgentError("NLU_TIMEOUT", "Ollama意图解析超过请求总时限")
            return result
        except httpx.TimeoutException as exc:
            raise AgentError("NLU_TIMEOUT", "Ollama意图解析超时；未启动研究") from exc
        except httpx.HTTPError as exc:
            raise AgentError("NLU_SERVICE_UNAVAILABLE", "无法访问Ollama意图服务；请检查服务地址和模型状态") from exc
        except (ValueError, TypeError) as exc:
            raise AgentError("NLU_INVALID_RESPONSE", "Ollama服务响应无效") from exc

    def _identity(self, client, deadline):
        available = self._request(client, "GET", "/api/tags", deadline).get("models", [])
        if not isinstance(available, list):
            raise AgentError("NLU_INVALID_RESPONSE", "Ollama模型清单格式无效")
        matches = [m for m in available if isinstance(m, dict) and m.get("name", m.get("model")) == self.bundle.model_id]
        if len(matches) != 1:
            raise AgentError("NLU_MODEL_MISSING", "Ollama未找到配置的意图模型tag；请先ollama create导入")
        digest = matches[0].get("digest")
        if not isinstance(digest, str):
            raise AgentError("NLU_INVALID_RESPONSE", "Ollama模型摘要格式无效")
        if digest.removeprefix("sha256:") != self.bundle.ollama_digest:
            raise AgentError("NLU_MODEL_CHANGED", "Ollama模型tag内容已改变，请重新导出并验收部署包")
        return matches[0]

    def check(self):
        with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False) as client:
            deadline = time.monotonic() + self.config.timeout_seconds
            version = self._request(client, "GET", "/api/version", deadline)
            model = self._identity(client, deadline)
            running = self._request(client, "GET", "/api/ps", deadline)
        return {"status": "ok", "backend": "qwen3_ollama", "model_id": self.bundle.model_id,
                "ollama_digest": self.bundle.ollama_digest, "gguf_sha256": self.bundle.gguf_sha256,
                "deployment_fingerprint": self.fingerprint, "quality_status": self.bundle.quality_status,
                "diagnostic_only": self.diagnostic, "unqualified_authorized": self.unqualified_authorized,
                "ollama_version": version.get("version"),
                "details": model.get("details"), "running": running.get("models", [])}

    def ensure_identity(self):
        current = load_bundle(self.config, diagnostic=self.diagnostic)
        if current.fingerprint() != self.fingerprint:
            raise AgentError("NLU_MODEL_CHANGED", "部署包在会话中改变，请重新输入请求")
        with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False) as client:
            self._identity(client, time.monotonic() + self.config.timeout_seconds)

    def parse(self, text, context=None):
        started = time.monotonic()
        deadline = started + self.config.timeout_seconds
        try:
            if not isinstance(text, str) or len(text) > self.config.max_input_chars:
                raise AgentError("NLU_INPUT_TOO_LONG", "意图输入超长，请缩短或拆分；没有截断原文")
            conversation = messages(text, {} if context is None else context)
            user = conversation[1]["content"]
            if len(user) > self.config.max_input_chars + 16000:
                raise AgentError("NLU_INPUT_TOO_LONG", "上下文过长，请清理会话或拆分请求")
            if any(token in user for token in ("<|im_start|>", "<|im_end|>", "<|endoftext|>")):
                raise AgentError("NLU_INVALID_INPUT", "输入包含模型协议控制标记，请移除后重试")
            count = len(self.tokenizer.encode(render_prompt(conversation), add_special_tokens=False).ids)
            if count + self.config.output_tokens + 32 > self.config.context_tokens:
                raise AgentError("NLU_INPUT_TOO_LONG", "完整输入加生成预算超过上下文窗口，请缩短；未调用生成接口")
        except (ValueError, TypeError, OverflowError) as exc:
            raise AgentError("NLU_INVALID_INPUT", "意图输入或有限上下文无效") from exc
        # Recheck artifacts on every request, including cached parser instances.
        current = load_bundle(self.config, diagnostic=self.diagnostic)
        if current.fingerprint() != self.fingerprint:
            raise AgentError("NLU_MODEL_CHANGED", "部署包在会话中改变，请重新启动请求")
        with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False) as client:
            self._identity(client, deadline)
            response = self._request(client, "POST", "/api/chat", deadline, json={
                "model": self.bundle.model_id, "messages": conversation,
                "format": SCHEMA, "think": False, "stream": False, "keep_alive": "5m",
                "options": {"temperature": 0, "seed": 42, "num_ctx": self.config.context_tokens,
                            "num_predict": self.config.output_tokens}})
            self._identity(client, deadline)
        try:
            if response.get("done") is not True or response.get("done_reason") != "stop":
                raise ValueError("响应未完成或触及生成上限")
            if response.get("model") != self.bundle.model_id:
                raise ValueError("返回模型名不符合请求")
            # Ollama may report only uncached prompt tokens. A smaller count is
            # not evidence of truncation; full-input preflight already reserved
            # generation space plus a fixed template margin before the request.
            if (type(response.get("prompt_eval_count")) is not int or
                    not 0 <= response["prompt_eval_count"] <= count + 32):
                raise AgentError("NLU_TEMPLATE_MISMATCH", "Ollama模板token开销超过预检范围；请检查导出模板")
            message = response["message"]
            if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("thinking") or message.get("tool_calls"):
                raise ValueError("意图模型不得生成思考或工具调用")
            raw = validate_frame(strict_json(message["content"]), text)
            return NLUResult(**raw, model_id=self.bundle.model_id, bundle_sha256=self.fingerprint,
                             model_sha256=self.bundle.ollama_digest, prompt_sha256=PROMPT_HASH,
                             diagnostic_only=self.diagnostic, quality_status=self.bundle.quality_status,
                             unqualified_authorized=self.unqualified_authorized,
                             admission="reject" if self.diagnostic else "accept",
                             usage={"input_tokens": count, "output_tokens": response.get("eval_count"),
                                    "load_duration_ns": response.get("load_duration"),
                                    "total_duration_ns": response.get("total_duration")})
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentError("NLU_INVALID_OUTPUT", "Qwen3输出不符合语义JSON、枚举或原文实体合同；未启动研究") from exc


_CACHE = {}
_LOCK = RLock()


def get_parser(settings):
    if settings.nlu.backend != "qwen3_ollama":
        raise AgentError("NLU_BACKEND_INVALID", "统一语义入口只接受qwen3_ollama")
    key = settings.nlu.model_dump_json() + ":" + (file_hash(settings.nlu.model_dir / "manifest.json") if (settings.nlu.model_dir / "manifest.json").is_file() else "missing")
    with _LOCK:
        if key not in _CACHE:
            parser = Qwen3Parser(settings)
            _CACHE.clear()
            _CACHE[key] = parser
        return _CACHE[key]


def clear_cache():
    with _LOCK:
        _CACHE.clear()
