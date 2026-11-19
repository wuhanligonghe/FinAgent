"""Unified s01/s15 model-tool-observation kernel. No finance intent routing."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import json
from threading import RLock
from time import monotonic

from ..errors import AgentError
from .hooks import Hooks


@dataclass
class KernelResult:
    answer: str = ""
    status: str = "completed"
    reason: str = ""
    model_calls: int = 0
    tool_calls: int = 0
    error_retryable: bool = False


class Ledger:
    """Shared operation budget for a foreground request and all its children."""
    def __init__(self, limits):
        self.limits, self.started = limits, monotonic()
        self.counts = Counter()
        self.lock = RLock()
        self.usage = Counter()

    def charge(self, kind, usage=None):
        with self.lock:
            cap = {"model": self.limits.model_calls, "tool": self.limits.tool_calls,
                   "source": self.limits.source_requests}.get(kind)
            if cap is not None and self.counts[kind] >= cap:
                raise AgentError(kind.upper() + "_BUDGET", f"{kind}调用预算耗尽")
            self.counts[kind] += 1
            for key, value in (usage or {}).items():
                if isinstance(value, int):
                    self.usage[key] += value

    def remaining(self):
        value = self.limits.wall_seconds - (monotonic() - self.started)
        if value <= 0:
            raise AgentError("TIME_BUDGET", "活动执行时间预算耗尽")
        return value

    def pause(self, seconds):
        self.started += seconds


class AgentKernel:
    """A single loop reused by sessions, isolated workers and research roles.

    Runtime supplies messages/context, tools, persistence and stop policy; the
    kernel decides only protocol/lifecycle order, never domain workflow.
    """
    def __init__(self, model, runtime, hooks=None):
        self.model, self.runtime = model, runtime
        self.hooks = hooks or Hooks()

    @staticmethod
    def validate(reply, messages, additional_ids=()):
        used = {c["id"] for m in messages for c in m.get("tool_calls", [])}
        used.update(additional_ids)
        ids = [c.id for c in reply.calls]
        if not all(ids) or len(ids) != len(set(ids)) or used.intersection(ids):
            raise AgentError("MODEL_PROTOCOL_ERROR", "工具调用ID为空或重复")
        if reply.message.get("role") != "assistant":
            raise AgentError("MODEL_PROTOCOL_ERROR", "缺少assistant响应")
        expected = [(c.id, c.name, c.arguments) for c in reply.calls]
        try:
            actual = [(c["id"], c["function"]["name"], c["function"]["arguments"])
                      for c in reply.message.get("tool_calls") or []]
        except (KeyError, TypeError) as exc:
            raise AgentError("MODEL_PROTOCOL_ERROR", "无效工具协议") from exc
        if expected != actual or reply.finish_reason not in {"stop", "tool_calls", "length", "content_filter", "pause"}:
            raise AgentError("MODEL_PROTOCOL_ERROR", "工具协议或结束原因不一致")
        if reply.finish_reason == "tool_calls" and not reply.calls:
            raise AgentError("MODEL_PROTOCOL_ERROR", "工具结束标志缺少调用")

    def run(self, messages, ctx, ledger=None):
        ledger = ledger or Ledger(ctx.limits)
        result = KernelResult()
        failures = deque(maxlen=12)
        retries, stop_blocks, truncations = 0, 0, 0
        pending = []
        def error(code, message="该调用未执行"):
            if hasattr(self.runtime, "error_result"):
                return self.runtime.error_result(code, message)
            return {"error": code, "message": message, "executed": False}
        paired = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
        for old in list(messages):
            for call in old.get("tool_calls", []):
                if call["id"] not in paired:
                    closed = {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(error("RUN_INTERRUPTED"), ensure_ascii=False)}
                    messages.append(closed)
                    self.runtime.message(closed)
                    paired.add(call["id"])
        try:
            while True:
                ctx.check()
                self.runtime.prepare(messages, ctx)
                ledger.charge("model")
                result.model_calls += 1
                ctx.model_attempts += 1
                self.runtime.event("model_started", attempt=result.model_calls)
                try:
                    reply = self.model.complete(self.runtime.model_messages(messages, ctx),
                                                self.runtime.schemas(ctx),
                                                min(ctx.check(), ledger.remaining(), ctx.limits.request_seconds))
                except AgentError as exc:
                    if retries < getattr(self.runtime, "model_retries", 2) and (exc.retryable or exc.code == "CONTEXT_LENGTH"):
                        retries += 1
                        self.runtime.recover(exc, messages, ctx, retries)
                        continue
                    raise
                known_ids = ctx.call_ids | set(getattr(self.runtime,"used_call_ids",lambda:[])())
                try:
                    self.validate(reply, messages, known_ids)
                except AgentError:
                    ledger.usage.update({k:v for k,v in reply.usage.items() if isinstance(v,int)})
                    self.runtime.record_usage(reply.usage)
                    self.runtime.event("model_protocol_rejected",message=reply.message,usage=reply.usage)
                    raise
                reply = getattr(self.runtime,"guard_reply",lambda reply:reply)(reply)
                ctx.call_ids.update(c.id for c in reply.calls)
                retries = 0
                ledger.usage.update({k: v for k, v in reply.usage.items() if isinstance(v, int)})
                self.runtime.record_usage(reply.usage)
                messages.append(reply.message)
                pending = list(reply.calls)
                self.runtime.message(reply.message)
                self.runtime.event("model_response", message=reply.message, usage=reply.usage,
                                   finish_reason=reply.finish_reason)
                terminal = None
                for index, call in enumerate(reply.calls):
                    if terminal or reply.finish_reason in {"length", "content_filter"}:
                        output = error("RUN_STOPPED" if terminal else "MODEL_TRUNCATED")
                    else:
                        ctx.check()
                        ledger.charge("tool")
                        result.tool_calls += 1
                        ctx.tool_attempts += 1
                        if index >= ctx.limits.tools_per_turn:
                            output = error("TURN_TOOL_LIMIT")
                        else:
                            blocked = self.hooks.trigger("PreToolUse", call=call, ctx=ctx)
                            output = ({"error": "HOOK_DENIED", "message": str(blocked)} if blocked is not None
                                      else self.runtime.execute(call, ctx, ledger))
                            self.hooks.trigger("PostToolUse", call=call, result=output, ctx=ctx)
                    try:
                        body = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str, allow_nan=False)
                    except ValueError:
                        output = error("NONFINITE_TOOL_OUTPUT", "工具结果包含不可序列化的数值")
                        body = json.dumps(output,ensure_ascii=False)
                    message = {"role": "tool", "tool_call_id": call.id, "content": body}
                    if hasattr(self.runtime, "tool_message"):
                        message = self.runtime.tool_message(call, output, message, ctx)
                    messages.append(message)
                    pending.pop(0)
                    self.runtime.message(message)
                    self.runtime.event("tool_result", name=call.name, call_id=call.id, result=output)
                    signature = self.runtime.failure_signature(call, output)
                    if signature:
                        failures.append(signature)
                        if Counter(failures)[signature] >= ctx.limits.repeated_observations:
                            terminal = "NO_PROGRESS"
                    else:
                        failures.clear()
                    self.runtime.checkpoint(messages, ctx)
                if terminal:
                    raise AgentError(terminal, "重复失败没有取得新的信息")
                if reply.finish_reason == "pause":
                    self.runtime.checkpoint(messages, ctx)
                    continue
                if reply.finish_reason == "length" and truncations < 2 and hasattr(self.runtime, "truncated"):
                    truncations += 1
                    self.runtime.truncated(messages, ctx, truncations)
                    continue
                if reply.finish_reason in {"length", "content_filter"}:
                    raise AgentError("MODEL_TRUNCATED", "模型输出截断或被过滤，未认定完成")
                clarification = self.runtime.clarify(messages, ctx, ledger)
                if clarification == "waiting":
                    decision = getattr(self.runtime,"check_answer",lambda answer,ctx:None)(ctx.clarification or "请补充信息",ctx)
                    if decision is not None:
                        stop_blocks += 1
                        if stop_blocks > self.runtime.stop_limit:
                            raise AgentError("FACT_CHECK_FAILED", "澄清内容事实核验未通过")
                        ctx.clarification = None
                        continuation={"role":"user","content":"[宿主完成检查反馈] "+decision["reason"],"_finagent_origin":"host_feedback"}
                        messages.append(continuation)
                        self.runtime.message(continuation)
                        self.runtime.checkpoint(messages,ctx)
                        continue
                    result.answer, result.status = ctx.clarification or "请补充信息", "needs_clarification"
                    break
                if clarification == "continue":
                    continue
                if reply.calls and ctx.submitted_analysis is None:
                    continue
                answer = ((ctx.submitted_analysis or {}).get("summary") if ctx.submitted_analysis is not None
                          else reply.message.get("content")) or ""
                if not answer.strip():
                    raise AgentError("EMPTY_RESPONSE", "模型未返回可见答复")
                # Fact checking runs before optional Stop hooks, which cannot
                # override an identified conflict or publish a rejected draft.
                decision = getattr(self.runtime,"check_answer",lambda answer,ctx:None)(answer,ctx)
                if decision is None:
                    decision = self.hooks.trigger("Stop", messages=messages, ctx=ctx)
                if decision is None:
                    decision = self.runtime.stop(messages, ctx, ledger)
                if isinstance(decision, dict) and decision.get("action") == "continue":
                    stop_blocks += 1
                    if stop_blocks > self.runtime.stop_limit:
                        if decision.get("block_answer"):
                            raise AgentError("FACT_CHECK_FAILED", "事实校验修正达到续跑上限")
                        result.status, result.reason = "suspended", "Stop续跑上限"
                    else:
                        continuation = {"role": "user", "content": "[宿主完成检查反馈] " + decision["reason"],"_finagent_origin":"host_feedback"}
                        messages.append(continuation)
                        self.runtime.message(continuation)
                        continue
                elif isinstance(decision, dict):
                    result.status = decision.get("status", "completed")
                    result.reason = decision.get("reason", "")
                result.answer = answer
                break
        except (AgentError, KeyboardInterrupt) as exc:
            code = exc.code if isinstance(exc, AgentError) else "CANCELLED"
            result.status = "cancelled" if code == "CANCELLED" else "failed"
            result.reason = code + ": " + (exc.message if isinstance(exc, AgentError) else "用户取消")
            result.error_retryable = isinstance(exc, AgentError) and exc.retryable
            result.answer = self.runtime.failure_answer(code, result.reason)
            self.runtime.event("kernel_stopped", code=code, reason=result.reason)
        except Exception as exc:
            result.status, result.reason = "failed", "INTERNAL_ERROR: " + type(exc).__name__
            result.answer = self.runtime.failure_answer("INTERNAL_ERROR", result.reason)
            self.runtime.event("kernel_stopped", code="INTERNAL_ERROR", reason=type(exc).__name__)
        finally:
            for call in pending:
                message = {"role": "tool", "tool_call_id": call.id,
                           "content": json.dumps(error(code if 'code' in locals() else "RUN_INTERRUPTED"), ensure_ascii=False)}
                messages.append(message)
                self.runtime.message(message)
            self.runtime.checkpoint(messages, ctx)
        return result
