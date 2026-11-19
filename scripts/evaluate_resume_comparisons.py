"""Frozen, retrieval-only and routing comparisons for evidence-backed resume claims.

No main chat model, financial API, production user memory, or model training is
used. Candidate labels remain outside every inference request. Prepare once,
then run each component once; a changed protocol requires a new output bundle.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
from time import monotonic
from unittest.mock import patch

import httpx

from finagent import acceptance, memory_benchmark
from finagent.config import load_settings
from finagent.harness.memory import MemoryStore
from finagent.harness.memory_search import HTTPEmbedder
from finagent.harness.state import atomic_json
from finagent.intent_routing import GROUPS, IntentRouter, fallback_route, routing_context, social_rule
from finagent.provenance import package_digest


ARMS = ("bm25", "dense", "hybrid", "hybrid_rerank")
ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def append(path, value):
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def prepare(output, settings):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("冻结目录必须为空，不能覆盖历史实验")
    acceptance.validate(ROOT / "evaluation/acceptance_v2")
    memory_benchmark.validate(ROOT / "evaluation/memory_v1")
    output.mkdir(parents=True, exist_ok=True)
    for source in ("acceptance_v2", "memory_v1"):
        shutil.copytree(ROOT / "evaluation" / source, output / "frozen" / source)
    shutil.copytree(ROOT / "src/finagent", output / "frozen/implementation/finagent",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(__file__, output / "frozen/evaluate_resume_comparisons.py")
    protocol = {
        "created_at": datetime.now().astimezone().isoformat(),
        "annotation_status": "developer_candidate_pending_independent_review",
        "implementation_digest": package_digest(),
        "intent_config": settings.intent.model_dump(mode="json"),
        "memory_config": settings.memory.model_dump(mode="json"),
        "routing": {
            "primary_split": "acceptance", "secondary_split": "calibration", "repeats": 1,
            "arms": ["keyword", "qwen"],
            "keyword": "unchanged production social_rule, then fallback_route; no extra rules",
            "context": "same last three user messages; no assistant, task-state oracle or gold",
            "metric": "required <= predicted <= required union optional; genuine fallback outputs count for BOTH arms",
            "failure_policy": "exceptions/missing outputs fail; no deletion from denominator",
            "secondary_metric": "Qwen strict nonfallback match, fallback rate, supported macro F1",
            "max_scored_http_calls": 342,
        },
        "memory": {
            "primary_split": "holdout", "secondary_split": "development", "repeats": 1,
            "arms": list(ARMS), "k": [1, 3, 5],
            "method": "production MemoryStore.select; only disable keyword/dense/rerank channels per arm",
            "thresholds": "unchanged production semantic_min_score and rerank_min_score",
            "initialization": "preserve every seed field and owner; replay forget; no gold-based prefilter",
            "scope": "production user/project filtering; core returned separately and excluded from ranking metrics",
            "primary_comparison": "hybrid Recall@5 minus max(bm25, dense) Recall@5",
            "rerank_comparison": "hybrid_rerank NDCG@5 minus hybrid NDCG@5",
            "generation": "not evaluated; no fake answers, no main model calls",
            "cache": "same real vectors/query vectors reused across arms; latency is diagnostic, not comparable",
            "max_scored_http_calls": 800,
        },
        "limitations": [
            "开发者编写且作者可见的候选集，无独立审核，不是独立验收或线上效果。",
            "路由指标不是工具实际调用成功率、回答正确率或任务完成率。",
            "记忆仅测检索/排序；不代表记忆提取质量、生成正确采用率或真实用户收益。",
            "记忆池较小，留出部分仅两个合成用户/项目组，不能据此推断泛化。",
            "固定配置单次运行；不调阈值、不修改标签、不选择最高分重跑。",
        ],
    }
    write(output / "frozen/protocol.json", protocol)
    files = {str(p.relative_to(output)): digest(p) for p in sorted((output / "frozen").rglob("*")) if p.is_file()}
    write(output / "manifest.json", {"created_at": protocol["created_at"], "files": files})
    return {"prepared": str(output.resolve()), "manifest_sha256": digest(output / "manifest.json")}


def verify(output, settings=None):
    output = Path(output)
    for relative, expected in read(output / "manifest.json")["files"].items():
        path = (output / relative).resolve()
        if not path.is_relative_to(output.resolve()) or digest(path) != expected:
            raise ValueError("冻结文件摘要不匹配：" + relative)
    protocol = read(output / "frozen/protocol.json")
    if package_digest() != protocol["implementation_digest"]:
        raise ValueError("当前包源码与冻结版本不同，请使用冻结源码运行")
    if digest(__file__) != digest(output / "frozen/evaluate_resume_comparisons.py"):
        raise ValueError("实验脚本与冻结版本不同")
    if settings and (settings.intent.model_dump(mode="json") != protocol["intent_config"] or
                     settings.memory.model_dump(mode="json") != protocol["memory_config"]):
        raise ValueError("配置与冻结实验不同，不能沿用同一实验")
    return protocol


class RecordingTransport(httpx.BaseTransport):
    """Record synthetic JSON bodies and actual outputs, never auth headers."""
    def __init__(self, directory, counter, maximum):
        self.inner = httpx.HTTPTransport(trust_env=False)
        self.directory, self.counter, self.maximum = directory, counter, maximum

    def handle_request(self, request):
        if request.url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("本实验仅允许本机模型服务")
        if self.counter[0] >= self.maximum:
            raise ValueError("实验HTTP调用预算耗尽")
        self.counter[0] += 1
        index = self.counter[0]
        stem = self.directory / f"call_{index:05d}"
        write(stem.with_suffix(".request.json"), {"url": str(request.url),
              "body": json.loads(request.content) if request.content else None})
        start = monotonic()
        try:
            response = self.inner.handle_request(request)
            response.read()
            try:
                body = response.json()
            except ValueError:
                body = {"non_json": response.text[:2000]}
            write(stem.with_suffix(".response.json"), {"status_code": response.status_code,
                  "elapsed_seconds": monotonic() - start, "body": body})
            return response
        except Exception as exc:
            write(stem.with_suffix(".error.json"), {"type": type(exc).__name__, "elapsed_seconds": monotonic() - start})
            raise

    def close(self):
        self.inner.close()


@contextmanager
def record_http(directory, maximum):
    directory.mkdir(parents=True, exist_ok=True)
    original, counter = httpx.Client, [0]
    def client(*args, **kwargs):
        if kwargs.get("transport") is not None:
            raise ValueError("实验不能替换为模拟HTTP")
        kwargs["transport"] = RecordingTransport(directory, counter, maximum)
        return original(*args, **kwargs)
    with patch("httpx.Client", side_effect=client):
        yield counter


def group_metrics(predictions, labels):
    """Keyword baselines are real predictions, not automatically wrong fallbacks."""
    counts = {group: Counter() for group in GROUPS}
    matched, strict, missing, fallback = 0, 0, 0, 0
    for prediction, label in zip(predictions, labels, strict=True):
        route = prediction.get("route")
        valid = route is not None and not prediction.get("error")
        selected = set(route["tool_groups"]) if valid else set()
        required, optional = set(label["route_required"]), set(label["route_optional"])
        ok = valid and required <= selected <= required | optional
        matched += int(ok)
        missing += int(not valid)
        degraded = bool(route and route.get("fallback_reason"))
        fallback += int(degraded)
        strict += int(ok and not degraded)
        for group in GROUPS:
            counts[group]["tp"] += int(valid and group in selected and group in required)
            counts[group]["fp"] += int(group in selected and group not in required | optional)
            counts[group]["fn"] += int(group in required and group not in selected)
    supported = [2 * c["tp"] / (2 * c["tp"] + c["fp"] + c["fn"])
                 for c in counts.values() if c["tp"] + c["fn"]]
    n = len(labels)
    return {"planned_turns": n, "matched": matched, "set_match": matched / n if n else None,
            "strict_nonfallback_matched": strict, "missing_or_errors": missing, "fallbacks": fallback,
            "macro_f1_supported": sum(supported) / len(supported) if supported else None,
            "per_class": counts}


def route_run(output, settings):
    output = Path(output)
    protocol = verify(output, settings)
    target = output / "routing"
    target.mkdir()  # refuse even partial previous runs
    cases = rows(output / "frozen/acceptance_v2/inputs.jsonl")
    router = IntentRouter(settings.intent)
    predictions = target / "predictions.jsonl"
    with record_http(target / "warmup_requests", 4):
        with httpx.Client(trust_env=False) as client:
            tags = client.get(settings.intent.endpoint.rstrip("/") + "/api/tags", timeout=10)
            tags.raise_for_status()
            write(target / "service_models.json", tags.json())
        warm = router.classifier.classify("贵州茅台最近股价是多少", timeout=settings.intent.timeout_seconds)
        write(target / "warmup.json", warm.model_dump(mode="json"))
    with record_http(target / "requests", protocol["routing"]["max_scored_http_calls"]) as calls:
        for ci, case in enumerate(cases, 1):
            history = []
            for index, turn in enumerate(case["turns"]):
                context = routing_context(history)
                for arm in ("keyword", "qwen"):
                    start, error, route = monotonic(), None, None
                    try:
                        value = ((social_rule(turn["user"]) or fallback_route(turn["user"], context)) if arm == "keyword"
                                 else router.route(turn["user"], context))
                        route = value.model_dump(mode="json")
                    except Exception as exc:
                        error = type(exc).__name__
                    append(predictions, {"id": case["id"], "turn": index, "split": case["split"], "arm": arm,
                           "input": turn["user"], "context": context, "route": route, "error": error,
                           "elapsed_seconds": monotonic() - start})
                history.append({"role": "user", "content": turn["user"]})
            if ci % 20 == 0 or ci == len(cases):
                print(f"routing {ci}/{len(cases)} dialogues; HTTP requests={calls[0]}", flush=True)
    write(target / "completion.json", {"scored_http_calls": calls[0], "predictions_sha256": digest(predictions)})
    return {"routing_complete": True, "scored_http_calls": calls[0]}


class CaseEmbedder:
    """Reuse real embeddings within one case; never inspect relevance labels."""
    def __init__(self, settings):
        self.inner = HTTPEmbedder(settings)
        self.fingerprint = self.inner.fingerprint
        self.cached = {}

    def query_text(self, request):
        return self.inner.query_text(request)

    def document_text(self, record):
        return self.inner.document_text(record)

    def embed(self, texts, timeout):
        missing = list(dict.fromkeys(text for text in texts if text not in self.cached))
        if missing:
            vectors = self.inner.embed(missing, timeout)
            if len(vectors) != len(missing):
                raise ValueError("Embedding返回数量错误")
            self.cached.update(zip(missing, vectors, strict=True))
        return [self.cached[text] for text in texts]


def seed_store(root, case, memories, settings, embedder=None):
    """Seed all corpus members under their actual owners, then replay deletion."""
    scope = case["scope"]
    config = settings.model_copy(update={"user_id": scope["user_id"], "project_id": scope["project_id"]})
    store = MemoryStore(root, settings=config, embedder=embedder)
    for key in case["corpus_ids"]:
        record = memories[key]
        directory = Path(root) / "users" / record["user_id"]
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / (record["id"] + ".json"), {**record, "scope": "persistent"})
    store.rebuild()
    for event in case["setup_events"]:
        if event["operation"] != "forget":
            raise ValueError("未知初始化事件")
        store.forget(event["memory_id"])
    return store


def memory_run(output, settings):
    output = Path(output)
    protocol = verify(output, settings)
    target = output / "memory"
    target.mkdir()
    cases = rows(output / "frozen/memory_v1/inputs.jsonl")
    memories = {r["id"]: r for r in rows(output / "frozen/memory_v1/memories.jsonl")}
    with record_http(target / "warmup_requests", 4):
        with httpx.Client(trust_env=False) as client:
            tags = client.get(settings.memory.embedding_endpoint.rstrip("/") + "/api/tags", timeout=10)
            tags.raise_for_status()
            health = client.get(settings.memory.rerank_endpoint.rstrip("/").removesuffix("/v1") + "/health", timeout=10)
            health.raise_for_status()
            write(target / "service_models.json", {"embedding": tags.json(), "reranker": health.json()})
        embedder = HTTPEmbedder(settings.memory)
        vectors = embedder.embed([embedder.query_text("记忆检索预热")], timeout=60)
        from finagent.harness.memory_search import HTTPReranker
        ranked = HTTPReranker(settings.memory).rank("回答风格", [{"id": "warmup", "description": "偏好", "body": "先给结论"}], 60)
        write(target / "warmup.json", {"dimensions": len(vectors[0]), "rerank_ids": ranked})
    predictions = target / "predictions.jsonl"
    with record_http(target / "requests", protocol["memory"]["max_scored_http_calls"]) as calls:
        for index, case in enumerate(cases, 1):
            embedder = CaseEmbedder(settings.memory)
            with tempfile.TemporaryDirectory(prefix="finagent_memory_comparison_") as temporary:
                # Seed separately per arm; vectors are reused only by the embedder.
                for arm in ARMS:
                    config = settings.memory.model_copy(update={
                        "embedding_backend": "disabled" if arm == "bm25" else settings.memory.embedding_backend,
                        "rerank": "always" if arm == "hybrid_rerank" else "off"})
                    store = seed_store(Path(temporary) / arm, case, memories, config,
                                       embedder=embedder if arm != "bm25" else None)
                    start, selected, error = monotonic(), [], None
                    try:
                        channel = patch("finagent.harness.memory.keyword_search", return_value=[]) if arm == "dense" else nullcontext()
                        with channel:
                            selected = store.select(case["query"])
                    except Exception as exc:
                        error = type(exc).__name__
                    core = set(store.last_recall.get("core_ids", []))
                    append(predictions, {"id": case["id"], "split": case["split"], "arm": arm,
                           "query": case["query"], "scope": case["scope"],
                           "core_ids": [r["id"] for r in selected if r["id"] in core],
                           "retrieved_ids": [r["id"] for r in selected if r["id"] not in core],
                           "selected_ids": [r["id"] for r in selected], "trace": store.last_recall,
                           "error": error, "elapsed_seconds": monotonic() - start})
            if index % 8 == 0 or index == len(cases):
                print(f"memory {index}/{len(cases)} queries x 4 arms; HTTP requests={calls[0]}", flush=True)
    write(target / "completion.json", {"scored_http_calls": calls[0], "predictions_sha256": digest(predictions)})
    return {"memory_complete": True, "scored_http_calls": calls[0]}


def report(output):
    output = Path(output)
    protocol = verify(output)
    route_rows, memory_rows = rows(output / "routing/predictions.jsonl"), rows(output / "memory/predictions.jsonl")
    for component in ("routing", "memory"):
        if digest(output / component / "predictions.jsonl") != read(output / component / "completion.json")["predictions_sha256"]:
            raise ValueError("原始预测已改变")
    route_cases = rows(output / "frozen/acceptance_v2/inputs.jsonl")
    route_gold = {r["id"]: r for r in rows(output / "frozen/acceptance_v2/gold.jsonl")}
    route_index = {(r["id"], r["turn"], r["arm"]): r for r in route_rows}
    if len(route_index) != len(route_rows):
        raise ValueError("重复路由预测")
    routing = {}
    for split in ("acceptance", "calibration", "all"):
        cases = [c for c in route_cases if split == "all" or c["split"] == split]
        expected = [t for c in cases for t in route_gold[c["id"]]["turns"]]
        metrics = {}
        for arm in ("keyword", "qwen"):
            predictions = [route_index.get((c["id"], i, arm), {"error": "missing"}) for c in cases for i in range(len(c["turns"]))]
            metrics[arm] = group_metrics(predictions, expected)
        metrics["delta_percentage_points"] = 100 * (metrics["qwen"]["set_match"] - metrics["keyword"]["set_match"])
        metrics["dialogues"] = len(cases)
        metrics["multiturn_dialogues"] = sum(len(c["turns"]) > 1 for c in cases)
        routing[split] = metrics
    cases, memories, gold, _ = memory_benchmark.load_dataset(output / "frozen/memory_v1")
    memory_index = {(r["id"], r["arm"]): r for r in memory_rows}
    if len(memory_index) != len(memory_rows):
        raise ValueError("重复记忆预测")
    memory_results = {}
    per_case = []
    for case in cases:
        grades = {r["memory_id"]: r["grade"] for r in gold[case["id"]]["relevance"] if not memories[r["memory_id"]]["core"]}
        deleted = {e["memory_id"] for e in case["setup_events"]}
        for arm in ARMS:
            row = memory_index.get((case["id"], arm))
            ranked = row["retrieved_ids"] if row else []
            if len(set(ranked)) != len(ranked) or any(key not in case["corpus_ids"] or memories[key]["core"] for key in ranked):
                raise ValueError("非法检索ID或常驻与排序混算")
            if row and (len(set(row["selected_ids"])) != len(row["selected_ids"]) or
                        set(row["selected_ids"]) != set(row["retrieved_ids"] + row["core_ids"])):
                raise ValueError("注入结果与检索/常驻记录不一致")
            excluded = [key for key in (row or {}).get("selected_ids", []) if key in deleted or not memory_benchmark.in_scope(memories[key], case["scope"])]
            per_case.append({"id": case["id"], "split": case["split"], "category": case["category"], "arm": arm,
                "at_k": {str(k): memory_benchmark.retrieval_metrics(ranked, grades, k) for k in (1, 3, 5)},
                "has_relevant": any(grades.values()), "returned_any": bool(ranked), "scope_or_deleted_violations": excluded,
                "missing_or_error": row is None or bool(row.get("error")), "trace": (row or {}).get("trace", {})})
    for split in ("holdout", "development", "all"):
        arm_metrics = {}
        for arm in ARMS:
            records = [r for r in per_case if r["arm"] == arm and (split == "all" or r["split"] == split)]
            negative = [r for r in records if not r["has_relevant"]]
            arm_metrics[arm] = {
                "queries": len(records), "positive_queries": sum(r["has_relevant"] for r in records),
                "at_k": {str(k): {m: memory_benchmark.mean(r["at_k"][str(k)][m] for r in records)
                                   for m in ("precision", "recall", "mrr", "ndcg")} for k in (1, 3, 5)},
                "negative_false_retrieval": sum(r["returned_any"] for r in negative), "negative_queries": len(negative),
                "scope_or_deleted_violations": sum(bool(r["scope_or_deleted_violations"]) for r in records),
                "missing_or_errors": sum(r["missing_or_error"] for r in records),
                "semantic_unavailable": sum(r["trace"].get("semantic_status") == "unavailable" for r in records),
                "rerank_fallbacks": sum(r["trace"].get("rerank_status") == "fallback" for r in records),
            }
        best = max(("bm25", "dense"), key=lambda arm: arm_metrics[arm]["at_k"]["5"]["recall"])
        arm_metrics["comparison"] = {
            "best_single_recall5_arm": best,
            "hybrid_recall5_delta_pp": 100 * (arm_metrics["hybrid"]["at_k"]["5"]["recall"] - arm_metrics[best]["at_k"]["5"]["recall"]),
            "rerank_ndcg5_delta_pp": 100 * (arm_metrics["hybrid_rerank"]["at_k"]["5"]["ndcg"] - arm_metrics["hybrid"]["at_k"]["5"]["ndcg"]),
        }
        memory_results[split] = arm_metrics
    result = {"created_at": datetime.now().astimezone().isoformat(), "manifest_sha256": digest(output / "manifest.json"),
              "routing": routing, "memory": memory_results, "limitations": protocol["limitations"],
              "independent_acceptance_passed": False, "generation_evaluated": False}
    write(output / "summary.json", result)
    write(output / "memory/per_case_scores.json", per_case)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify", "route", "memory", "report"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", default="finagent.toml")
    args = parser.parse_args()
    settings = load_settings(args.config)
    actions = {"prepare": lambda: prepare(args.output, settings), "verify": lambda: verify(args.output, settings),
               "route": lambda: route_run(args.output, settings), "memory": lambda: memory_run(args.output, settings),
               "report": lambda: report(args.output)}
    print(json.dumps(actions[args.action](), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
