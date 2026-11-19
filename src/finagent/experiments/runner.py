"""Preregistered, budgeted direct/Agent experiments.

No research, auxiliary agent, model judge or real financial network access.
Gold labels remain outside model messages. Each job has its own data directory.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import log2
from pathlib import Path
import random
import re
import shutil
from threading import Lock
from time import monotonic
from unittest.mock import patch

import httpx

from ..data.evidence import EvidenceStore
from ..data.http import SourceHTTP
from ..data.service import DataService
from ..dialogue_evaluation import evaluation_clock
from ..errors import AgentError
from ..harness.host import SessionHost
from ..harness.research import ResearchService
from .legacy_library import KnowledgeBase
from ..models import CompatibleChatModel
from ..provenance import package_digest
from ..runtime import ExecutionContext
from .fixtures import COMPANIES, digest, fixture_id, load_suite, public_pack, seed_fixture, truth_index, validate_suite
from .reporting import quantile, write_report
from .retrieval import retrieve
from .scoring import answer_contract, expected_value, grade


ARMS = ("direct", "agent")
COMMON_TOOLS = {"resolve_entity", "resolve_task_scope", "search_evidence", "get_company_disclosures", "read_document", "read_evidence", "generate_program", "execute_program", "ask_user"}


def _append(path, value):
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str) + "\n")


class ExperimentBudget:
    def __init__(self, max_calls, soft_token_limit, log_path):
        self.max_calls, self.soft_token_limit, self.log_path = max_calls, soft_token_limit, Path(log_path)
        self.calls, self.usage, self.missing_usage = 0, Counter(), 0
        self.lock = Lock()

    def snapshot(self):
        return {"model_calls": self.calls, "max_model_calls": self.max_calls, "token_usage": dict(self.usage),
                "calls_without_usage": self.missing_usage, "soft_token_limit": self.soft_token_limit,
                "research_launches": 0, "estimated_currency_cost": None}

    def wrap(self, model, job_id):
        budget = self
        class Counted:
            @property
            def settings(self):
                return getattr(model, "settings", None)

            def complete(self, messages, tools, timeout):
                with budget.lock:
                    used = budget.usage["prompt_tokens"] + budget.usage["completion_tokens"]
                    if budget.calls >= budget.max_calls or used >= budget.soft_token_limit:
                        raise AgentError("EXPERIMENT_BUDGET", "实验全局调用或响应后token预算已耗尽")
                    budget.calls += 1
                    number = budget.calls
                    request_directory = budget.log_path.parent / "requests"
                    request_directory.mkdir(exist_ok=True)
                    request_path = request_directory / f"call_{number:05d}.json"
                    request_path.write_text(json.dumps({"messages": messages, "tools": tools, "timeout": timeout},
                        ensure_ascii=False, indent=2), encoding="utf-8")
                    _append(budget.log_path, {"event": "request", "job_id": job_id, "call": number,
                        "input_file": "requests/" + request_path.name,
                        "max_output_tokens": getattr(getattr(model, "settings", None), "max_output_tokens", None)})
                started = monotonic()
                try:
                    reply = model.complete(messages, tools, timeout)
                except Exception as exc:
                    with budget.lock:
                        budget.missing_usage += 1
                    _append(budget.log_path, {"event": "failure", "job_id": job_id, "call": number,
                        "error": getattr(exc, "code", type(exc).__name__), "elapsed_seconds": monotonic() - started})
                    raise
                usage = {k: v for k, v in reply.usage.items() if isinstance(v, int) and not isinstance(v, bool)}
                with budget.lock:
                    budget.usage.update(usage)
                    if not {"prompt_tokens", "completion_tokens"} <= set(usage):
                        budget.missing_usage += 1
                _append(budget.log_path, {"event": "response", "job_id": job_id, "call": number,
                    "usage": usage, "finish_reason": reply.finish_reason, "elapsed_seconds": monotonic() - started})
                return reply

            def close(self):
                if hasattr(model, "close"):
                    model.close()
        return Counted()


def _review(suite, cases, review_file):
    records = []
    if review_file:
        body = json.loads(Path(review_file).read_text(encoding="utf-8"))
        if body.get("dataset_hash") != digest(suite):
            raise ValueError("审核文件不属于当前冻结数据集")
        records = body.get("cases", [])
    approved = set()
    for row in records:
        case = next((c for c in cases if c["id"] == row.get("case_id")), None)
        if row.get("status") not in {"pending", "approved", "needs_revision", "rejected"}:
            raise ValueError("审核状态无效")
        if case and row.get("status") in {"needs_revision", "rejected"}:
            raise ValueError("所选样本的标签已被退回，须修订并重新冻结后运行")
        if row.get("status") == "approved" and case:
            if row.get("case_hash") != digest(case) or not row.get("reviewer") or not row.get("reviewed_at"):
                raise ValueError("审核缺少匹配标签摘要、审核者或日期")
            stamp = datetime.fromisoformat(row["reviewed_at"])
            if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
                raise ValueError("审核时间必须带时区且不在未来")
            if case["id"] in approved:
                raise ValueError("重复审核记录")
            approved.add(case["id"])
    return {"approved": len(approved), "selected": len(cases), "all_approved": len(approved) == len(cases),
            "file_hash": digest(json.loads(Path(review_file).read_text())) if review_file else None,
            "status": "declared_human_approved" if len(approved) == len(cases) else "pending_human_review"}


def prepare(settings, output, *, split="development", case_ids=None, arms=ARMS, repeats=1, seed=17,
            max_calls=72, soft_token_limit=500000, live=False, review_file=None, retrieval_strategy="keyword", test_fixture=False):
    output = Path(output).resolve()
    if output.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("实验输出不能放在包源码目录内，以免源码归档递归复制")
    if output.exists() and any(output.iterdir()):
        raise ValueError("输出目录必须为空，避免覆盖或混入历史运行")
    if split not in {"development", "holdout"} or not set(arms) <= set(ARMS) or len(set(arms)) != len(arms) or not arms:
        raise ValueError("无效集合或对照方案")
    if repeats < 1 or max_calls < 1 or soft_token_limit < 1 or retrieval_strategy not in {"keyword", "bm25_expansion"}:
        raise ValueError("无效重复次数、预算或检索方法")
    suite = load_suite()
    validation = validate_suite(suite)
    cases = [c for c in suite["dialogues"] if c["split"] == split and (not case_ids or c["id"] in case_ids)]
    if not cases or case_ids and set(case_ids) - {c["id"] for c in cases}:
        raise ValueError("没有对应对话，或case不属于所选集合")
    review = _review(suite, cases, review_file)
    if live and split == "holdout" and not review["all_approved"]:
        raise ValueError("留出集真实运行需要运行前人工标签审核；先prepare --split holdout生成审核材料")
    jobs, rng = [], random.Random(seed)
    for repeat in range(repeats):
        shuffled = list(cases)
        rng.shuffle(shuffled)
        for case in shuffled:
            order = list(arms)
            rng.shuffle(order)
            for arm in order:
                jobs.append({"job_id": f"{case['id']}_r{repeat}_{arm}", "case_id": case["id"], "repeat": repeat, "arm": arm})
    manifest = dict(schema_version=2, experiment_id="experiment_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
        created_at=datetime.now(timezone.utc).isoformat(), mode="test_fixture" if test_fixture else "live" if live else "prepare", split=split,
        dataset_hash=digest(suite), dataset_version=suite["version"], dataset_validation=validation,
        implementation_digest=package_digest(), case_ids=[c["id"] for c in cases], case_hashes={c["id"]: digest(c) for c in cases},
        review=review, model=settings.model.name, provider_hash=sha256(settings.model.base_url.encode()).hexdigest(),
        protocol=settings.model.protocol, seed=seed, repeats=repeats, arms=list(arms), jobs=jobs,
        sampling={"temperature": "provider_default", "model_seed": None, "seed_role": "job_order_and_bootstrap_only"},
        retrieval_strategy=retrieval_strategy, max_model_calls=max_calls, soft_token_limit=soft_token_limit,
        clock=suite["clock"], max_output_tokens=2400, per_turn_model_calls=8, per_turn_tool_calls=14,
        request_timeout_seconds=45, per_turn_wall_seconds=150, real_research="disabled",
        contract_hash=sha256(answer_contract().encode()).hexdigest(), common_tools=sorted(COMMON_TOOLS),
        ablation="none; Task Frame removed, direct versus current Agent only",
        source_access="direct receives inline original tables; the Agent retrieves the same original documents",
        statistics="paired bootstrap on dialogue IDs, mean repeats within ID; task outcome requires all turns",
        scorer_revision="financial-v1.1-structured-and-text-audit",
        preregistered_resume_gate="complete 18-case human-reviewed holdout, >=3 repeats, 2 arms, no missing jobs")
    output.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1]
    archive = output / "implementation" / "finagent"
    shutil.copytree(source, archive, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    archive_digest = sha256()
    for file in sorted((p for p in archive.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".json", ".txt"}),
                       key=lambda p: p.relative_to(archive).as_posix()):
        archive_digest.update(file.relative_to(archive).as_posix().encode())
        archive_digest.update(b"\0")
        archive_digest.update(file.read_bytes())
    if archive_digest.hexdigest() != manifest["implementation_digest"]:
        raise ValueError("冻结源码快照与加载包摘要不一致")
    manifest["implementation_archive"] = {"path": "implementation/finagent", "sha256": archive_digest.hexdigest()}
    manifest["manifest_hash"] = digest(manifest)
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "dataset_snapshot.json").write_text(json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "output_contract.txt").write_text(answer_contract(), encoding="utf-8")
    (output / "review_template.json").write_text(json.dumps({"dataset_hash": digest(suite), "cases": [
        {"case_id": c["id"], "case_hash": digest(c), "split": c["split"], "status": "pending", "reviewer": None,
         "reviewed_at": None, "notes": "核对原话、继承、更正、口径和原始表格；审核后才填写approved。"} for c in cases]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    review_materials(output, manifest, cases)
    return manifest, cases


def review_materials(output, manifest, cases):
    specifications = {}
    for case in cases:
        for spec in case["fixtures"]:
            specifications[fixture_id(spec["symbol"], spec["year"], spec.get("scope", "consolidated"), spec.get("basis", "annual"))] = spec
    with evaluation_clock(datetime.fromisoformat(manifest["clock"])):
        documents = seed_fixture(EvidenceStore(Path(output) / "review_data"), list(specifications.values()))
    (Path(output) / "review_sources.json").write_text(json.dumps(public_pack(documents), ensure_ascii=False, indent=2), encoding="utf-8")
    expected = []
    for case in cases:
        allowed = {fixture_id(f["symbol"], f["year"], f.get("scope", "consolidated"), f.get("basis", "annual")) for f in case["fixtures"]}
        index = truth_index([d for d in documents if d["fixture_id"] in allowed])
        rows = []
        for turn in case["turns"]:
            claims = []
            for spec in turn["gold"]["claims"]:
                value, facts, unit = expected_value(spec, index)
                if value is None:
                    raise ValueError("候选答案与case可见原文不一致")
                claims.append({**spec, "value": str(value), "unit": unit, "sources": [
                    {"document_id": f["document_id"], "locator": f["locator"], "value": f["value"]} for f in facts]})
            rows.append({"user": turn["user"], "gold": turn["gold"], "expanded_claims": claims})
        expected.append({"case_id": case["id"], "case_hash": digest(case), "allowed_fixtures": sorted(allowed), "turns": rows})
    (Path(output) / "review_answers.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")


def retrieval_experiment(output, manifest, suite):
    output = Path(output)
    specifications = [{"symbol": symbol, "year": 2024} for symbol in COMPANIES]
    store = EvidenceStore(output / "retrieval_data")
    with evaluation_clock(datetime.fromisoformat(suite["clock"])):
        documents = seed_fixture(store, specifications)
        library = KnowledgeBase(store)
        queries = [q for q in suite["retrieval_queries"] if q["split"] == manifest["split"]]
        records = []
        for query in queries:
            relevant = {(d["document_id"], f["locator"].split("/row:")[0]) for d in documents if d["symbol"] == query["symbol"]
                        for f in d["facts"] if f["metric"] == query["relevant_metric"]}
            for method in ("keyword", "bm25_expansion"):
                ctx = ExecutionContext(as_of=datetime.fromisoformat(suite["clock"]))
                ctx.bind_symbol(query["symbol"])
                ctx.scope_constraints["consolidation"] = query["scope"]
                start = monotonic()
                matches = retrieve(library, query["query"], ctx, query["report_period"], 5, strategy=method).data["matches"]
                ranked = [(m["document_id"], m["locator"]) for m in matches]
                positions = [i + 1 for i, key in enumerate(ranked) if key in relevant]
                dcg = sum(1 / log2(i + 1) for i in positions)
                ideal = sum(1 / log2(i + 2) for i in range(min(5, len(relevant))))
                records.append({"query_id": query["id"], "method": method, "query": query["query"], "split": query["split"],
                    "recall_at_1": len(set(ranked[:1]) & relevant) / len(relevant),
                    "recall_at_3": len(set(ranked[:3]) & relevant) / len(relevant),
                    "recall_at_5": len(set(ranked[:5]) & relevant) / len(relevant),
                    "mrr": 1 / positions[0] if positions else 0, "ndcg_at_5": dcg / ideal if ideal else 0,
                    "elapsed_seconds": monotonic() - start, "ranked": ranked, "relevant": sorted(relevant)})
    metrics = {}
    for method in ("keyword", "bm25_expansion"):
        rows = [r for r in records if r["method"] == method]
        metrics[method] = {key: sum(r[key] for r in rows) / len(rows) for key in ("recall_at_1", "recall_at_3", "recall_at_5", "mrr", "ndcg_at_5")}
        metrics[method].update(queries=len(rows), latency_p50_seconds=quantile([r["elapsed_seconds"] for r in rows], .5))
    result = {"dataset_hash": manifest["dataset_hash"], "split": manifest["split"], "label_status": "developer_candidate",
              "methods": metrics, "records": records, "model_calls": 0,
              "limitations": ["语料和同义词均为开发阶段编写；不作为独立泛化效果。", "增强方法是稀疏BM25加规则扩展，不是向量或神经重排。",
                              "相关单元是包含目标指标的原始表格；文本副本未作为另一个正确表格计分。"]}
    (output / "retrieval.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


@contextmanager
def closed_world(strategy):
    """Patch only the isolated experiment process; default product retrieval unchanged."""
    original = DataService.__init__
    def initialize(service, settings, http=None):
        original(service, settings, http or SourceHTTP(EvidenceStore(settings.data_dir), retries=0,
            client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503, text="EXPERIMENT_EXTERNAL_DATA_DISABLED")))))
    def search(library, query, ctx, period=None, limit=5, disclosures=False):
        return retrieve(library, query, ctx, period, limit, disclosures, strategy)
    def denied(*args, **kwargs):
        raise AgentError("EXPERIMENT_RESEARCH_DISABLED", "对照实验禁止新增或恢复独立深度研究")
    from .legacy_financial import register_financial_tools
    with patch("finagent.tools.financial.register_financial_tools", register_financial_tools), \
         patch.object(DataService, "__init__", initialize), patch.object(KnowledgeBase, "search", search), \
         patch.object(ResearchService, "start", denied), patch.object(ResearchService, "resume", denied):
        yield


def _agent_context(host, arm):
    original = host.model_messages
    def messages(history, ctx):
        result = original(history, ctx)
        result[0] = {**result[0], "content": result[0]["content"] + "\n\n[实验统一输出合同]\n" + answer_contract()}
        return result
    host.model_messages = messages


def _shown_sources(messages):
    shown = set()
    for message in messages:
        if message["role"] != "tool":
            continue
        try:
            value = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        data = value.get("data", {}) if isinstance(value, dict) else {}
        for item in data.get("matches", []):
            shown.add((item["document_id"], item["locator"]))
        for item in data.get("blocks", []):
            if data.get("document_id"):
                shown.add((data["document_id"], item["locator"]))
    return shown


def _usage_delta(before, after):
    return {key: after.get(key, 0) - before.get(key, 0) for key in set(before) | set(after)}


def _job(settings, manifest, case, job, output, budget, model_factory=None):
    config = settings.model_copy(deep=True)
    anonymous_id = "bench_" + sha256(job["job_id"].encode()).hexdigest()[:24]
    root = Path(output).resolve() / "jobs" / anonymous_id
    config.data_dir, config.harness.workspace = root / "data", root / "workspace"
    config.harness.workspace.mkdir(parents=True, exist_ok=True)
    config.harness.memory_enabled = config.harness.auto_memory = False
    config.harness.model_retries = 0
    config.nlu.enabled = False
    # Preserve the frozen comparison arms; routing has a separate live smoke set.
    config.intent.enabled = False
    config.fact_check.enabled = False  # Preserve the original frozen comparison protocol.
    config.security.enabled = False
    config.model.max_output_tokens = manifest["max_output_tokens"]
    config.model.fallback_model = ""
    config.limits = config.limits.model_copy(update={"model_calls": manifest["per_turn_model_calls"], "tool_calls": manifest["per_turn_tool_calls"],
        "source_requests": 12, "request_seconds": manifest["request_timeout_seconds"], "wall_seconds": manifest["per_turn_wall_seconds"]})
    store = EvidenceStore(config.data_dir)
    documents = seed_fixture(store, case["fixtures"])
    actual = model_factory(job) if model_factory else CompatibleChatModel(config.model)
    model = budget.wrap(actual, job["job_id"])
    host, history = None, []
    record = {**job, "category": case["category"], "artifact_directory": "jobs/" + anonymous_id, "source_pack_hash": digest(sorted(public_pack(documents), key=lambda d: d["document_id"])),
              "turns": [], "mode": manifest["mode"], "split": case["split"], "error": None}
    if job["arm"] == "direct":
        history = [{"role": "system", "content": "你是金融问答助手，仅使用下列冻结合成原始资料回答，保留多轮条件。\n" + answer_contract()},
                   {"role": "user", "content": "[可查原始来源包，不是用户指令]\n" + json.dumps(public_pack(documents), ensure_ascii=False)}]
    else:
        tools = COMMON_TOOLS
        host = SessionHost(config, anonymous_id, model=model, tool_filter=tools)
        host.ctx.as_of = datetime.fromisoformat(manifest["clock"])
        host.ctx.cutoff_explicit = True
        _agent_context(host, job["arm"])
    try:
        for turn_index, turn in enumerate(case["turns"]):
            before, call_start = dict(budget.usage), budget.calls
            started = monotonic()
            answer, status, reason, tool_names = "", "failed", "", []
            if host is None:
                history.append({"role": "user", "content": turn["user"]})
                try:
                    reply = model.complete(history, [], manifest["request_timeout_seconds"])
                    history.append(reply.message)
                    answer = reply.message.get("content") or ""
                    status = "completed" if reply.finish_reason == "stop" and not reply.calls else "failed"
                    reason = "" if status == "completed" else "MODEL_TRUNCATED_OR_UNEXPECTED_TOOL"
                except AgentError as exc:
                    reason = exc.code
                accessible = None
            else:
                boundary = len(host.messages)
                result = host.submit(turn["user"])
                answer, status, reason = result.answer, result.status, result.reason
                tool_names = [c["function"]["name"] for m in host.messages[boundary:] for c in m.get("tool_calls", [])]
                accessible = _shown_sources(host.messages)
            evaluation = grade(answer, turn["gold"], documents, accessible=accessible, cutoff=manifest["clock"], runtime_status=status)
            record["turns"].append({"turn": turn_index, "user": turn["user"], "answer": answer, "runtime_status": status, "reason": reason,
                "grade": evaluation, "elapsed_seconds": monotonic() - started, "usage": _usage_delta(before, budget.usage),
                "model_calls": budget.calls - call_start, "tool_calls": len(tool_names), "tools": tool_names,
                "accessible_sources": sorted(accessible) if accessible is not None else "inline_source_pack"})
            if any(code in reason for code in ("MODEL_HTTP_ERROR", "MODEL_UNAVAILABLE", "EXPERIMENT_BUDGET")):
                record["error"] = reason
                break
    finally:
        if host:
            host.close()
        else:
            model.close()
        (root / "transcript.json").write_text(json.dumps(host.messages if host else history, ensure_ascii=False, indent=2), encoding="utf-8")
    record["dialogue_success"] = len(record["turns"]) == len(case["turns"]) and all(t["grade"]["task_success"] for t in record["turns"])
    _fill_missing_turns(record, case, documents, manifest["clock"])
    return record


def _fill_missing_turns(record, case, documents, cutoff):
    for index in range(len(record["turns"]), len(case["turns"])):
        turn = case["turns"][index]
        record["turns"].append({"turn": index, "user": turn["user"], "answer": "", "runtime_status": "not_run", "reason": record["error"] or "EARLIER_TURN_FAILED",
            "grade": grade("", turn["gold"], documents, cutoff=cutoff, runtime_status="failed"), "elapsed_seconds": 0,
            "usage": {}, "model_calls": 0, "tool_calls": 0, "tools": [], "accessible_sources": []})


def run(settings, output, *, live=False, model_factory=None, **options):
    manifest, cases = prepare(settings, output, live=live, test_fixture=model_factory is not None, **options)
    output = Path(output)
    suite = load_suite()
    if manifest["split"] == "holdout" and not (live and manifest["review"]["all_approved"]):
        retrieval = {"status": "not_run_before_review", "split": "holdout", "methods": {}, "model_calls": 0,
                     "reason": "留出集准备阶段只导出标签和原文审核材料，不查看检索效果"}
        (output / "retrieval.json").write_text(json.dumps(retrieval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        retrieval = retrieval_experiment(output, manifest, suite)
    budget = ExperimentBudget(manifest["max_model_calls"], manifest["soft_token_limit"], output / "model_calls.jsonl")
    records, stopped = [], None
    if live:
        with evaluation_clock(datetime.fromisoformat(manifest["clock"])), closed_world(manifest["retrieval_strategy"]):
            for job in manifest["jobs"]:
                case = next(c for c in cases if c["id"] == job["case_id"])
                try:
                    row = _job(settings, manifest, case, job, output, budget, model_factory)
                except (AgentError, ValueError, OSError) as exc:
                    row = {**job, "category": case["category"], "mode": manifest["mode"], "split": manifest["split"], "turns": [], "dialogue_success": False,
                           "error": getattr(exc, "code", type(exc).__name__)}
                    _fill_missing_turns(row, case, [], manifest["clock"])
                records.append(row)
                _append(output / "runs.jsonl", row)
                if row["error"] and any(code in row["error"] for code in ("MODEL_HTTP_ERROR", "MODEL_UNAVAILABLE", "EXPERIMENT_BUDGET", "MODEL_NOT_CONFIGURED", "MODEL_KEY_MISSING")):
                    stopped = row["error"]
                write_report(output, manifest, records, budget.snapshot(), retrieval, stopped)
                if stopped:
                    break
    return write_report(output, manifest, records, budget.snapshot(), retrieval, stopped)


def analyze(input_directory, output):
    """Regrade original responses against frozen labels without a model request."""
    source, output = Path(input_directory).resolve(), Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("分析输出目录必须为空；不能覆盖原始推理报告")
    manifest = json.loads((source / "manifest.json").read_text())
    suite = json.loads((source / "dataset_snapshot.json").read_text())
    original = json.loads((source / "report.json").read_text())
    if digest(suite) != manifest["dataset_hash"] or digest({k: v for k, v in manifest.items() if k != "manifest_hash"}) != manifest["manifest_hash"]:
        raise ValueError("冻结数据集或运行清单摘要不匹配")
    records = [json.loads(line) for line in (source / "runs.jsonl").read_text().splitlines() if line.strip()]
    for row in records:
        case = next(c for c in suite["dialogues"] if c["id"] == row["case_id"])
        row.setdefault("category", case["category"])
        if digest(case) != manifest["case_hashes"][row["case_id"]]:
            raise ValueError("冻结case摘要不匹配")
        if row.get("artifact_directory"):
            expected_name = "bench_" + sha256(row["job_id"].encode()).hexdigest()[:24]
            if Path(row["artifact_directory"]).name != expected_name:
                raise ValueError("源文档目录与运行ID不匹配")
            # Anchor to the supplied experiment, not the old machine's cwd/home.
            directory = (source / "jobs" / expected_name).resolve()
            if not directory.is_relative_to(source / "jobs"):
                raise ValueError("源文档路径超出原实验的jobs目录")
            library = KnowledgeBase(EvidenceStore(directory / "data"))
            documents = list(library.documents())
            if row.get("source_pack_hash") and digest(sorted(public_pack(documents), key=lambda d: d["document_id"])) != row["source_pack_hash"]:
                raise ValueError("原文包摘要不匹配")
            with evaluation_clock(datetime.fromisoformat(manifest["clock"])):
                ctx = ExecutionContext(as_of=datetime.fromisoformat(manifest["clock"]))
                for doc in documents:
                    ctx.bind_symbol(doc["symbol"])
                    ctx.document_ids.add(doc["document_id"])
                    for fact in doc["facts"]:
                        library.fact(fact["fact_id"], ctx)
        else:
            documents = []
        for turn in row["turns"]:
            visible = turn["accessible_sources"]
            accessible = None if visible == "inline_source_pack" else {tuple(item) for item in visible}
            turn["grade"] = grade(turn["answer"], case["turns"][turn["turn"]]["gold"], documents, accessible=accessible,
                                  cutoff=manifest["clock"], runtime_status=turn["runtime_status"])
        row["dialogue_success"] = len(row["turns"]) == len(case["turns"]) and all(t["grade"]["task_success"] for t in row["turns"])
    output.mkdir(parents=True, exist_ok=True)
    retrieval = json.loads((source / "retrieval.json").read_text())
    report = write_report(output, manifest, records, original["budget"], retrieval, original.get("stopped_reason"))
    analysis = {"kind": "offline_reanalysis", "source_experiment": str(source), "manifest_hash": manifest["manifest_hash"],
                "inference_implementation_digest": manifest["implementation_digest"], "analysis_implementation_digest": package_digest(),
                "additional_model_calls": 0, "real_research_launches": 0,
                "scorer_revision": "financial-v1.1-structured-and-text-audit", "posthoc_analysis": True,
                "note": "保留原始推理记录和原报告；重新评分版本单独留痕，不追改推理前清单。"}
    (output / "analysis_manifest.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in records:
        _append(output / "rescored_runs.jsonl", row)
    return report
