"""Archived document-library implementation for frozen offline benchmarks only.

No production tool, CLI command or Web endpoint registers this implementation.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from pydantic import Field, field_validator, model_validator

from ..config import StrictModel
from ..data.contracts import Observation
from ..data.evidence import EvidenceStore, write_json
from ..data.symbols import normalize_symbol
from ..errors import AgentError
from ..runtime import TZ, now

METRICS = {
    "revenue": ["营业收入", "营收", "收入", "revenue"],
    "net_profit": ["净利润", "全部净利润", "net profit"],
    "attributable_net_profit": ["归母净利润", "归属于母公司股东的净利润", "归属于上市公司股东的净利润"],
    "operating_profit": ["营业利润", "operating profit"],
    "operating_cashflow": ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流净额"],
    "total_assets": ["资产总计", "总资产"],
    "total_liabilities": ["负债合计", "总负债"],
}
SCALES = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000), "百万元": Decimal(1000000),
          "亿元": Decimal(100000000), "%": Decimal(1)}


def numeric(value) -> Decimal:
    text = str(value).strip().replace(",", "").replace("，", "").replace("−", "-")
    if text.startswith(("(", "（")) and text.endswith((")", "）")):
        text = "-" + text[1:-1]
    text = text.removesuffix("%")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise AgentError("INVALID_NUMBER", "原文不是可确认的数值") from exc
    if not result.is_finite() or abs(result) > Decimal("1e40"):
        raise AgentError("INVALID_NUMBER", "数值超出有限执行范围")
    return result


class FactAnnotation(StrictModel):
    metric: Literal["revenue", "net_profit", "attributable_net_profit", "operating_profit", "operating_cashflow", "total_assets", "total_liabilities"]
    locator: str = Field(description="实际 table:N/row:R/col:C 或 page:N/table:T/row:R/col:C，均从1开始")
    label_locator: str
    period_locator: str
    unit_locator: str
    scope_locator: str
    currency_locator: str | None = None
    period_start: date
    period_end: date
    unit: Literal["元", "千元", "万元", "百万元", "亿元"]
    currency: Literal["CNY", "USD", "HKD"]
    scope: Literal["consolidated", "parent"]
    basis: Literal["annual", "ytd", "quarter", "instant"]
    original_label: str = Field(min_length=1)

    @model_validator(mode="after")
    def periods(self):
        if self.period_start > self.period_end:
            raise ValueError("无效期间")
        if self.basis == "instant" and self.period_start != self.period_end:
            raise ValueError("时点指标必须使用同一日期")
        if self.basis == "annual" and (self.period_start.month, self.period_start.day, self.period_end.month, self.period_end.day) != (1, 1, 12, 31):
            raise ValueError("当前沪深A股年度流量期间必须为1月1日至12月31日")
        if self.basis in ("annual", "ytd", "quarter") and self.period_start.year != self.period_end.year:
            raise ValueError("期间不能跨会计年度")
        if self.basis == "ytd" and (self.period_start.month, self.period_start.day) != (1, 1):
            raise ValueError("年初累计必须始于1月1日")
        return self


class DocumentImport(StrictModel):
    symbol: str
    title: str = Field(min_length=1, max_length=300)
    kind: Literal["annual_report", "interim_report", "quarterly_report", "announcement"]
    source_url: str
    publisher: str = Field(min_length=1)
    source_type: Literal["official", "regulatory", "third_party"] = "regulatory"
    published_at: str = Field(description="带时区的ISO时间，或YYYY-MM-DD")
    publication_trusted: bool = False
    version: str = Field(min_length=1)
    report_period: date | None = None
    event_key: str | None = Field(None, max_length=200, description="原始公告标识；转载使用相同值")
    event_type: str = "disclosure"
    synthetic: bool = False
    facts: list[FactAnnotation] = Field(default_factory=list, max_length=1000)

    @field_validator("symbol")
    @classmethod
    def symbol_valid(cls, value):
        return normalize_symbol(value)

    @field_validator("source_url")
    @classmethod
    def url_valid(cls, value):
        url = urlparse(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("来源必须为不含凭证/查询串的公开HTTPS定位")
        return value


def publication(value: str, trusted: bool, observed: datetime):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        stamp = datetime.combine(date.fromisoformat(value), datetime.min.time(), TZ)
        available, precision = stamp + timedelta(days=1), "date"
    else:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise AgentError("INVALID_PUBLICATION", "披露时间必须包含时区")
        available, precision = stamp, "second"
    if stamp > observed:
        raise AgentError("INVALID_PUBLICATION", "披露时间晚于导入时间")
    return stamp, precision, available if trusted else max(available, observed)


def parse_material(path: Path):
    """Preserve page/table/cell positions; no invented PDF page numbers."""
    blocks, cells = [], {}

    def table(rows, prefix):
        for r, row in enumerate(rows, 1):
            for c, value in enumerate(row, 1):
                cells[f"{prefix}/row:{r}/col:{c}"] = str(value or "").strip()
        blocks.append({"locator": prefix, "text": "\n".join(" | ".join(str(v or "") for v in row) for row in rows), "rows": rows})

    if path.suffix.lower() in (".html", ".htm"):
        soup = BeautifulSoup(path.read_text(encoding="utf-8-sig"), "html.parser")
        for hidden in soup(["script", "style"]):
            hidden.decompose()
        for i, item in enumerate(soup.find_all("table"), 1):
            # Row/column spans require manual reconstruction, not guessed columns.
            if item.select('[rowspan]:not([rowspan="1"]), [colspan]:not([colspan="1"])'):
                blocks.append({"locator": f"table:{i}", "text": item.get_text(" ", strip=True), "pending": "merged cells"})
            else:
                table([[v.get_text(" ", strip=True) for v in row.find_all(["td", "th"])] for row in item.find_all("tr")], f"table:{i}")
        text = soup.get_text("\n", strip=True)
    elif path.suffix.lower() == ".pdf":
        try:
            import pdfplumber
        except ImportError as exc:
            raise AgentError("PDF_DEPENDENCY", "请安装 finagent[pdf] 以读取文本PDF和表格") from exc
        with pdfplumber.open(path) as pdf:
            for p, page in enumerate(pdf.pages, 1):
                blocks.append({"locator": f"page:{p}", "text": page.extract_text() or ""})
                for i, rows in enumerate(page.extract_tables(), 1):
                    table(rows, f"page:{p}/table:{i}")
        text = "\n".join(b["text"] for b in blocks if re.fullmatch(r"page:\d+", b["locator"]))
    elif path.suffix.lower() in (".txt", ".md"):
        text = path.read_text(encoding="utf-8-sig")
    else:
        raise AgentError("UNSUPPORTED_DOCUMENT", "只接受文本PDF、UTF-8 HTML/Markdown/TXT；不导入训练答案字段")
    for i in range(0, len(text), 1200):
        blocks.append({"locator": f"text:{i}:{min(i+1200,len(text))}", "text": text[i:i+1200]})
    if not text.strip():
        raise AgentError("UNPARSEABLE_DOCUMENT", "未提取到文字；扫描件应先人工核验或另行OCR")
    return blocks, cells


class KnowledgeBase:
    def __init__(self, store: EvidenceStore):
        self.store, self.root = store, store.root / "library"

    def documents(self):
        if self.root.exists():
            for path in sorted(self.root.glob("doc_*.json")):
                yield json.loads(path.read_text(encoding="utf-8"))

    def load(self, key):
        if not re.fullmatch(r"doc_[a-f0-9]{64}", key):
            raise AgentError("INVALID_DOCUMENT_ID", "材料ID无效")
        path = self.root / f"{key}.json"
        if not path.is_file():
            raise AgentError("EVIDENCE_NOT_FOUND", "材料未入库")
        return json.loads(path.read_text(encoding="utf-8"))

    def ingest(self, path: Path, spec: DocumentImport):
        content = path.read_bytes()
        if len(content) > 50 * 1024 * 1024:
            raise AgentError("DOCUMENT_TOO_LARGE", "单份材料超过50MiB")
        digest = sha256(content).hexdigest()
        identity = sha256((digest + spec.model_dump_json()).encode()).hexdigest()
        key = "doc_" + identity
        if (self.root / f"{key}.json").is_file():
            return self.load(key)  # repeated import is idempotent
        cached = next((d for d in self.documents() if d["content_sha256"] == digest), None)
        blocks, cells = (cached["blocks"], cached["cells"]) if cached else parse_material(path)
        observed = now()
        stamp, precision, available = publication(spec.published_at, spec.publication_trusted, observed)
        raw = self.store.save_raw(content, {"source": spec.publisher, "url": spec.source_url,
                                           "encoding": "utf-8", "fetched_at": observed.isoformat(), "content_sha256": digest,
                                           "kind": "original_document", "format": path.suffix.lower(), "synthetic": spec.synthetic})
        facts, pending = [], []
        for i, item in enumerate(spec.facts):
            try:
                value = numeric(cells[item.locator])
                locations = {**cells, **{b["locator"]: b["text"] for b in blocks}}
                label, period, unit = (locations[getattr(item, f"{name}_locator")] for name in ("label", "period", "unit"))
                label = re.sub(r"\s", "", label)
                if re.sub(r"\s", "", item.original_label) not in label or not any(alias in label for alias in METRICS[item.metric]):
                    raise AgentError("LABEL_MISMATCH", "指标名称与原文不一致")
                if item.metric == "revenue" and ("营业收入" not in label or "营业总收入" in label):
                    raise AgentError("LABEL_MISMATCH", "营业总收入不能替代营业收入")
                if item.metric == "net_profit" and any(word in label for word in ("归属", "归母", "调整", "扣除")):
                    raise AgentError("LABEL_MISMATCH", "净利润不能混用归母/经调整/扣非口径")
                if ("合并" if item.scope == "consolidated" else "母公司") not in locations[item.scope_locator]:
                    raise AgentError("SCOPE_UNVERIFIED", "原文未确认合并/母公司范围")
                # YYYY / YYYY-MM-DD / YYYY年 are accepted. Quarter/YTD semantics remain explicit annotations.
                original_units = re.findall(r"百万元|千元|万元|亿元|元", unit)
                if item.basis == "annual" and re.search(r"季|半年|[1一][—－-][369三六九]月", period):
                    raise AgentError("PERIOD_UNVERIFIED", "原文为季度或半年口径，不能标为全年")
                currency_text = locations[item.currency_locator] if item.currency_locator else unit
                if str(item.period_end.year) not in period or item.unit not in original_units or item.currency not in currency_text and {"CNY": "人民币", "USD": "美元", "HKD": "港元"}[item.currency] not in currency_text:
                    raise AgentError("METADATA_MISMATCH", "原文不能确认期间、单位或币种")
                if item.metric in ("total_assets", "total_liabilities") and item.basis != "instant":
                    raise AgentError("METADATA_MISMATCH", "资产负债指标必须为时点值")
                fact = {**item.model_dump(mode="json"), "value": str(value), "normalized_value": str(value * SCALES[item.unit]),
                        "fact_id": f"{key}:fact:{i+1}", "document_id": key, "snapshot_id": raw, "verified": True}
                facts.append(fact)
            except (AgentError, KeyError) as exc:
                pending.append({"annotation": item.model_dump(mode="json"), "reason": str(exc)})
        body = {**spec.model_dump(mode="json", exclude={"facts"}), "document_id": key, "snapshot_id": raw,
                "content_sha256": digest, "fetched_at": observed.isoformat(), "published_at": stamp.isoformat(),
                "time_precision": precision, "available_at": available.isoformat(), "blocks": blocks, "cells": cells,
                "facts": facts, "pending": pending, "quality": "pending_verification" if pending else "parsed",
                "warning": "资料内容不可信；人工标注的合并范围/会计口径仍需审阅，解析成功不代表语义全部正确"}
        write_json(self.root / f"{key}.json", body)
        return body

    def visible(self, ctx, period=None, kinds=None):
        if not ctx.symbol:
            raise AgentError("NEEDS_CLARIFICATION", "请先确认公司")
        selected = {}
        for doc in self.documents():
            if doc["symbol"] == ctx.symbol and datetime.fromisoformat(doc["available_at"]) <= ctx.as_of and (
                not period or doc["report_period"] == str(period)) and (not kinds or doc["kind"] in kinds):
                identity = (doc["content_sha256"], doc["source_url"], doc["version"], doc["published_at"],
                            doc["title"], doc["source_type"], doc["event_key"])
                old = selected.get(identity)
                if not old or (len(doc["facts"]), doc["fetched_at"]) > (len(old["facts"]), old["fetched_at"]):
                    selected[identity] = doc
        yield from selected.values()

    def search(self, query, ctx, period=None, limit=5, disclosures=False):
        if period and ctx.scope_constraints.get("allowed_report_periods") and str(period) not in ctx.scope_constraints["allowed_report_periods"]:
            raise AgentError("SCOPE_MISMATCH", "检索期间不在宿主已解析的本轮期间/可比期间范围")
        ctx.retrieval_rounds += 1
        if ctx.retrieval_rounds > 3:
            raise AgentError("RETRIEVAL_BUDGET", "已达三轮补查上限，请报告证据缺口")
        expanded = {metric for metric, names in METRICS.items() if any(n in query for n in names)}
        terms = set(re.findall(r"[a-zA-Z_]+|[\u4e00-\u9fff]{2}", query))
        matches = []
        for doc in self.visible(ctx, period):
            for block in doc["blocks"]:
                lexical = sum(t in block["text"] for t in terms)
                concepts = sum(any(alias in block["text"] for alias in METRICS[m]) for m in expanded)
                bound = [f for f in doc["facts"] if f["locator"].startswith(block["locator"] + "/")]
                score = lexical + 3 * concepts + 10 * sum(f["metric"] in expanded for f in bound)
                if score or disclosures or not query:
                    matches.append({"document_id": doc["document_id"], "snapshot_id": doc["snapshot_id"],
                                    "locator": block["locator"], "text": block["text"][:2000], "score": score,
                                    "title": doc["title"], "version": doc["version"], "source_url": doc["source_url"],
                                    "available_at": doc["available_at"], "facts": [f for f in doc["facts"] if f["locator"].startswith(block["locator"] + "/")],
                                    "pending_count": len(doc["pending"]), "published_at": doc["published_at"]})
        matches.sort(key=lambda m: (m["score"], m["published_at"]), reverse=True)
        selected = matches[:limit]
        docs = {m["document_id"] for m in selected}
        ctx.document_ids.update(docs)
        snapshots = sorted({m["snapshot_id"] for m in selected})
        ctx.evidence_ids.update(snapshots)
        return Observation(status="partial" if selected else "empty", source="local_versioned_library", symbol=ctx.symbol, market="CN_A",
                           snapshot_id=snapshots[0] if snapshots else None, snapshot_ids=snapshots,
                           data={"matches": selected, "coverage": "仅本地已导入且截止前可用材料", "retrieval_round": ctx.retrieval_rounds,
                                 "retriever": "keyword + curated financial concept synonyms (no learned embeddings)"},
                           warnings=["候选片段不是已核验结论；可读取完整表格，未核验单位的数字不能计算"])

    def read(self, key, locator, ctx):
        if key not in ctx.document_ids:
            raise AgentError("EVIDENCE_DENIED", "请先在当前任务检索该材料")
        doc = self.load(key)
        if doc["symbol"] != ctx.symbol or datetime.fromisoformat(doc["available_at"]) > ctx.as_of:
            raise AgentError("SCOPE_MISMATCH", "材料不在当前公司及截止范围内")
        blocks = [b for b in doc["blocks"] if b["locator"] == locator]
        if not blocks:
            raise AgentError("EVIDENCE_NOT_FOUND", "原文定位不存在")
        return Observation(status="partial", source=doc["publisher"], source_url=doc["source_url"],
                           fetched_at=doc["fetched_at"], snapshot_id=doc["snapshot_id"], symbol=ctx.symbol, market="CN_A",
                           data={"document_id": key, "version": doc["version"], "blocks": blocks,
                                 "facts": [f for f in doc["facts"] if f["locator"].startswith(locator + "/")],
                                 "published_at": doc["published_at"], "available_at": doc["available_at"], "pending": doc["pending"]},
                           warnings=[doc["warning"]])

    def fact(self, key, ctx):
        match = re.fullmatch(r"(doc_[a-f0-9]{64}):fact:(\d+)", key)
        if not match or match[1] not in ctx.document_ids:
            raise AgentError("EVIDENCE_DENIED", "操作数必须来自本任务检索的事实")
        doc = self.load(match[1])
        if doc["symbol"] != ctx.symbol or datetime.fromisoformat(doc["available_at"]) > ctx.as_of:
            raise AgentError("SCOPE_MISMATCH", "事实不在公司及截止时间范围")
        fact = next((f for f in doc["facts"] if f["fact_id"] == key), None)
        if not fact or numeric(doc["cells"][fact["locator"]]) != numeric(fact["value"]):
            raise AgentError("UNVERIFIED_FACT", "事实未核验或与原文单元格不符")
        if ctx.scope_constraints.get("allowed_report_periods") and fact["period_end"] not in ctx.scope_constraints["allowed_report_periods"]:
            raise AgentError("SCOPE_MISMATCH", "事实期间不属于本轮已解析报告期或允许的可比期")
        if numeric(fact["normalized_value"]) != numeric(fact["value"]) * SCALES[fact["unit"]]:
            raise AgentError("UNVERIFIED_FACT", "规范化数值与原文单位不一致")
        raw = self.store.root / "evidence" / f"{doc['snapshot_id']}.bin"
        if not raw.is_file() or sha256(raw.read_bytes()).hexdigest() != doc["content_sha256"]:
            raise AgentError("EVIDENCE_CORRUPTED", "原文快照缺失或内容摘要改变")
        return fact


def seed_documents(store):
    """Historical original-document fixtures for archived tests only."""
    library = KnowledgeBase(store)
    folder = store.root / "synthetic_inputs"
    folder.mkdir(parents=True, exist_ok=True)
    for symbol in ("600519.SH", "000858.SZ", "000568.SZ"):
        for year in (2023, 2024, 2025):
            rows = [("营业收入", "revenue", 100 + year % 3 * 10), ("净利润", "net_profit", 20),
                    ("归属于母公司股东的净利润", "attributable_net_profit", 18),
                    ("经营活动产生的现金流量净额", "operating_cashflow", 25 + year % 3 * 2)]
            heading = f"{year}年 合并报表 人民币 元"
            html = f"<p>冻结合成财报，仅用于评测。{symbol}</p><table><tr><th>项目</th><th>{heading}</th></tr>" + "".join(
                f"<tr><td>{label}</td><td>{value}</td></tr>" for label, _, value in rows) + "</table>"
            path = folder / f"{symbol}_{year}.html"
            path.write_text(html, encoding="utf-8")
            facts = [FactAnnotation(metric=metric, locator=f"table:1/row:{index}/col:2", label_locator=f"table:1/row:{index}/col:1",
                       period_locator="table:1/row:1/col:2", unit_locator="table:1/row:1/col:2", scope_locator="table:1/row:1/col:2",
                       period_start=f"{year}-01-01", period_end=f"{year}-12-31", unit="元", currency="CNY", scope="consolidated", basis="annual", original_label=label)
                     for index, (label, metric, _) in enumerate(rows, 2)]
            library.ingest(path, DocumentImport(symbol=symbol, kind="annual_report", title=f"合成{year}年度报告", source_url=f"https://example.com/synthetic/{symbol}/{year}",
                           publisher="合成评测夹具", source_type="official", published_at=f"{year+1}-04-30", publication_trusted=True,
                           version="synthetic-v1", report_period=f"{year}-12-31", synthetic=True, facts=facts))

