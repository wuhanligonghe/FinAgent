"""Operands from registered online statements, checked against their API snapshots."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from ..errors import AgentError


def numeric(value):
    text = str(value).strip().replace(",", "").replace("，", "").replace("−", "-")
    if text.startswith(("(", "（")) and text.endswith((")", "）")):
        text = "-" + text[1:-1]
    try:
        result = Decimal(text.removesuffix("%"))
    except InvalidOperation as exc:
        raise AgentError("INVALID_NUMBER", "接口字段不是可确认的数值") from exc
    if not result.is_finite() or abs(result) > Decimal("1e40"):
        raise AgentError("INVALID_NUMBER", "数值超出有限执行范围")
    return result


def attach_facts(observation):
    """Expose stable operand IDs without inventing an accounting scope."""
    statement = observation.data.get("statement")
    if statement not in {"income", "balance", "cashflow"} or not observation.snapshot_id:
        return observation
    facts = []
    for report in observation.data.get("reports", []):
        end = date.fromisoformat(report["report_period"])
        for metric, item in report.get("metrics", {}).items():
            if not item or metric == "eps":
                continue
            key = f"{observation.snapshot_id}:{statement}:{end}:{metric}"
            item["fact_id"] = key
            facts.append({"fact_id": key, "snapshot_id": observation.snapshot_id,
                "symbol": observation.symbol, "metric": metric, "locator": item["locator"],
                "raw_field": item["raw_field"], "value": str(item["raw_value"]),
                "normalized_value": str(numeric(item["raw_value"])), "unit": "元",
                "currency": report["currency"], "scope": "provider_reported",
                "basis": "instant" if statement == "balance" else "annual" if (end.month, end.day) == (12, 31) else "ytd",
                "period_start": end.isoformat() if statement == "balance" else f"{end.year}-01-01",
                "period_end": end.isoformat(), "available_at": report["available_at"],
                "verification": "provider_field_mapping", "statement": statement})
    observation.data["facts"] = facts
    return observation


class StatementFacts:
    def __init__(self, store):
        self.store = store

    def fact(self, key, ctx):
        if not re.fullmatch(r"raw_[a-f0-9]{64}:(income|balance|cashflow):\d{4}-\d{2}-\d{2}:[a-z_]+", key):
            raise AgentError("EVIDENCE_DENIED", "操作数必须使用在线三表返回的fact_id")
        for result_id, tool in ctx.result_tools.items():
            if tool != "get_financial_statements" or result_id in ctx.invalidated_result_ids:
                continue
            observation = self.store.load_result(result_id)
            if observation.stale or observation.status not in {"ok", "partial"}:
                continue
            fact = next((f for f in observation.data.get("facts", []) if f["fact_id"] == key), None)
            if fact is None:
                continue
            if observation.symbol != ctx.symbol or datetime.fromisoformat(fact["available_at"]) > ctx.as_of:
                raise AgentError("SCOPE_MISMATCH", "三表字段不在当前公司及截止时间范围内")
            if fact["snapshot_id"] != observation.snapshot_id or fact["snapshot_id"] not in ctx.evidence_ids:
                raise AgentError("EVIDENCE_DENIED", "接口快照未在本任务登记")
            if ctx.scope_constraints.get("allowed_report_periods") and fact["period_end"] not in ctx.scope_constraints["allowed_report_periods"]:
                raise AgentError("SCOPE_MISMATCH", "事实期间不属于本轮报告期或允许的可比期")
            self._verify(observation, fact)
            validate_fact_scope(fact, ctx)
            return {**fact, "result_id": result_id}
        raise AgentError("EVIDENCE_DENIED", "操作数未在当前任务查询，或结果已失效")

    def _verify(self, observation, fact):
        snapshot = fact["snapshot_id"]
        if not self.store.has_raw(snapshot):
            raise AgentError("EVIDENCE_CORRUPTED", "接口快照缺失")
        folder = self.store.root / "evidence"
        content = (folder / f"{snapshot}.bin").read_bytes()
        metadata = json.loads((folder / f"{snapshot}.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(content + json.dumps(metadata, sort_keys=True).encode()).hexdigest()
        if snapshot != "raw_" + digest:
            raise AgentError("EVIDENCE_CORRUPTED", "接口快照内容或元数据已改变")
        from urllib.parse import parse_qs, urlsplit
        from .providers.sina import SinaProvider, STATEMENTS
        from .symbols import vendor_symbol
        query = parse_qs(urlsplit(metadata.get("url", "")).query)
        if (metadata.get("source") != "sina_finance" or metadata.get("http_status") != 200
                or query.get("paperCode") != [vendor_symbol(observation.symbol)]
                or query.get("source") != [STATEMENTS[fact["statement"]]]):
            raise AgentError("UNVERIFIED_FACT", "接口快照的证券或报表类型不匹配")
        # Replay the production parser locally; this adapter never makes a request.
        class Raw:
            snapshot_id = snapshot
            fetched_at = observation.fetched_at.isoformat()
            url = observation.source_url
            source = observation.source
            def json(self):
                return json.loads(content)
        class HTTP:
            def get(self, *args, **kwargs):
                return Raw()
        from ..runtime import ExecutionContext
        replay = attach_facts(SinaProvider(HTTP()).financials(observation.symbol, fact["statement"],
            ExecutionContext(as_of=datetime.fromisoformat(fact["available_at"]))))
        original = next((f for f in replay.data.get("facts", []) if f["fact_id"] == fact["fact_id"]), None)
        if original != fact:
            raise AgentError("UNVERIFIED_FACT", "结构化三表字段与接口原始响应不一致")


def validate_fact_scope(fact, ctx):
    """Verify actual evidence semantics, not just a model's requested parameters."""
    constraints = ctx.scope_constraints
    for field, key in (("currency", "currency"), ("scope", "consolidation")):
        if constraints.get(key) and fact.get(field) != constraints[key]:
            raise AgentError("SCOPE_MISMATCH", "证据币种或合并范围与任务口径不一致")
    basis = constraints.get("financial_basis")
    allowed_basis = {"annual": {"annual"}, "cumulative": {"ytd", "annual"}, "single_quarter": {"quarter"}, "instant": {"instant"}}.get(basis)
    # Balance-sheet stocks are measured at the period end, including annual reports.
    if fact.get("metric") in {"total_assets", "total_liabilities"}:
        allowed_basis = {"instant"}
    if allowed_basis and fact.get("basis") not in allowed_basis:
        raise AgentError("SCOPE_MISMATCH", "证据累计/单季/时点口径与任务不一致，不能直接替代")
    profits = constraints.get("profit_metrics", [])
    if profits and "profit" in fact.get("metric", "") and fact["metric"] not in profits:
        raise AgentError("SCOPE_MISMATCH", "证据利润口径与用户要求不一致")
    if fact.get("metric") in constraints.get("excluded_metrics", []):
        raise AgentError("SCOPE_MISMATCH", "该指标已被用户排除")

