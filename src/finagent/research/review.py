"""Deterministic provenance checks. Semantic support still needs review."""
from __future__ import annotations

from datetime import datetime
import json
import re

from ..errors import AgentError

CALCULATIONS = {"calculate_market_metrics", "execute_program", "calculate_closing_review", "calculate_news_tone", "calculate_technical_divergence"}


def number_tokens(text):
    from decimal import Decimal
    return {Decimal(n) for n in re.findall(r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", str(text).replace(",", "").replace("，", ""))}


def numeric_content(value):
    if isinstance(value, dict):
        return " ".join(numeric_content(v) for k, v in value.items() if not k.endswith(("_id", "_ids", "_url")) and k not in ("locator", "url"))
    if isinstance(value, list):
        return " ".join(numeric_content(v) for v in value)
    return str(value)


def unmatched_numbers(text, support):
    from decimal import Decimal, localcontext
    missing = set()
    with localcontext() as context:
        context.prec = 60
        for number in number_tokens(text):
            precision = number.as_tuple().exponent
            matches = number in support
            if not matches and precision >= -6:
                # Presentation rounding and magnitude phrases such as "下跌2.97%"
                # are allowed; direction and units remain semantic-review items.
                matches = any(abs(v).quantize(Decimal(1).scaleb(precision)) == abs(number) for v in support)
            if not matches:
                missing.add(number)
    return missing


def nested(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nested(item)
    elif isinstance(value, list):
        for item in value:
            yield from nested(item)


def check_result(value, ctx):
    if value.status not in ("ok", "partial", "empty") or value.stale:
        raise AgentError("UNUSABLE_EVIDENCE", "失败或过期证据不能支持主张")
    if value.symbol and ctx.frozen_symbols is not None and value.symbol not in ctx.frozen_symbols:
        raise AgentError("SCOPE_MISMATCH", "证据证券不匹配")
    for item in nested(value.model_dump(mode="json")):
        for name in ("available_at", "published_at"):
            if item.get(name):
                stamp = datetime.fromisoformat(item[name])
                if stamp.tzinfo is None or stamp > ctx.as_of:
                    raise AgentError("AFTER_CUTOFF", "证据晚于截止时间或缺少时区")


def locate(value, locator):
    body = value.model_dump(mode="json")
    if locator.startswith("data/"):
        locator = "/" + locator
    if locator.startswith("/"):
        selected = body
        try:
            for part in locator.strip("/").split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                selected = selected[int(part)] if isinstance(selected, list) else selected[part]
            return selected
        except (KeyError, ValueError, TypeError, IndexError):
            raise AgentError("LOCATOR_NOT_FOUND", "JSON定位不存在")
    for item in nested(body):
        if item.get("locator") == locator:
            return item
        for fact in item.get("facts", []):
            if isinstance(fact, dict) and fact.get("locator") == locator:
                return fact
    raise AgentError("LOCATOR_NOT_FOUND", "证据原文位置不存在")


def checked_subset(candidates, ctx, store):
    """Build a NEW partial host artifact; never accept the rejected narrative."""
    from .contracts import AnalysisSubmission
    for candidate in reversed(candidates):
        try:
            submission = AnalysisSubmission.model_validate(candidate)
        except ValueError:
            continue
        accepted, rejected = [], []
        for claim in submission.claims:
            probe = AnalysisSubmission(content_status="partial", summary=claim.text, report_markdown=claim.text, claims=[claim])
            try:
                validate_claims(probe, ctx, store)
                accepted.append(claim)
            except AgentError as exc:
                rejected.append({"claim_id": claim.claim_id, "code": exc.code})
        if accepted:
            body = AnalysisSubmission(content_status="partial", summary="完整专家交接未通过；仅保留可回读的部分主张，仍需语义审阅。",
                report_markdown="完整专家交接未通过。以下为宿主逐条检查后保留的部分主张：\n\n" + "\n\n".join(c.text for c in accepted),
                claims=accepted, missing=["完整专家交接未通过，原始正文未被接纳", *submission.missing],
                limitations=[*submission.limitations, "程序检查不证明自然语言语义支持"]).model_dump(mode="json")
            body["recovery"] = {"kind": "checked_claim_subset", "rejected_claims": rejected, "original_narrative_accepted": False}
            return body
    return None


def citation_hints(value):
    hints = [{"result_id": value.result_id, "snapshot_id": value.snapshot_id or (value.snapshot_ids[0] if value.snapshot_ids else None), "locator": "/data"}]
    for key in ("metrics", "counts", "percentages", "articles", "matches", "blocks", "reports", "policies"):
        data = value.data.get(key)
        if data:
            locator = "/data/" + key + ("/0" if isinstance(data, list) else "")
            hints.append({"result_id": value.result_id, "snapshot_id": value.snapshot_id or (value.snapshot_ids[0] if value.snapshot_ids else None), "locator": locator})
    return [h for h in hints if h["snapshot_id"]]


def validate_claims(submission, ctx, store):
    for claim in submission.claims:
        support = [str(ctx.symbol), json.dumps(ctx.scope, ensure_ascii=False, default=str)]
        for ref in claim.evidence_refs:
            if ref.result_id not in ctx.result_tools or ref.snapshot_id not in ctx.evidence_ids:
                raise AgentError("EVIDENCE_DENIED", "主张引用未获授权")
            value = store.load_result(ref.result_id)
            if ref.result_id in ctx.invalidated_result_ids:
                raise AgentError("INVALIDATED_EVIDENCE", "引用已经失效")
            check_result(value, ctx)
            if ref.snapshot_id not in {value.snapshot_id, *value.snapshot_ids} or not store.has_raw(ref.snapshot_id):
                raise AgentError("SNAPSHOT_MISMATCH", "引用的原始响应与结果不匹配")
            support.append(numeric_content(locate(value, ref.locator)))
        for key in claim.calculation_refs:
            if key not in ctx.result_tools or ctx.result_tools[key] not in CALCULATIONS:
                raise AgentError("CALCULATION_DENIED", "计算不属于当前任务")
            value = store.load_result(key)
            support.append(numeric_content(value.data))
            for input_id in value.data.get("input_result_ids", []):
                if input_id in ctx.result_tools:
                    input_value = store.load_result(input_id)
                    check_result(input_value, ctx)
                    support.append(numeric_content(input_value.data))
            check_result(value, ctx)
            snapshots = set(value.snapshot_ids) | ({value.snapshot_id} if value.snapshot_id else set())
            if not snapshots or not snapshots.issubset(ctx.evidence_ids) or not all(store.has_raw(s) for s in snapshots):
                raise AgentError("CALCULATION_DENIED", "计算输入快照未获授权")
        missing = unmatched_numbers(claim.text, number_tokens(" ".join(support)))
        if claim.kind in ("fact", "calculation") and missing:
            raise AgentError("UNSUPPORTED_CLAIM_NUMBER", "主张" + claim.claim_id + "的数字缺少绑定原文/计算：" + ",".join(str(n) for n in sorted(missing)[:8]))
    if not submission.claims and submission.content_status != "unavailable":
        raise AgentError("NO_SUPPORTED_CLAIMS", "没有受支持主张的产物须标为不可用")
    supported_numbers = number_tokens(" ".join(c.text for c in submission.claims)) | number_tokens(str(ctx.symbol) + " " + ctx.as_of.isoformat())
    for prose in (submission.summary, submission.report_markdown):
        prose = re.sub(r"(?m)^\s*\d+[.、)]\s*", "", prose)
        if unmatched_numbers(prose, supported_numbers):
            raise AgentError("UNSUPPORTED_REPORT_NUMBER", "专家正文包含未登记到主张的数字")


def coverage(role, ctx, store):
    observations = {name: [] for name in ctx.result_tools.values()}
    for key, name in ctx.result_tools.items():
        if key in ctx.invalidated_result_ids:
            continue
        value = store.load_result(key)
        if value.status in ("ok", "partial") and value.data and not value.stale:
            observations.setdefault(name, []).append(value)
    has = lambda name: bool(observations.get(name))
    gaps = []
    if role == "technical":
        for name in ("get_market_data", "get_benchmark_data", "calculate_closing_review"):
            if not has(name):
                gaps.append("缺少" + name)
        if has("get_market_data"):
            bars = observations["get_market_data"][-1].data.get("bars", [])
            if len(bars) < 60:
                gaps.append("MA60有效行情窗口不足")
    elif role == "sentiment":
        for name in ("get_fund_flow", "get_market_attention", "get_market_data", "calculate_news_tone"):
            if not has(name):
                gaps.append("缺少" + name)
    elif role == "news":
        if not has("get_stock_news"):
            gaps.append("缺少公司新闻")
        if not has("get_macro_news"):
            gaps.append("缺少宏观新闻覆盖")
    elif role == "fundamentals":
        materials = observations.get("get_financial_statements", [])
        facts = [item for v in materials for item in nested(v.data) if "fact_id" in item and item.get("verification") == "provider_field_mapping"]
        metrics = {f.get("metric") for f in facts}
        periods = {f.get("period_end") for f in facts}
        if len(periods) < 2:
            gaps.append("缺少可比三表报告期间")
        if not {"revenue", "operating_cashflow", "total_assets", "total_liabilities"}.issubset(metrics) or not metrics.intersection({"net_profit", "attributable_net_profit"}):
            gaps.append("在线三表核心财务字段覆盖不足")
        if not has("execute_program"):
            gaps.append("缺少财务计算记录")
    elif role == "policy":
        if not has("read_policy_document"):
            gaps.append("缺少政策原文条款")
        if any(v.data.get("as_of_status") == "unknown" for v in observations.get("read_policy_document", [])):
            gaps.append("政策状态未知")
    return gaps


def validate_synthesis(submission, artifacts):
    claims = {f"{a['task_id']}:{c['claim_id']}": c for a in artifacts for c in a.get("claims", [])}
    if not submission.claim_refs or set(submission.claim_refs) - set(claims):
        raise AgentError("INVALID_REPORT_REFERENCE", "综合报告主张引用不存在")
    referenced = [claims[k] for k in submission.claim_refs]
    # Reject newly introduced numeric facts. This is deliberately conservative;
    # it cannot establish semantic support and never sets outcome_verified=true.
    numbers = number_tokens
    supported = set().union(*(numbers(c["text"]) for c in referenced))
    clean = re.sub(r"(?m)^\s*\d+[.、)]\s*", "", submission.report_markdown)
    if unmatched_numbers(clean, supported):
        raise AgentError("UNSUPPORTED_REPORT_NUMBER", "综合正文出现未被所引主张支持的数字")
    return {k: claims[k] for k in submission.claim_refs}
