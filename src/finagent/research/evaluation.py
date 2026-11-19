"""Frozen-input ablations. Executing comparisons requires model authority."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
from threading import Event

from ..data.evidence import EvidenceStore, write_json
from ..errors import AgentError
from .budget import ResearchBudget
from .contracts import ResearchRequest, ResearchScope, digest
from .coordinator import ResearchCoordinator
from .runner import AnalystRunner, DataBroker
from .store import ResearchStore

READS = DataBroker.NAMES | {"get_daily_bars"}
VARIANTS = ("single", "experts_no_review", "experts_review")


def freeze_packs(settings, source_run, destination):
    store = ResearchStore.from_settings(settings)
    scope = json.loads(store.get(source_run)["scope"])
    observations = []
    for path in store.directory(source_run).glob("tasks/*/*/loop/*/events.jsonl"):
        started = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["kind"] == "tool_started":
                try:
                    started[event["call_id"]] = json.loads(event["arguments"])
                except ValueError:
                    continue
            if event["kind"] == "tool_result" and event["name"] in READS and event["call_id"] in started:
                observations.append({"tool": event["name"], "arguments": started[event["call_id"]], "result_id": event["result"]["result_id"]})
    destination = Path(destination).resolve()
    if (destination / "packs.json").exists():
        raise AgentError("EVALUATION_EXISTS", "不能覆盖已冻结的输入")
    destination.mkdir(parents=True, exist_ok=True)
    for directory in ("evidence", "results", "library", "policies", "signals"):
        source = settings.data_dir / directory
        if source.exists():
            for path in source.iterdir():
                if path.is_file() and path.suffix in (".json", ".bin"):
                    target = destination / "input" / directory / path.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
    hashes = {str(p.relative_to(destination)): sha256(p.read_bytes()).hexdigest() for p in (destination / "input").rglob("*") if p.is_file()}
    cases = [("normal", [], scope["as_of"]), ("missing_policy", ["read_policy_document", "search_policy_documents"], scope["as_of"]),
             ("missing_sentiment", ["get_fund_flow", "get_market_attention"], scope["as_of"]), ("historical", [], "2025-05-08T23:59:59+08:00"),
             ("cross_dimension", [], scope["as_of"]), ("news_unavailable", ["get_stock_news", "get_macro_news"], scope["as_of"])]
    live_source_verified = any(json.loads(p.read_text(encoding="utf-8")).get("result", {}).get("id") == source_run
                               for p in (settings.data_dir / "phase2_validation").glob("model_*.json"))
    packs = []
    for name, missing, cutoff in cases:
        request = {k: v for k, v in scope.items() if k not in ("scope_hash", "market", "benchmarks")}
        request.update(as_of=cutoff)
        if name == "cross_dimension":
            request["goal"] = "对固定资料进行五维研究，说明分歧、时间尺度和覆盖。"
        packs.append({"name": name, "request": request, "excluded_tools": missing, "recorded_live_run": live_source_verified,
                      "data_provenance": "recorded_tool_observations_with_explicit_failure_injection"})
    manifest = {"schema_version": "1", "source_research_id": source_run, "observations": observations, "files": hashes, "packs": packs,
                "note": "仅冻结输入，不是已执行对照；不预设金融观点存在冲突"}
    manifest["input_hash"] = digest(manifest)
    write_json(destination / "packs.json", manifest)
    return manifest


class FrozenPack:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = json.loads((self.root / "packs.json").read_text(encoding="utf-8"))
        if digest({k: v for k, v in self.manifest.items() if k != "input_hash"}) != self.manifest["input_hash"]:
            raise AgentError("EVALUATION_INPUT_CHANGED", "冻结清单已改变")
        for relative, checksum in self.manifest["files"].items():
            path = (self.root / relative).resolve()
            if not path.is_relative_to(self.root) or not path.is_file() or sha256(path.read_bytes()).hexdigest() != checksum:
                raise AgentError("EVALUATION_INPUT_CHANGED", "冻结文件缺失或改变")

    def prepare(self, target):
        target = Path(target).resolve()
        if target.exists():
            raise AgentError("EVALUATION_EXISTS", "评估目录已存在，不能覆盖")
        shutil.copytree(self.root / "input", target)

    def registry(self, registry, case):
        source = EvidenceStore(self.root / "input")
        indexed = {(item["tool"], json.dumps(item["arguments"], sort_keys=True)): item["result_id"] for item in self.manifest["observations"]}
        for name in READS | set(case["excluded_tools"]):
            if name not in registry.tools:
                continue
            spec = registry.tools[name]
            def replay(args, ctx, name=name, argument_type=spec.arguments):
                if name in case["excluded_tools"]:
                    raise AgentError("EVALUATION_INJECTED_FAILURE", "固定案例移除此来源")
                result_id = next((result for (tool, body), result in indexed.items() if tool == name and argument_type.model_validate_json(body) == args), None)
                if not result_id:
                    raise AgentError("FROZEN_COVERAGE_MISSING", "冻结资料没有该请求参数，不访问网络")
                value = source.load_result(result_id).model_copy(deep=True)
                from .review import check_result
                check_result(value, ctx)
                if value.data.get("scope") and name == "get_market_data":
                    ctx.scope.update(value.data["scope"])
                value.warnings.append("固定真实观测回放，非在线获取")
                return value
            registry.tools[name] = replace(spec, handler=replay)


def make_runner(settings, store, budget, scope, pack, case):
    runner = AnalystRunner(settings, store, budget, scope)
    runner.registry_transform = lambda registry: pack.registry(registry, case)
    return runner


def run_variant(settings, pack, case, variant, *, runner_factory=None):
    if variant not in VARIANTS:
        raise ValueError("未知对照配置")
    request = ResearchRequest.model_validate(case["request"])
    factory = runner_factory or (lambda settings, store, budget, scope: make_runner(settings, store, budget, scope, pack, case))
    if variant != "single":
        return ResearchCoordinator(settings, runner_factory=factory, evaluation_no_review=variant == "experts_no_review").run(request)
    coordinator = ResearchCoordinator(settings)
    scope = ResearchScope(**request.model_dump())
    run = coordinator.store.create(scope, settings.research, coordinator.fingerprint())
    owner = coordinator.store.acquire(run, coordinator.fingerprint())
    budget = ResearchBudget(coordinator.store, run, owner, settings.research, Event())
    runner = factory(settings, coordinator.store, budget, scope)
    try:
        attempt = coordinator.store.begin_task(run, owner, "single", "single", "baseline")
        artifact = runner.run("single", "single", attempt)
        coordinator.store.end_task(run, owner, "single", attempt, "completed", artifact)
        directory = coordinator.store.directory(run)
        path = directory / "report.md"
        path.write_text(artifact["report_markdown"], encoding="utf-8")
        write_json(directory / "report.json", {"evaluation_variant": "single", "artifacts": [artifact], "scope": scope.model_dump(mode="json"),
                   "content_status": artifact["content_status"], "outcome_verified": False, "budget": coordinator.store.usage(run)})
        coordinator.store.finish(run, owner, "completed", artifact["content_status"], "unavailable", str(path))
    except Exception as exc:
        coordinator.store.finish(run, owner, "failed", "unavailable", "unavailable", reason=exc.code if isinstance(exc, AgentError) else type(exc).__name__)
    return coordinator.store.snapshot(run)


def summarize_runs(records):
    summary, annotations = [], []
    for variant, state in records:
        if state["report"]:
            body = json.loads((Path(state["report"]).parent / "report.json").read_text(encoding="utf-8"))
            for artifact in body.get("artifacts", []):
                for claim in artifact.get("claims", []):
                    annotations.append({"variant": variant, "research_id": state["id"], "task_id": artifact["task_id"], "claim": claim,
                                        "human_support": None, "critical_error": None, "note": ""})
        summary.append({"variant": variant, "research_id": state["id"], "execution_status": state["status"], "content_status": state["content"],
                        "active_seconds": state["active_used"], "usage": state["budget"], "report": state["report"]})
    return {"runs": summary, "human_annotations": annotations, "quality_status": "pending_human_review", "support_rate": None,
            "note": "从完整主张队列按冻结规则抽样；未标注不算正确"}
