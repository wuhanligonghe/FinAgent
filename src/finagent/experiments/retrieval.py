"""Offline keyword vs BM25 + curated expansion. No learned embeddings."""
from collections import Counter
from math import log
import re

from ..data.contracts import Observation
from ..errors import AgentError
from .legacy_library import METRICS


EXPANSIONS = {
    "revenue": ["卖货收入", "销售进账", "主营收款规模"],
    "net_profit": ["税后赚了多少", "最终赚到的钱", "税后盈利"],
    "attributable_net_profit": ["归母盈利", "母公司股东应占盈利", "股东应占利润"],
    "operating_cashflow": ["经营活动净流入", "经营现金净流入", "经营造血", "日常经营带来的净现金"],
    "total_assets": ["资产盘子", "资产规模"], "total_liabilities": ["负债规模", "债务总盘子"],
}


def tokens(text):
    text = text.lower()
    result = re.findall(r"[a-z_]+|\d{4}|[\u4e00-\u9fff]", text)
    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        result.extend(segment[i:i+2] for i in range(len(segment)-1))
    return result


def metric_hints(query):
    occupied, found = set(), set()
    aliases = [(word, metric) for metric in METRICS for word in [*METRICS[metric], *EXPANSIONS.get(metric, [])]]
    for word, metric in sorted(aliases, key=lambda x: -len(x[0])):
        for match in re.finditer(re.escape(word.lower()), query.lower()):
            positions = set(range(match.start(), match.end()))
            if not positions & occupied:
                found.add(metric)
                occupied.update(positions)
    return found


def rank(query, candidates, strategy):
    if strategy not in {"keyword", "bm25_expansion"}:
        raise ValueError("未知检索方法")
    if strategy == "keyword":
        expanded = {m for m, names in METRICS.items() if any(n in query for n in names)}
        terms = set(re.findall(r"[a-zA-Z_]+|[\u4e00-\u9fff]{2}", query))
        scored = [(sum(t in c["text"] for t in terms) + 3 * sum(any(a in c["text"] for a in METRICS[m]) for m in expanded)
                   + 10 * sum(f["metric"] in expanded for f in c["facts"]), c) for c in candidates]
    else:
        hints = metric_hints(query)
        expanded_query = query + " " + " ".join(" ".join(METRICS[m]) for m in sorted(hints))
        query_terms = set(tokens(expanded_query))
        documents = [Counter(tokens(c["text"])) for c in candidates]
        lengths = [sum(d.values()) for d in documents]
        avg = sum(lengths) / len(lengths) if lengths else 1
        frequencies = Counter(term for doc in documents for term in doc)
        scored = []
        for candidate, doc, length in zip(candidates, documents, lengths):
            score = 0
            for term in query_terms:
                frequency = doc[term]
                if frequency:
                    idf = log(1 + (len(documents) - frequencies[term] + .5) / (frequencies[term] + .5))
                    score += idf * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * length / (avg or 1)))
            score += 4 * sum(f["metric"] in hints for f in candidate["facts"])
            scored.append((score, candidate))
    return [{**c, "score": round(float(score), 8)} for score, c in sorted(scored, key=lambda x: (-x[0], x[1]["document_id"], x[1]["locator"])) if score > 0]


def retrieve(library, query, ctx, period=None, limit=5, disclosures=False, strategy="keyword"):
    if period and ctx.scope_constraints.get("allowed_report_periods") and str(period) not in ctx.scope_constraints["allowed_report_periods"]:
        raise AgentError("SCOPE_MISMATCH", "检索期间不在已解析范围")
    ctx.retrieval_rounds += 1
    if ctx.retrieval_rounds > 3:
        raise AgentError("RETRIEVAL_BUDGET", "已达三轮补查上限")
    candidates = []
    for doc in library.visible(ctx, period):
        required_scope = ctx.scope_constraints.get("consolidation")
        if required_scope and doc["facts"] and not any(f["scope"] == required_scope for f in doc["facts"]):
            continue
        for block in doc["blocks"]:
            candidates.append({"document_id": doc["document_id"], "snapshot_id": doc["snapshot_id"], "locator": block["locator"],
                "text": block["text"], "facts": [f for f in doc["facts"] if f["locator"].startswith(block["locator"] + "/")],
                "title": doc["title"], "source_url": doc["source_url"], "available_at": doc["available_at"], "published_at": doc["published_at"]})
    selected = rank(query, candidates, strategy)[:limit]
    if disclosures and not selected:
        selected = candidates[:limit]
    ctx.document_ids.update(c["document_id"] for c in selected)
    snapshots = sorted({c["snapshot_id"] for c in selected})
    ctx.evidence_ids.update(snapshots)
    return Observation(status="partial" if selected else "empty", source="benchmark_frozen_library", symbol=ctx.symbol, market="CN_A",
        snapshot_id=snapshots[0] if snapshots else None, snapshot_ids=snapshots,
        data={"matches": selected, "retriever": strategy, "coverage": "冻结合成文档，含共同公司/期间/截止/合并范围过滤"})
