from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from threading import Event, Lock

from ..data.evidence import write_json
from ..errors import AgentError
from ..provenance import package_digest
from .budget import ResearchBudget
from .contracts import ResearchRequest, ResearchScope, digest
from .reporting import export_report, export_state
from .runner import AnalystRunner, DataBroker
from .store import ResearchStore


_FRONTEND_LOCK = Lock()


class ResearchCoordinator:
    def __init__(self, settings, *, model_factory=None, service_factory=None, runner_factory=None, evaluation_no_review=False):
        self.settings = settings
        self.store = ResearchStore.from_settings(settings)
        self.model_factory, self.service_factory, self.runner_factory = model_factory, service_factory, runner_factory
        self.evaluation_no_review = evaluation_no_review

    def fingerprint(self):
        return digest({"implementation": package_digest(), "research": self.settings.research.model_dump(mode="json"),
                       "model": self.settings.model.model_dump(mode="json"), "evaluation_no_review": self.evaluation_no_review})

    def run(self, request: ResearchRequest, *, progress=None, cancelled=None, prior_usage=None, on_created=None):
        from .decision_contracts import DecisionRequest
        if isinstance(request, DecisionRequest):
            from .decision_coordinator import DecisionCoordinator
            return DecisionCoordinator(self.settings, model_factory=self.model_factory, service_factory=self.service_factory,
                                       runner_factory=self.runner_factory).run(request, progress=progress, cancelled=cancelled, prior_usage=prior_usage, on_created=on_created)
        if not _FRONTEND_LOCK.acquire(blocking=False):
            raise AgentError("RESEARCH_BUSY", "本进程已有前台研究")
        try:
            scope = ResearchScope(**request.model_dump())
            run = self.store.create(scope, self.settings.research, self.fingerprint())
            write_json(self.store.directory(run) / "plan.json", {"schema_version": "1", "tasks": [
                {"id": r, "role": r, "depends_on": [], "acceptance": "范围/引用检查与角色最低证据合同"} for r in scope.analysts],
                "review": "after_first_wave", "max_followups": 2, "scope_hash": scope.scope_hash})
            if on_created:
                on_created(run, scope)
            prior_usage = dict(prior_usage or {})
            prior_usage["source"] = prior_usage.get("source", 0) + request.resolution_source_requests
            return self._execute(run, scope, progress, cancelled, False, prior_usage)
        finally:
            _FRONTEND_LOCK.release()

    def resume(self, run, *, progress=None, cancelled=None):
        row = self.store.get(run)
        if json.loads(row["scope"]).get("workflow_version") in ("2.1", "2.2"):
            from .decision_coordinator import DecisionCoordinator
            return DecisionCoordinator(self.settings, model_factory=self.model_factory, service_factory=self.service_factory,
                                       runner_factory=self.runner_factory).resume(run, progress=progress, cancelled=cancelled)
        cfg = self.settings.research
        calls = self.store.usage(run)["calls"]
        if calls.get("model", 0) >= cfg.model_calls or row["active_used"] >= cfg.wall_seconds:
            raise AgentError("RESEARCH_BUDGET", "研究没有可恢复预算")
        tasks = self.store.tasks(run)
        latest = {r["id"]: r for r in tasks}
        scope = ResearchScope.model_validate_json(row["scope"])
        retryable = any(r not in latest or latest[r]["status"] != "completed" or
                        (self.store.read_artifact(latest[r]) or {}).get("content_status") != "complete" for r in scope.analysts)
        if not retryable and all(stage in latest and latest[stage]["status"] == "completed" for stage in ("review", "synthesis")):
            raise AgentError("NOTHING_TO_RESUME", "没有可恢复的缺口；更新信息请新建研究")
        if len(tasks) >= cfg.max_attempts:
            raise AgentError("RESEARCH_ATTEMPT_BUDGET", "尝试额度已耗尽")
        if not _FRONTEND_LOCK.acquire(blocking=False):
            raise AgentError("RESEARCH_BUSY", "本进程已有前台研究")
        try:
            return self._execute(run, scope, progress, cancelled, True)
        finally:
            _FRONTEND_LOCK.release()

    def _execute(self, run, scope, progress, cancelled, resume, prior_usage=None):
        cancelled = cancelled or Event()
        owner = self.store.acquire(run, self.fingerprint(), resume=resume)
        budget = ResearchBudget(self.store, run, owner, self.settings.research, cancelled)
        runner = (self.runner_factory(self.settings, self.store, budget, scope) if self.runner_factory else
                  AnalystRunner(self.settings, self.store, budget, scope, model_factory=self.model_factory,
                                service_factory=self.service_factory, broker=DataBroker()))
        artifacts, errors, review, synthesis = [], [], None, None
        cached = {}
        try:
            for kind, count in (prior_usage or {}).items():
                for _ in range(count):
                    budget.reserve(kind, "parent_admission")
            for task in self.store.tasks(run):
                if task["status"] == "completed":
                    artifact = self.store.read_artifact(task)
                    if artifact:
                        if artifact.get("analyst") in scope.analysts and artifact.get("scope_hash") != scope.scope_hash:
                            raise AgentError("ARTIFACT_SCOPE_MISMATCH", "已保存产物的研究范围不匹配")
                        if artifact.get("analyst") in scope.analysts:
                            from ..runtime import ExecutionContext
                            from ..data.evidence import EvidenceStore
                            from .contracts import AnalysisSubmission
                            from .review import validate_claims
                            evidence = EvidenceStore(self.settings.data_dir)
                            check_context = ExecutionContext(as_of=scope.as_of, symbol=scope.symbol,
                                frozen_symbols=frozenset([scope.symbol, *scope.benchmarks]))
                            check_context.scope.update(market_window=scope.market_window, indicator_windows=[5, 20, 60])
                            for grant in self.store.grants(run, task["id"]):
                                value = evidence.load_result(grant["result_id"])
                                check_context.result_tools[value.result_id] = grant["tool"]
                                check_context.evidence_ids.update([value.result_id, *value.snapshot_ids])
                                if value.snapshot_id:
                                    check_context.evidence_ids.add(value.snapshot_id)
                            validate_claims(AnalysisSubmission.model_validate({k: v for k, v in artifact.items() if k in AnalysisSubmission.model_fields}), check_context, evidence)
                        cached[task["id"]] = artifact
            def notify(text):
                if progress:
                    progress(text)

            def execute(role, task, question="", inputs=None):
                budget.remaining(role in ("review", "synthesis"))
                attempt = self.store.begin_task(run, owner, task, role,
                    "followup" if question else "analyst" if role in scope.analysts else role, self.settings.research.max_attempts)
                try:
                    result = runner.run(role, task, attempt, question=question, artifacts=inputs)
                    budget.remaining(role in ("review", "synthesis"))
                    result.update(task_id=task, attempt_id=attempt)
                    self.store.end_task(run, owner, task, attempt, "completed", result)
                    return result
                except BaseException as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        cancelled.set()
                    code = exc.code if isinstance(exc, AgentError) else type(exc).__name__
                    self.store.end_task(run, owner, task, attempt, "cancelled" if cancelled.is_set() else "failed", reason=code)
                    raise

            pending = []
            for role in scope.analysts:
                previous = cached.get(role)
                # Valid completed output is reusable; partial role outputs are
                # eligible for one explicit resume attempt, never automatic loops.
                if previous and (previous.get("content_status") == "complete" or not resume):
                    artifacts.append(previous)
                else:
                    pending.append(role)
            notify("开始研究：" + scope.symbol + "；所选维度 " + "、".join(scope.analysts))
            pool = ThreadPoolExecutor(max_workers=self.settings.research.max_concurrent, thread_name_prefix="finagent-research")
            futures = {}
            try:
                for role in pending:
                    futures[pool.submit(execute, role, role)] = role
                for future in as_completed(futures):
                    role = futures[future]
                    try:
                        artifacts.append(future.result())
                        notify(role + " 分析已提交")
                    except Exception as exc:
                        detail = str(exc) if isinstance(exc, AgentError) else type(exc).__name__
                        errors.append(role + "：" + detail)
                        if role in cached:
                            artifacts.append(cached[role])
                        notify(role + " 未完整交付：" + detail)
                    if cancelled.is_set():
                        raise AgentError("CANCELLED", "研究已取消")
            except KeyboardInterrupt:
                cancelled.set()
                raise
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
            if artifacts:
                notify("正在交叉审阅证据与分歧")
                review = cached.get("review") if not pending else None
                if review is None and not self.evaluation_no_review:
                    try:
                        review = execute("review", "review", inputs=artifacts)
                    except Exception as exc:
                        errors.append("交叉审阅未完成：" + (exc.code if isinstance(exc, AgentError) else type(exc).__name__))
                if review and review.get("issues"):
                    # Followup identities persist; never schedule a second batch
                    # after recovery or after a completed first followup round.
                    scheduled = {t["id"] for t in self.store.tasks(run)}
                    candidates, seen = [], set()
                    for issue in review["issues"]:
                        key = (issue["task_id"], issue["claim_id"], issue.get("question"))
                        if key not in seen and issue.get("question") and issue.get("success_condition"):
                            seen.add(key)
                            candidates.append(issue)
                    candidates = candidates[:2]
                    for index, issue in enumerate(candidates):
                        task = f"followup_{index}"
                        source = next((a for a in artifacts if a["task_id"] == issue["task_id"]), None)
                        if not source:
                            continue
                        if task in cached:
                            artifacts.append(cached[task])
                        elif task not in scheduled:
                            try:
                                artifacts.append(execute(source["analyst"], task, question=json.dumps(issue, ensure_ascii=False), inputs=[source]))
                            except Exception as exc:
                                errors.append("补查未完成：" + (exc.code if isinstance(exc, AgentError) else type(exc).__name__))
                notify("正在生成综合报告")
                try:
                    synthesis = cached.get("synthesis") if not pending else None
                    if synthesis is None:
                        synthesis = execute("synthesis", "synthesis", inputs=artifacts)
                except Exception as exc:
                    errors.append("综合研判未完成：" + (exc.code if isinstance(exc, AgentError) else type(exc).__name__))
            budget.remaining(True)
            report, path = export_report(self.store, run, scope, artifacts, review, synthesis, errors)
            budget.remaining(True)
            if self.evaluation_no_review:
                report["evaluation_variant"] = "experts_no_review"
                write_json(path.parent / "report.json", report)
            self.store.finish(run, owner, "completed", report["content_status"], report["review_status"], str(path))
            export_state(self.store, run)
            return self.store.snapshot(run)
        except (KeyboardInterrupt, AgentError) as exc:
            is_cancel = isinstance(exc, KeyboardInterrupt) or getattr(exc, "code", "") == "CANCELLED"
            if is_cancel:
                cancelled.set()
            self.store.finish(run, owner, "cancelled" if is_cancel else "failed", "partial" if artifacts else "unavailable", "unavailable",
                              reason="CANCELLED" if is_cancel else exc.code)
            export_state(self.store, run)
            return self.store.snapshot(run)
        except BaseException:
            self.store.finish(run, owner, "failed", "partial" if artifacts else "unavailable", "unavailable", reason="INTERNAL_ERROR")
            export_state(self.store, run)
            raise
