"""Conservative common token envelope for offline-input model comparisons.

Without a provider tokenizer, UTF-8 bytes plus a protocol allowance are charged
as an upper estimate. Unknown usage keeps that charge; it is never zero.
"""
import json
from threading import Lock
from ..errors import AgentError


class TokenEnvelope:
    def __init__(self, limit):
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("比较 token 额度必须为正整数")
        self.limit, self.charged, self.pending = limit, 0, 0
        self.unknown, self.overrun, self.lock = 0, False, Lock()

    def reserve(self, messages, tools, max_output):
        estimate = len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode("utf-8")) + max_output + 4096
        with self.lock:
            if self.charged + self.pending + estimate > self.limit:
                raise AgentError("COMPARISON_TOKEN_BUDGET", "比较 token 保守额度不足，不发送新请求")
            self.pending += estimate
        return estimate

    def settle(self, estimate, usage=None):
        known = usage and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens"))
        charged = usage["prompt_tokens"] + usage["completion_tokens"] if known else estimate
        with self.lock:
            self.pending -= estimate
            self.charged += charged
            self.unknown += int(not known)
            self.overrun |= charged > estimate or self.charged + self.pending > self.limit

    def snapshot(self):
        with self.lock:
            return {"limit": self.limit, "charged": self.charged, "pending": self.pending, "unknown_requests": self.unknown,
                    "provider_estimate_overrun": self.overrun,
                    "method": "UTF8_bytes_plus_output_cap_plus_protocol_allowance; reported_usage_settlement"}


class TokenEnvelopeModel:
    def __init__(self, model, envelope, settings):
        self.model, self.envelope = model, envelope
        self.settings = getattr(model, "settings", None) or settings
        self.protocol = getattr(model, "protocol", "synthetic")

    def complete(self, messages, tools, timeout):
        estimate = self.envelope.reserve(messages, tools, self.settings.max_output_tokens)
        try:
            reply = self.model.complete(messages, tools, timeout)
        except BaseException:
            self.envelope.settle(estimate)
            raise
        self.envelope.settle(estimate, reply.usage)
        if self.envelope.snapshot()["provider_estimate_overrun"]:
            raise AgentError("COMPARISON_TOKEN_OVERRUN", "供应商实际token超出保守估计，记录偏差并停止，不视为可比预算通过")
        return reply

    def close(self):
        if hasattr(self.model, "close"):
            self.model.close()
