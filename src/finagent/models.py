from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import json
from typing import Protocol
from urllib.parse import urlparse

import httpx

from .config import ModelSettings
from .errors import AgentError


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ModelReply:
    message: dict
    calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)


class ChatModel(Protocol):
    def complete(self, messages: list[dict], tools: list[dict], timeout: float) -> ModelReply: ...


class LazyChatModel:
    """Keep local help/diagnostics usable before a remote model is configured."""
    def __init__(self, factory):
        self.factory, self.instance = factory, None

    def complete(self, messages, tools, timeout):
        if self.instance is None:
            self.instance = self.factory()
        return self.instance.complete(messages, tools, timeout)

    def close(self):
        if self.instance is not None:
            self.instance.close()

    def with_options(self, **options):
        if self.instance is None:
            self.instance = self.factory()
        return self.instance.with_options(**options) if hasattr(self.instance, "with_options") else self.instance


class CompatibleChatModel:
    """Direct Chat Completions protocol, with no agent framework/hidden executor."""

    def __init__(self, settings: ModelSettings, client: httpx.Client | None = None):
        self.settings = settings.model_copy(deep=True)
        parsed = urlparse(settings.base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not settings.name or not parsed.hostname:
            raise AgentError("MODEL_NOT_CONFIGURED", "请配置 FINAGENT_MODEL 和 FINAGENT_BASE_URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or (parsed.scheme != "https" and not (local and parsed.scheme == "http")):
            raise AgentError("INVALID_MODEL_URL", "模型地址必须是无凭证/查询参数的HTTPS API前缀；本机服务允许HTTP")
        if not local and not settings.api_key.get_secret_value():
            raise AgentError("MODEL_KEY_MISSING", f"请通过环境变量 {settings.api_key_env} 配置模型密钥")
        self.protocol = settings.protocol
        if self.protocol == "auto":
            self.protocol = "anthropic_messages" if parsed.path.rstrip("/").endswith(("/anthropic", "/messages")) else "chat_completions"
        if settings.tool_loading == "deferred" and self.protocol != "anthropic_messages":
            raise AgentError("MODEL_TOOL_LOADING_UNSUPPORTED", "deferred工具加载需要支持tool_reference的Anthropic Messages接口；普通接口请设置tool_loading=eager")
        self.client = client or httpx.Client(follow_redirects=False, proxy=settings.proxy_url)

    def close(self):
        self.client.close()

    def with_options(self, **options):
        """Request-specific profile; keep the main settings and transport intact."""
        return CompatibleChatModel(self.settings.model_copy(update=options, deep=True), client=self.client)

    def complete(self, messages, tools, timeout):
        if self.protocol == "anthropic_messages":
            return self._anthropic_complete(messages, tools, timeout)
        headers = {"Content-Type": "application/json"}
        key = self.settings.api_key.get_secret_value()
        if key:
            headers["Authorization"] = "Bearer " + key
        body = {"model": self.settings.name, "messages": messages, "tools": tools,
                self.settings.token_parameter: self.settings.max_output_tokens, "stream": False}
        if self.settings.thinking is not None:
            body["thinking"] = {"type": self.settings.thinking}
        try:
            response = self.client.post(self.settings.base_url.rstrip("/") + "/chat/completions",
                                        headers=headers, json=body, timeout=timeout)
        except httpx.HTTPError as exc:
            raise AgentError("MODEL_UNAVAILABLE", f"模型请求失败（{type(exc).__name__}）", True) from exc
        if response.status_code != 200:
            if response.status_code in (400, 413) and any(word in response.text.lower() for word in ("context_length", "prompt is too long", "maximum context", "prompt too long")):
                raise AgentError("CONTEXT_LENGTH", "上下文超过模型窗口，可压缩后重试", True)
            raise AgentError("MODEL_HTTP_ERROR", f"模型返回 HTTP {response.status_code}", response.status_code in (429, 500, 502, 503, 504))
        try:
            body = response.json()
            choice = body["choices"][0]
            message = choice["message"]
            if message.get("role") != "assistant" or not isinstance(message.get("content"), (str, type(None))):
                raise ValueError("Invalid assistant message")
            calls = []
            for item in message.get("tool_calls") or []:
                if item.get("type") != "function":
                    raise ValueError("Unsupported call type")
                call = ToolCall(item["id"], item["function"]["name"], item["function"]["arguments"])
                if not all(isinstance(v, str) for v in (call.id, call.name, call.arguments)) or not call.id or not call.name:
                    raise ValueError("Invalid call")
                calls.append(call)
            if len({c.id for c in calls}) != len(calls):
                raise ValueError("Duplicate call ID")
            # Keep provider-specific assistant fields (e.g. reasoning_content) for
            # compatible services that require them in subsequent requests.
            return ModelReply(message=message, calls=calls, finish_reason=choice["finish_reason"], usage=body.get("usage") or {})
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise AgentError("MODEL_PROTOCOL_ERROR", "模型响应不符合工具调用协议") from exc

    def _anthropic_complete(self, messages, tools, timeout):
        system, converted = [], []
        for message in messages:
            role = message["role"]
            if role == "system":
                system.append(message["content"])
                continue
            if role == "tool":
                content = message["content"]
                references = message.get("_tool_references", [])
                if references:
                    known = {t["function"]["name"] for t in tools if t.get("type") == "function"}
                    if any(not isinstance(ref, dict) or ref.get("type") != "tool_reference"
                           or ref.get("tool_name") not in known for ref in references):
                        raise AgentError("MODEL_PROTOCOL_ERROR", "工具发现结果引用未登记的工具")
                    content = [{"type": "text", "text": content}, *deepcopy(references)]
                block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": content}
                if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                    converted[-1]["content"].append(block)
                else:
                    converted.append({"role": "user", "content": [block]})
                continue
            if role == "assistant":
                blocks = deepcopy(message.get("_anthropic_content"))
                if blocks is None:
                    blocks = [{"type": "text", "text": message["content"]}] if message.get("content") else []
                    for call in message.get("tool_calls") or []:
                        blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                                       "input": json.loads(call["function"]["arguments"])})
                converted.append({"role": "assistant", "content": blocks})
            else:
                converted.append({"role": "user", "content": message["content"]})
        definitions = []
        for tool in tools:
            if tool.get("type", "").startswith("tool_search_tool_"):
                definitions.append(deepcopy(tool))
                continue
            function = tool["function"]
            definition = {"name": function["name"], "description": function["description"],
                          "input_schema": function["parameters"]}
            for key in ("defer_loading", "cache_control", "strict"):
                if key in tool:
                    definition[key] = deepcopy(tool[key])
            definitions.append(definition)
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        key = self.settings.api_key.get_secret_value()
        if key:
            headers.update({"x-api-key": key, "Authorization": "Bearer " + key})
        prefix = self.settings.base_url.rstrip("/")
        endpoint = prefix if prefix.endswith("/messages") else prefix + ("/messages" if prefix.endswith("/v1") else "/v1/messages")
        request_body = {"model": self.settings.name, "max_tokens": self.settings.max_output_tokens,
                        "system": "\n\n".join(system), "messages": converted, "tools": definitions, "stream": False}
        if self.settings.thinking is not None:
            request_body["thinking"] = {"type": self.settings.thinking}
        if self.settings.prompt_caching:
            request_body["cache_control"] = {"type": "ephemeral"}
        try:
            response = self.client.post(endpoint, headers=headers, timeout=timeout,
                                        json=request_body)
        except httpx.HTTPError as exc:
            raise AgentError("MODEL_UNAVAILABLE", f"模型请求失败（{type(exc).__name__}）", True) from exc
        if response.status_code != 200:
            if response.status_code in (400, 413) and any(word in response.text.lower() for word in ("prompt is too long", "prompt too long", "maximum context")):
                raise AgentError("CONTEXT_LENGTH", "上下文超过模型窗口，可压缩后重试", True)
            raise AgentError("MODEL_HTTP_ERROR", f"模型返回 HTTP {response.status_code}", response.status_code in (429, 500, 502, 503, 504))
        try:
            body = response.json()
            if body["role"] != "assistant" or not isinstance(body["content"], list):
                raise ValueError("Invalid assistant message")
            calls, text = [], []
            for block in body["content"]:
                if block["type"] == "text":
                    text.append(block["text"])
                elif block["type"] == "tool_use":
                    if not isinstance(block["input"], dict):
                        raise ValueError("Invalid tool input")
                    call = ToolCall(block["id"], block["name"], json.dumps(block["input"], ensure_ascii=False, allow_nan=False))
                    if not isinstance(call.id, str) or not call.id or not isinstance(call.name, str) or not call.name:
                        raise ValueError("Invalid tool ID/name")
                    calls.append(call)
                elif block["type"] == "server_tool_use":
                    if (not isinstance(block.get("id"), str) or not block["id"]
                            or not isinstance(block.get("name"), str) or not isinstance(block.get("input"), dict)):
                        raise ValueError("Invalid server tool use")
                elif block["type"] == "tool_search_tool_result":
                    content = block.get("content")
                    if not isinstance(block.get("tool_use_id"), str) or not isinstance(content, dict):
                        raise ValueError("Invalid tool search result")
                    if content.get("type") == "tool_search_tool_search_result":
                        refs = content.get("tool_references")
                        known = {t["name"] for t in definitions if "input_schema" in t}
                        if not isinstance(refs, list) or any(not isinstance(ref, dict)
                                or ref.get("type") != "tool_reference" or ref.get("tool_name") not in known for ref in refs):
                            raise ValueError("Invalid tool reference")
                    elif content.get("type") != "tool_search_tool_result_error":
                        raise ValueError("Invalid tool search result type")
                elif block["type"] not in ("thinking", "redacted_thinking"):
                    raise ValueError("Unsupported content block")
            if len({c.id for c in calls}) != len(calls):
                raise ValueError("Duplicate call ID")
            reasons = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length", "refusal": "content_filter", "pause_turn": "pause"}
            message = {"role": "assistant", "content": "\n".join(text) or None, "_anthropic_content": body["content"]}
            if calls:
                message["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls]
            usage = body.get("usage") or {}
            # Cache tokens also consume context and must be counted when supplied.
            prompt = usage.get("input_tokens")
            if isinstance(prompt, int):
                prompt += sum(usage.get(k, 0) or 0 for k in ("cache_creation_input_tokens", "cache_read_input_tokens"))
            return ModelReply(message, calls, reasons[body["stop_reason"]],
                              {**usage, "prompt_tokens": prompt, "completion_tokens": usage.get("output_tokens")})
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise AgentError("MODEL_PROTOCOL_ERROR", "模型响应不符合 Messages 工具调用协议") from exc
