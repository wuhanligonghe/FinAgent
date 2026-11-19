"""Research artifact adapter; model/tool iteration exists only in AgentKernel."""
from __future__ import annotations

from hashlib import sha256
from importlib.resources import files
import json
from time import monotonic
from uuid import uuid4

from ..data.evidence import write_json
from ..errors import AgentError
from ..intent import requires_data
from ..provenance import package_digest
from ..runtime import ExecutionContext
from ..terminal import read_terminal_response, response_records
from .kernel import AgentKernel, Ledger


class ResearchRuntime:
    stop_limit = 5

    def __init__(self, loop, request, ctx, handler, progress):
        from ..presentation import ProgressReporter
        self.loop, self.request, self.ctx = loop, request, ctx
        self.handler, self.progress = handler, ProgressReporter(progress)
        self.require_data = (requires_data(request.goal) if request.require_data is None else request.require_data) or bool(request.required_tools)
        self.response, self.usage = None, {"prompt_tokens": 0, "completion_tokens": 0}

    def event(self, kind, **payload):
        from ..runtime import now
        item = dict(time=now().isoformat(), kind=kind, **payload)
        with (self.ctx.run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        self.progress(item)

    def message(self, message):
        pass

    def prepare(self, messages, ctx):
        if len(json.dumps(messages, ensure_ascii=False)) > ctx.limits.context_chars:
            from ..session import compact_messages
            shortened, changed = compact_messages(messages, ctx, self.loop.store)
            if changed:
                messages[:] = shortened
                self.event("context_compacted", retained_messages=len(messages))
        messages.append({"role": "system", "content": json.dumps({"runtime_state": {
            "symbol": ctx.symbol, "symbol_role": "current_focus", "scope": ctx.scope,
            "scope_constraints": ctx.scope_constraints, "queried_symbols": sorted(ctx.queried_symbols),
            "known_evidence_count": len(ctx.evidence_ids)}}, ensure_ascii=False)})

    def error_result(self, code, message):
        from ..data.contracts import error_observation
        saved = self.loop.store.save_result(error_observation(code, message))
        self.ctx.result_tools[saved.result_id] = "kernel_error"
        self.ctx.evidence_ids.add(saved.result_id)
        return saved.model_dump(mode="json")

    def model_messages(self, messages, ctx):
        return messages

    def schemas(self, ctx):
        if (self.loop.profile and "submit_analysis" in self.loop.registry.tools
                and ctx.model_attempts > ctx.limits.model_calls - ctx.submit_reserve):
            return [self.loop.registry.tools["submit_analysis"].schema()]
        return self.loop.registry.schemas()

    def execute(self, call, ctx, ledger):
        self.event("tool_started", name=call.name, call_id=call.id)
        if (self.loop.profile and "submit_analysis" in self.loop.registry.tools
                and ctx.model_attempts > ctx.limits.model_calls - ctx.submit_reserve and call.name != "submit_analysis"):
            return self.error_result("SUBMIT_NOW", "剩余预算用于提交与修正")
        saved = self.loop.store.save_result(self.loop.registry.execute(call.name, call.arguments, ctx, ledger))
        ctx.evidence_ids.add(saved.result_id)
        ctx.result_tools[saved.result_id] = call.name
        ctx.evidence_ids.update(saved.snapshot_ids)
        if saved.snapshot_id:
            ctx.evidence_ids.add(saved.snapshot_id)
        if call.name in self.loop.registry.tools and self.loop.registry.tools[call.name].perception:
            ctx.perception_results.add(saved.result_id)
        if ctx.grant_result:
            ctx.grant_result(saved.result_id, call.name)
        body = saved.model_dump(mode="json")
        if self.loop.profile and saved.data:
            from ..research.review import citation_hints
            body["citation_hints"] = citation_hints(saved)
        if len(json.dumps(body, ensure_ascii=False)) > 16000:
            body["data"] = {"preview": json.dumps(body["data"], ensure_ascii=False)[:10000],
                            "truncated": True, "read_full_result_with": saved.result_id}
        return body

    def checkpoint(self, messages, ctx):
        state = dict(run_id=ctx.run_id, state="running", goal=self.request.goal,
                     symbol=ctx.symbol, queried_symbols=sorted(ctx.queried_symbols), as_of=ctx.as_of.isoformat(),
                     required_tools=sorted(ctx.required_tools), require_data=self.require_data,
                     limits=ctx.limits.model_dump(), model_calls=ctx.model_attempts, tool_calls=ctx.tool_attempts,
                     remaining_seconds=max(0, ctx.limits.wall_seconds-(monotonic()-ctx.started)),
                     scope=ctx.scope, scope_constraints=ctx.scope_constraints, clarification=ctx.clarification,
                     conversation_history=ctx.conversation_history, source_requests=ctx.source_requests,
                     source_events=ctx.source_events, result_tools=ctx.result_tools,
                     loaded_skills=ctx.loaded_skills, cutoff_explicit=ctx.cutoff_explicit,
                     call_ids=sorted(ctx.call_ids),
                     implementation_sha256=package_digest(),
                     tool_schema_sha256=sha256(json.dumps(self.loop.registry.schemas(), sort_keys=True).encode()).hexdigest(),
                     **self.usage)
        for name in ("evidence_ids", "perception_results", "document_ids", "invalidated_result_ids"):
            state[name] = sorted(getattr(ctx, name))
        for name in ("retrieval_rounds", "calculation_attempts", "calculation_failures", "event_search_rounds"):
            state[name] = getattr(ctx, name)
        write_json(ctx.run_dir / "state.json", state)
        write_json(ctx.run_dir / "messages.json", messages)

    def record_usage(self, usage):
        for key in self.usage:
            self.usage[key] += usage.get(key, 0) or 0

    def clarify(self, messages, ctx, ledger):
        if not ctx.clarification:
            return None
        if self.handler is None:
            return "waiting"
        started = monotonic()
        answer = self.handler(ctx.clarification)
        elapsed = monotonic()-started
        ctx.started += elapsed
        ledger.pause(elapsed)
        if not answer:
            raise AgentError("CANCELLED", "用户取消澄清")
        from ..session import Conversation
        temporary = object.__new__(Conversation)
        temporary.state = {"symbol": ctx.symbol, "scope": dict(ctx.scope), "history": []}
        updated = temporary.context(answer, ctx.limits)
        if (updated.symbol != ctx.symbol or updated.scope.get("report_period") != ctx.scope.get("report_period")):
            ctx.symbol = updated.symbol
            ctx.scope, ctx.scope_constraints = updated.scope, updated.scope_constraints
            for name in ("evidence_ids", "result_tools", "perception_results", "document_ids", "invalidated_result_ids"):
                getattr(ctx, name).clear()
        if self.request.require_data is None:
            self.require_data = self.require_data or requires_data(answer)
        ctx.clarification = None
        messages.append({"role": "user", "content": answer})
        return "continue"

    def stop(self, messages, ctx, ledger):
        content = ((ctx.submitted_analysis or {}).get("summary") if ctx.submitted_analysis is not None else messages[-1].get("content"))
        self.response = read_terminal_response(content)
        if self.response.status == "needs_clarification":
            ctx.clarification = self.response.answer
            if self.clarify(messages, ctx, ledger) == "continue":
                return {"action": "continue", "reason": "用户已回答澄清，请继续"}
        records = response_records(self.response, ctx, self.loop.store, require_data=self.require_data)
        status = self.response.status
        if status == "completed" and records["completion_warnings"]:
            status = "partial"
        self.event("terminal_response", format=self.response.format, status=status,
                   completion_warnings=records["completion_warnings"])
        return {"status": status, "reason": "; ".join(w["code"] + ": " + w["message"] for w in records["completion_warnings"])}

    def failure_signature(self, call, value):
        if isinstance(value, dict) and value.get("status") in {"error", "empty"}:
            return json.dumps([call.name, call.arguments, value.get("error"), value.get("status")], sort_keys=True)
        return None

    def recover(self, exc, messages, ctx, attempt):
        self.prepare(messages, ctx)

    def failure_answer(self, code, reason):
        from ..presentation import failure_answer
        return failure_answer(code, reason=reason, has_evidence=bool(self.ctx.perception_results))


class ResearchLoop:
    """Internal API for existing role runners and legacy artifact readers."""
    def __init__(self, model, registry, store, *, profile=None):
        self.model, self.registry, self.store, self.profile = model, registry, store, profile

    def run(self, request, ctx=None, *, clarification_handler=None, progress_handler=None, _resume=None):
        from ..agent import RunResult
        if not request.goal.strip():
            raise AgentError("EMPTY_GOAL", "任务目标不能为空")
        if set(request.required_tools) - self.registry.tools.keys():
            raise AgentError("UNKNOWN_REQUIRED_TOOL", "必需工具未注册")
        ctx = ctx or ExecutionContext()
        ctx.run_id = _resume["run_id"] if _resume else uuid4().hex
        ctx.run_dir = (ctx.output_base or self.store.root / "runs") / ctx.run_id
        ctx.run_dir.mkdir(parents=True, exist_ok=bool(_resume))
        ctx.required_tools.update(request.required_tools)
        runtime = ResearchRuntime(self, request, ctx, clarification_handler, progress_handler)
        system = self.profile.system_prompt if self.profile else files("finagent").joinpath("prompts/system.md").read_text(encoding="utf-8")
        initial = {"as_of": ctx.as_of.isoformat(), "symbol": ctx.symbol, "scope": ctx.scope,
                   "scope_constraints": ctx.scope_constraints, "required_tools": sorted(ctx.required_tools), "require_data": runtime.require_data}
        if self.registry.skill_library:
            initial.update(json.loads(self.registry.skill_library.catalog_message()))
        messages = [{"role": "system", "content": system}, {"role": "system", "content": json.dumps(initial, ensure_ascii=False)}]
        if ctx.conversation_history:
            from ..session import recent_dialogue
            messages.append({"role": "user", "content": json.dumps({"conversation_history": recent_dialogue(ctx.conversation_history)}, ensure_ascii=False)})
        messages.append({"role": "user", "content": request.goal})
        if _resume:
            if _resume["implementation_sha256"] != package_digest():
                raise AgentError("RESUME_VERSION_MISMATCH", "旧记录与当前实现不兼容，可只读查看并创建新任务")
            messages = json.loads((ctx.run_dir / "messages.json").read_text(encoding="utf-8"))
        ledger = Ledger(ctx.limits)
        ledger.counts.update(model=ctx.model_attempts, tool=ctx.tool_attempts)
        runtime.event("run_resumed" if _resume else "run_started", run_id=ctx.run_id)
        outcome = AgentKernel(self.model, runtime).run(messages, ctx, ledger)
        failed = outcome.status in {"failed", "cancelled"}
        if outcome.status == "failed" and ctx.perception_results:
            outcome.status = "partial"
        response = runtime.response
        answer = response.answer if response and not failed else outcome.answer
        if response is None:
            response = read_terminal_response(answer)
        records = response_records(response, ctx, self.store, require_data=runtime.require_data)
        code = outcome.reason.split(":", 1)[0] if failed else None
        calculations = [k for k, v in ctx.result_tools.items() if v in {"execute_program", "calculate_market_metrics", "calculate_closing_review", "calculate_technical_divergence"}
                        and self.store.load_result(k).status in {"ok", "partial"}]
        result = RunResult(ctx.run_id, outcome.status, answer, outcome.reason, ctx.model_attempts, ctx.tool_attempts,
                           ctx.source_requests, round(monotonic()-ctx.started, 3), sorted(k for k in ctx.evidence_ids if k.startswith("raw_")),
                           ctx.run_dir, calculations, code, outcome.reason if code else "", outcome.error_retryable)
        state = json.loads((ctx.run_dir / "state.json").read_text())
        state.update(state=result.status, reason=result.reason, final_validation_mode="natural_language", corrections=0)
        write_json(ctx.run_dir / "state.json", state)
        body = {**vars(result), **records, "result_tools": ctx.result_tools, "final_protocol_verified": False,
                "terminal_response_validated": bool(runtime.response) and not failed, "outcome_verified": False,
                "final_validation_mode": "natural_language", "completion_warnings": records.get("completion_warnings", []),
                "model_protocol": getattr(self.model, "protocol", "synthetic"), "usage": runtime.usage,
                "cost": None,
                "response_format": runtime.response.format if runtime.response and not failed else None,
                "final_answer": {"answer": runtime.response.answer, "status": result.status} if runtime.response and not failed else None,
                "task": {"goal": request.goal, "symbol": ctx.symbol, "scope": ctx.scope,
                         "as_of": ctx.as_of.isoformat(), "require_data": runtime.require_data},
                "symbol": ctx.symbol, "queried_symbols": sorted(ctx.queried_symbols),
                "implementation_sha256": package_digest(), "tool_schema_sha256": state["tool_schema_sha256"],
                "prompt_sha256": sha256(system.encode()).hexdigest()}
        write_json(ctx.run_dir / "result.json", body)
        from ..presentation import format_sources
        report = "# FinSight\n\n" + answer + "\n"
        if runtime.require_data:
            report += "\n研究对象：" + "、".join(sorted(ctx.queried_symbols)) + "；截止时间：" + ctx.as_of.isoformat() + "\n"
        report += "\n<details>\n<summary>来源与核验记录</summary>\n\n" + format_sources(result, self.store) + "\n\n状态：" + result.status + "\n</details>\n"
        (ctx.run_dir / "report.md").write_text(report, encoding="utf-8")
        return result

    def resume(self, run_id, *, clarification_handler=None, progress_handler=None):
        from ..session import restore_context
        from ..agent import RunRequest
        state, ctx = restore_context(run_id, self.store)
        ctx.model_attempts, ctx.tool_attempts = state["model_calls"], state["tool_calls"]
        return self.run(RunRequest(state["goal"], tuple(state["required_tools"]), state["require_data"]), ctx,
                        clarification_handler=clarification_handler, progress_handler=progress_handler, _resume=state)
