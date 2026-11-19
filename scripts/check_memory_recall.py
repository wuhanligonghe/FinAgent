"""Probe the configured embedding service using temporary synthetic memories."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from time import monotonic

from finagent.config import load_settings
from finagent.harness.memory import MemoryStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scores", action="store_true", help="Include all synthetic candidate cosine scores")
    parser.add_argument("--warmup", action="store_true", help="Warm the model separately with a 60-second timeout")
    parser.add_argument("--require-rerank", action="store_true", help="Require dedicated reranking on expected matching cases")
    parser.add_argument("--output", type=Path, help="Also save the synthetic check report as JSON")
    args = parser.parse_args()
    settings = load_settings().memory.model_copy(update={"user_id": "memory_probe", "project_id": "probe_a"})
    with tempfile.TemporaryDirectory(prefix="finagent_memory_probe_") as temporary:
        store = MemoryStore(Path(temporary), settings=settings)
        warmup = None
        if args.warmup and store.embedder is not None:
            started = monotonic()
            vectors = store.embedder.embed([store.embedder.query_text("记忆检索预热")], timeout=60)
            warmup = {"seconds": round(monotonic() - started, 3), "dimensions": len(vectors[0])}
        if args.warmup and store.reranker is not None:
            started = monotonic()
            store.reranker.rank("本金安全对我更重要", [{"id": "synthetic", "description": "投资风格",
                "body": "用户偏好保守配置，避免高风险资产"}], timeout=60)
            warmup = {**(warmup or {}), "rerank_seconds": round(monotonic() - started, 3)}
        core = store.write("表达偏好", "回答先给结论，再补充依据", source="synthetic_style")
        risk = store.write("投资风格", "用户投资风格保守，优先保本，避免高风险资产",
                           source="synthetic_risk", core=False)
        stock = store.write("证券线索", "600519.SH 对应贵州茅台", "reference", "synthetic_stock")
        tech = store.write("项目背景", "FinSight 使用 Python 和 FastAPI 开发财务分析助手", "project", "synthetic_tech")
        store.write("饮食偏好", "用户爱吃面食", source="synthetic_food", core=False)
        other = MemoryStore(Path(temporary), settings=settings.model_copy(update={"project_id": "probe_b"}))
        foreign = other.write("其他项目技术栈", "后端应该用什么技术栈，另一个项目用 Java", "project", "synthetic_other")
        cases = [("我承受不了太大亏损", risk["id"]), ("本金的安全对我更重要", risk["id"]),
                 ("600519.SH", stock["id"]), ("后端应该用什么技术栈", tech["id"]),
                 ("如何部署一个网页服务", None), ("法国首都是哪里", None)]
        results = []
        for query, expected in cases:
            started = monotonic()
            records = store.select(query)
            ids = {r["id"] for r in records}
            details = ids - {core["id"]}
            passed = (details == {expected} if expected else not details)
            passed = passed and core["id"] in ids and foreign["id"] not in ids
            passed = passed and store.last_recall["semantic_status"] == "ok"
            if args.require_rerank and expected:
                passed = passed and store.last_recall["rerank_status"] == "ok"
            results.append({"query": query, "passed": passed, "seconds": round(monotonic() - started, 3),
                            "descriptions": [r["description"] for r in records], "retrieval": store.last_recall})
            if args.scores and store.embedder is not None:
                candidates = [r for r in store.records() if r["id"] != core["id"]]
                descriptions = {r["id"]: r["description"] for r in candidates}
                scores = store.vectors.search(query, candidates, store.embedder,
                                              settings.embedding_timeout_seconds, -1)
                results[-1]["cosine_scores"] = [{"description": descriptions[key], "score": round(score, 4)}
                                                for key, score in scores]
        report = {"model": settings.embedding_model, "format": store.embedder.format if store.embedder else None,
                  "rerank_model": settings.rerank_model, "rerank_backend": settings.rerank_backend,
                  "rerank_mode": settings.rerank, "rerank_min_score": settings.rerank_min_score,
                  "threshold": settings.semantic_min_score, "warmup": warmup,
                  "passed": sum(r["passed"] for r in results), "total": len(results), "cases": results}
        serialized = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
        return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
