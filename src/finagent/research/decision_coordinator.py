"""Versioned sequential deliberation on top of the existing evidence Harness."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from threading import Event

from ..errors import AgentError
from ..data.evidence import EvidenceStore, write_json
from ..provenance import package_digest
from ..runtime import ExecutionContext
from .contracts import AnalysisSubmission, ReviewSubmission, digest
from .decision_contracts import DecisionScope
from .decision_runner import DecisionRunner
from .decision_reporting import export_decision
from .reporting import export_state
from .budget import ResearchBudget
from .store import ResearchStore
from .quality import research_bundle, issue_list
from .workflow import plan, validate_config, seal, ref, verify_seal, access, ROLE_TITLES, validate_output
from .review import validate_claims, coverage


class DecisionCoordinator:
    def __init__(self, settings, *, model_factory=None, service_factory=None, runner_factory=None):
        self.settings = settings
        self.store = ResearchStore.from_settings(settings)
        self.model_factory, self.service_factory, self.runner_factory = model_factory, service_factory, runner_factory

    def fingerprint(self):
        return digest({"implementation": package_digest(), "workflow": "2.1", "decision": self.settings.decision.model_dump(mode="json"),
                       "research": self.settings.research.model_dump(mode="json"), "model": self.settings.model.model_dump(mode="json")})

    def run(self, request, *, progress=None, cancelled=None, prior_usage=None, on_created=None):
        from .selection import SelectedRequest, SelectedScope, config_for, consume, bind_run
        scope = (SelectedScope if isinstance(request, SelectedRequest) else DecisionScope)(**request.model_dump())
        cfg = config_for(scope, self.settings)
        validate_config(scope, cfg)
        from .coordinator import _FRONTEND_LOCK
        if not _FRONTEND_LOCK.acquire(blocking=False):
            raise AgentError("RESEARCH_BUSY", "本进程已有前台研究")
        try:
            if isinstance(scope, SelectedScope):
                consume(self.store, scope)
            run = self.store.create(scope, cfg, self.fingerprint())
            if isinstance(scope, SelectedScope):
                bind_run(self.store, scope, run)
            if on_created:
                on_created(run, scope)
            prior_usage = dict(prior_usage or {})
            prior_usage["source"] = prior_usage.get("source", 0) + scope.resolution_source_requests
            return self._execute(run, scope, False, progress, cancelled, prior_usage)
        finally:
            _FRONTEND_LOCK.release()

    def resume(self, run, *, progress=None, cancelled=None):
        from .selection import SelectedScope, config_for
        stored = json.loads(self.store.get(run)["scope"])
        scope = (SelectedScope if stored.get("workflow_version") == "2.2" else DecisionScope).model_validate(stored)
        validate_config(scope, config_for(scope, self.settings))
        from .coordinator import _FRONTEND_LOCK
        if not _FRONTEND_LOCK.acquire(blocking=False):
            raise AgentError("RESEARCH_BUSY", "本进程已有前台研究")
        try:
            return self._execute(run, scope, True, progress, cancelled)
        finally:
            _FRONTEND_LOCK.release()

    def _check_expert(self, a, run, scope):
        verify_seal(a, scope)
        ctx = ExecutionContext(as_of=scope.as_of, symbol=scope.symbol, frozen_symbols=frozenset([scope.symbol, *scope.benchmarks]))
        ctx.scope.update(market_window=scope.market_window, indicator_windows=[5, 20, 60])
        evidence = EvidenceStore(self.settings.data_dir)
        for grant in self.store.grants(run, a["task_id"]):
            value = evidence.load_result(grant["result_id"])
            ctx.result_tools[value.result_id] = grant["tool"]
            ctx.evidence_ids.update([value.result_id, *value.snapshot_ids])
            if value.snapshot_id:
                ctx.evidence_ids.add(value.snapshot_id)
        validate_claims(AnalysisSubmission.model_validate({k: v for k, v in a.items() if k in AnalysisSubmission.model_fields}), ctx, evidence)
        return coverage(a["analyst"], ctx, evidence)

    def _execute(self, run, scope, resume, progress, cancelled, prior_usage=None):
        from .selection import config_for
        cfg = config_for(scope, self.settings)
        cancelled = cancelled or Event()
        owner = self.store.acquire(run, self.fingerprint(), resume=resume)
        budget = ResearchBudget(self.store, run, owner, cfg, cancelled)
        artifacts, errors, cache = [], [], {}
        bundle = {"gate_status": "blocked", "coverage": {}, "review_status": "unavailable", "issues": []}
        terminal, reason = "completed", ""

        def notify(text):
            if progress:
                progress(text)

        def execute(role, task, inputs=None, question="", host_body=None):
            inputs = list(inputs or [])
            previous = cache.get(task)
            retry_partial = False
            if previous and previous.get("input_artifact_refs") == [ref(a) for a in inputs]:
                # A partial first-wave output may be retried once on explicit resume.
                retry_partial = resume and role in scope.analysts and not question and previous["content_status"] != "complete" and len([t for t in self.store.tasks(run) if t["id"] == task]) < 2
                if not retry_partial:
                    self.store.stage(run, owner, task, role, "completed", "reused")
                    return previous
            if previous:
                self.store.invalidate(run, owner, task, "partial_retry" if retry_partial else "dependency_changed")
                cache.pop(task, None)
                changed = {previous["artifact_id"]}
                # Invalidate all transitive dependents now, including stages
                # that may subsequently be skipped by a new quality gate.
                for key, value in list(cache.items()):
                    if any(r["artifact_id"] in changed for r in value.get("input_artifact_refs", [])):
                        self.store.invalidate(run, owner, key)
                        changed.add(value["artifact_id"])
                        cache.pop(key, None)
            level = access(role, scope)
            for _ in range(2):
                budget.remaining(level)
                attempt = self.store.begin_task(run, owner, task, role, "followup" if question else "analyst" if role in scope.analysts else role,
                                                cfg.max_attempts, max_retries=4)
                self.store.stage(run, owner, task, role, "running")
                notify(ROLE_TITLES.get(role, role) + "：" + task)
                try:
                    if host_body is not None:
                        body = host_body
                    elif role in ROLE_TITLES:
                        body = runner.run(role, task, attempt, question=question, artifacts=inputs, bundle=bundle)
                        # Host validates injected runners too, not just the native one.
                        body = validate_output(role, body, inputs, scope, task, bundle)
                    else:
                        body = runner.run(role, task, attempt, question=question, artifacts=inputs)
                        if role == "review":
                            body = ReviewSubmission.model_validate(body).model_dump(mode="json")
                            known = {(a["task_id"], c["claim_id"]) for a in inputs for c in a.get("claims", [])}
                            if any((i["task_id"], i["claim_id"]) not in known for i in body["issues"]):
                                raise AgentError("INVALID_REVIEW_REFERENCE", "审阅问题没有对应有效主张")
                    budget.remaining(level)
                    result = seal(body, scope, run, task, role, attempt, inputs)
                    if bundle.get("bundle_hash"):
                        result["research_bundle_hash"] = bundle["bundle_hash"]
                        result["artifact_hash"] = digest({k: v for k, v in result.items() if k != "artifact_hash"})
                    if role in scope.analysts:
                        gaps = self._check_expert(result, run, scope)
                        if gaps and result["content_status"] == "complete":
                            body = {**body, "content_status": "partial", "missing": list(dict.fromkeys([*body.get("missing", []), *gaps]))}
                            result = seal(body, scope, run, task, role, attempt, inputs)
                    self.store.end_task(run, owner, task, attempt, "completed", result)
                    self.store.stage(run, owner, task, role, "completed")
                    cache[task] = result
                    return result
                except BaseException as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        cancelled.set()
                    code = exc.code if isinstance(exc, AgentError) else type(exc).__name__
                    self.store.end_task(run, owner, task, attempt, "cancelled" if cancelled.is_set() else "failed", reason=code)
                    self.store.stage(run, owner, task, role, "cancelled" if cancelled.is_set() else "failed", code)
                    if cancelled.is_set() or isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    if attempt >= 2 or code in {"RESEARCH_BUDGET", "TIME_BUDGET", "STALE_RESEARCH_OWNER"}:
                        raise
            raise AgentError("SUBMISSION_MISSING", "任务未交付")

        def optional(role, task, inputs=None, question="", host_body=None):
            try:
                return execute(role, task, inputs, question, host_body)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                if cancelled.is_set():
                    raise AgentError("CANCELLED", "研究已取消")
                code = exc.code if isinstance(exc, AgentError) else type(exc).__name__
                errors.append(task + "：" + code)
                self.store.stage(run, owner, task, role, "failed", code)
                return None

        try:
            # Initialization also owns the run. Failures here must release that
            # ownership and export a truthful state, just like task failures.
            nodes = plan(scope)
            for n in nodes:
                self.store.stage(run, owner, n["id"], n["role"], "planned")
            write_json(self.store.directory(run) / "plan.json", {"workflow_version": scope.workflow_version, "execution_mode": getattr(scope, "execution_mode", "legacy"), "scope_hash": scope.scope_hash,
                       "tasks": nodes, "optional_followups": 2, "budget": cfg.model_dump(mode="json")})
            runner = self.runner_factory(self.settings, self.store, budget, scope) if self.runner_factory else DecisionRunner(
                self.settings, self.store, budget, scope, model_factory=self.model_factory, service_factory=self.service_factory)
            # Fail closed on changed saved evidence, including when an otherwise
            # complete upstream would have been reused.
            for t in self.store.tasks(run):
                if t["status"] == "completed":
                    a = self.store.read_artifact(t)
                    verify_seal(a, scope)
                    if a.get("analyst") in scope.analysts:
                        if self._check_expert(a, run, scope) and a["content_status"] == "complete":
                            raise AgentError("ARTIFACT_COVERAGE_CHANGED", "原有完整产物不满足最低证据合同")
                    cache[t["id"]] = a
            for kind, count in (prior_usage or {}).items():
                for _ in range(count):
                    budget.reserve(kind, "parent_admission")
            from .selection import MODES
            title = MODES.get(getattr(scope, "execution_mode", ""), "完整研究与决策")
            notify(f"{title}：{scope.symbol}；期限 {scope.decision_horizon}；模型上限 {cfg.model_calls}；多空 {scope.debate_rounds} 轮、风险 {scope.risk_rounds} 轮")
            with ThreadPoolExecutor(max_workers=cfg.max_concurrent, thread_name_prefix="finagent-decision") as pool:
                futures = {pool.submit(optional, r, r): r for r in scope.analysts}
                results = {futures[f]: f.result() for f in as_completed(futures)}
            artifacts.extend(results[r] for r in scope.analysts if results[r])
            review = optional("review", "review", list(artifacts)) if artifacts else None
            if review:
                artifacts.append(review)
            review_inputs = [a for a in artifacts if a.get("analyst") in scope.analysts]
            candidates, seen = [], set()
            for i in (review or {}).get("issues", []):
                key = (i["task_id"], i["claim_id"], i.get("question"))
                if key not in seen and i.get("question") and i.get("success_condition"):
                    seen.add(key)
                    candidates.append(i)
            for index, issue in enumerate(candidates[:2]):
                source = next((a for a in review_inputs if a["task_id"] == issue["task_id"]), None)
                if source:
                    key = f"followup_{index}"
                    a = optional(source["analyst"], key, [source, review], json.dumps(issue, ensure_ascii=False))
                    if a:
                        artifacts.append(a)
            experts = [a for a in artifacts if a.get("analyst") in scope.analysts]
            required = bool((review or {}).get("issues"))
            excluded = [{"role": r, "reason": "专家未交付"} for r in scope.analysts if not results[r]]
            bundle = research_bundle(scope, experts, review, closing_required=required, excluded=excluded)
            if review and required:
                closing = optional("quality_gate", "quality_gate", list(artifacts))
            else:
                closing = optional("quality_gate", "quality_gate", list(artifacts), host_body={"summary": "宿主质量门收口；语义审阅状态单独保留", "resolutions": []})
            if closing:
                artifacts.append(closing)
            bundle = research_bundle(scope, experts, review, closing, closing_required=required, excluded=excluded)
            bundle["accepted_artifact_refs"] = [ref(a) for a in artifacts]
            bundle["bundle_hash"] = digest(bundle)
            # Freeze this bundle before any debate. Completion flags below are
            # execution context, not edits to the immutable evidence bundle.
            write_json(self.store.directory(run) / "bundles" / (bundle["bundle_hash"] + ".json"), bundle)
            write_json(self.store.directory(run) / "research_bundle.json", bundle)
            notify("质量门：" + bundle["gate_status"])
            if bundle["gate_status"] != "blocked":
                debate = [n for n in nodes if n["role"] in ("bull", "bear")]
                for n in debate:
                    a = optional(n["role"], n["id"], list(artifacts))
                    if not a:
                        break
                    artifacts.append(a)
                bundle = {**bundle, "debate_complete": len([a for a in artifacts if a["role"] in ("bull", "bear")]) == len(debate)}
                has_debate = any(a["role"] in ("bull", "bear") for a in artifacts)
                manager = optional("research_manager", "research_manager", list(artifacts)) if has_debate else None
                if manager:
                    artifacts.append(manager)
                    trader = optional("trader", "trader", list(artifacts)) if getattr(scope, "execution_mode", None) != "deep_research" else None
                    if trader:
                        artifacts.append(trader)
                        risk_nodes = [n for n in nodes if n["role"].startswith("risk_")]
                        for n in risk_nodes:
                            a = optional(n["role"], n["id"], list(artifacts))
                            if not a:
                                break
                            artifacts.append(a)
                        risks = [a for a in artifacts if a["role"].startswith("risk_")]
                        bundle = {**bundle, "risk_complete": len(risks) == len(risk_nodes)}
                        if risks:
                            final = optional("portfolio_manager", "portfolio_manager", list(artifacts))
                            if final:
                                artifacts.append(final)
        except (KeyboardInterrupt, AgentError) as exc:
            if isinstance(exc, KeyboardInterrupt) or getattr(exc, "code", "") == "CANCELLED":
                cancelled.set()
            terminal = "cancelled" if cancelled.is_set() else "failed"
            reason = "CANCELLED" if cancelled.is_set() else exc.code
            errors.append(reason)
        except BaseException:
            terminal, reason = "failed", "INTERNAL_ERROR"
            errors.append(reason)
            raise
        finally:
            try:
                for s in self.store.stages(run):
                    if s["status"] in ("planned", "running"):
                        if s["id"] in cache:
                            self.store.invalidate(run, owner, s["id"])
                        self.store.stage(run, owner, s["id"], s["role"], "cancelled" if cancelled.is_set() else "skipped",
                                         reason or ("quality_gate_blocked" if bundle["gate_status"] == "blocked" else "dependency_unavailable"))
                report, path = export_decision(self.store, run, scope, artifacts, bundle, errors)
                self.store.finish(run, owner, terminal, report["content_status"], report["review_status"], str(path), reason)
                export_state(self.store, run)
            except BaseException:
                row = self.store.get(run)
                if row["owner"] == owner:
                    self.store.finish(run, owner, "failed", "partial" if artifacts else "unavailable", "unavailable", reason="REPORT_EXPORT_FAILED")
                raise
        return self.store.snapshot(run)
