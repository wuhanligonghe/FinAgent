"""Closed-world financial documents with machine-verifiable source cells.

Amounts are fictional and intentionally differ across companies and periods.
Neither the model nor retrieval code receives gold answers or replay actions.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from hashlib import sha256
from importlib.resources import files
import json
from pathlib import Path

from .legacy_library import DocumentImport, FactAnnotation, KnowledgeBase


LABELS = {
    "revenue": "营业收入", "net_profit": "净利润", "attributable_net_profit": "归属于母公司股东的净利润",
    "operating_profit": "营业利润", "operating_cashflow": "经营活动产生的现金流量净额",
    "total_assets": "资产总计", "total_liabilities": "负债合计",
}
COMPANIES = {"600519.SH": "贵州茅台", "300750.SZ": "宁德时代", "000651.SZ": "格力电器",
             "601088.SH": "中国神华", "601012.SH": "隆基绿能", "000333.SZ": "美的集团"}


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_suite():
    return json.loads(files("finagent").joinpath("resources/business_benchmark.json").read_text(encoding="utf-8"))


def amount(symbol, year, metric, scope="consolidated", basis="annual"):
    base = Decimal(int(symbol[:6]) % 997 + 101) * Decimal("10000000")
    revenue = base + Decimal(year - 2022) * Decimal("130000000")
    factors = {"revenue": "1", "net_profit": ".13", "attributable_net_profit": ".11", "operating_profit": ".16",
               "operating_cashflow": ".19", "total_assets": "2.7", "total_liabilities": "1.2"}
    value = revenue * Decimal(factors[metric])
    if scope == "parent":
        value *= Decimal(".63")
    if basis in {"ytd", "quarter"} and metric not in {"total_assets", "total_liabilities"}:
        value *= Decimal(".72") if basis == "ytd" else Decimal(".24")
    return value.quantize(Decimal(".01"))


def fixture_id(symbol, year, scope="consolidated", basis="annual"):
    return f"{symbol}_{year}_{scope}_{basis}"


def seed_fixture(store, specifications):
    library = KnowledgeBase(store)
    folder = store.root / "benchmark_inputs"
    folder.mkdir(parents=True, exist_ok=True)
    documents = []
    for spec in specifications:
        symbol, year = spec["symbol"], spec["year"]
        scope, basis = spec.get("scope", "consolidated"), spec.get("basis", "annual")
        key = fixture_id(symbol, year, scope, basis)
        end = f"{year}-12-31" if basis == "annual" else f"{year}-09-30"
        start = f"{year}-07-01" if basis == "quarter" else f"{year}-01-01"
        heading = f"{year}年 {'全年' if basis == 'annual' else '第三季度' if basis == 'quarter' else '前三季度累计'} {'合并' if scope == 'consolidated' else '母公司'}报表 人民币 元"
        tables, facts = [], []
        for index, (metric, label) in enumerate(LABELS.items(), 1):
            value = amount(symbol, year, metric, scope, basis)
            tables.append(f"<table><tr><th>项目</th><th>{heading}</th></tr><tr><td>{label}</td><td>{value}</td></tr></table>")
            header = f"table:{index}/row:1/col:2"
            stock = metric in {"total_assets", "total_liabilities"}
            facts.append(FactAnnotation(metric=metric, locator=f"table:{index}/row:2/col:2", label_locator=f"table:{index}/row:2/col:1",
                period_locator=header, unit_locator=header, scope_locator=header, period_start=end if stock else start, period_end=end,
                unit="元", currency="CNY", scope=scope, basis="instant" if stock else basis, original_label=label))
        # Distractors are deliberately not asserted as verified financial facts.
        tables.append(f"<table><tr><th>经营讨论</th><th>{year}年</th></tr><tr><td>营业收入、净利润、现金流数据的解释须回读对应报表，不能混用金额和增长率。</td><td>参见原文科目</td></tr></table>")
        path = folder / (key + ".html")
        path.write_text(f"<p>完全合成的评测资料，不代表真实证券财报。{COMPANIES[symbol]} {symbol}</p>" + "".join(tables), encoding="utf-8")
        published = f"{year+1}-04-30" if basis == "annual" else f"{year}-10-30"
        doc = library.ingest(path, DocumentImport(symbol=symbol, title=f"合成{COMPANIES[symbol]}{year}年{scope}/{basis}报告",
            kind="annual_report" if basis == "annual" else "quarterly_report", source_url=f"https://example.com/finagent-benchmark/{key}",
            publisher="FinAgent冻结合成资料", source_type="official", published_at=published, publication_trusted=True,
            version="business-fixture-v1", report_period=end, synthetic=True, facts=facts))
        if doc["pending"] or len(doc["facts"]) != len(LABELS):
            raise ValueError("合成原文未通过真实入库校验")
        doc["fixture_id"] = key
        documents.append(doc)
    return documents


def public_pack(documents):
    """Only source content, not questions, labels, formulas or expected outputs."""
    return [{"document_id": d["document_id"], "symbol": d["symbol"], "title": d["title"], "report_period": d["report_period"],
             "available_at": d["available_at"], "source_url": d["source_url"], "blocks": [{"locator": b["locator"], "text": b["text"]} for b in d["blocks"] if b["locator"].startswith("table:")]}
            for d in documents]


def truth_index(documents):
    return {(d["symbol"], f["metric"], f["period_end"], f["scope"], f["basis"]): {**f, "symbol": d["symbol"], "available_at": d["available_at"]}
            for d in documents for f in d["facts"]}


def validate_suite(suite):
    ids, texts, split_entities = set(), {}, {"development": set(), "holdout": set()}
    groups = {"development": set(), "holdout": set()}
    for case in suite["dialogues"]:
        split = case["split"]
        if case["id"] in ids or split not in split_entities or not case["turns"]:
            raise ValueError("重复case ID、无效split或空对话")
        ids.add(case["id"])
        groups[split].add(case["group_id"])
        split_entities[split].update(f["symbol"] for f in case["fixtures"])
        for turn in case["turns"]:
            if "replay" in turn or "replay" in case:
                raise ValueError("业务测试集不能包含回放动作")
            user = turn["user"]
            if user in texts and texts[user] != split:
                raise ValueError("同一原话跨split泄漏")
            texts[user] = split
            if turn["gold"]["action"] not in {"answer", "clarify", "unavailable", "explain"}:
                raise ValueError("无效gold action")
            gold = turn["gold"]
            for claim in gold["claims"]:
                request = gold.get("request") or {}
                if (claim["symbol"] not in request.get("symbols", []) or claim["metric"] not in request.get("metrics", [])
                        or claim["period_end"] not in request.get("periods", [])):
                    raise ValueError("答案主张与请求标签的对象/指标/期间不一致")
                if claim["metric"].endswith("_yoy") and request.get("unit") != "%":
                    raise ValueError("同比增长率的请求标签必须使用百分比单位")
    if groups["development"] & groups["holdout"] or split_entities["development"] & split_entities["holdout"]:
        raise ValueError("对话组或证券跨开发/留出集泄漏")
    retrieval_text = {split: {q["query"] for q in suite["retrieval_queries"] if q["split"] == split} for split in groups}
    if retrieval_text["development"] & retrieval_text["holdout"]:
        raise ValueError("检索查询原文跨split泄漏")
    if any(c.get("real_research") for c in suite["dialogues"]):
        raise ValueError("本业务实验禁止新增真实深度研究")
    return {"dialogues": len(ids), "turns": sum(len(c["turns"]) for c in suite["dialogues"]),
            "splits": {s: sum(c["split"] == s for c in suite["dialogues"]) for s in groups}, "group_overlap": 0, "entity_overlap": 0}
