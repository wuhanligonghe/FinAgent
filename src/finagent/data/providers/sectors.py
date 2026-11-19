from decimal import Decimal, InvalidOperation
import json
import re

from ...errors import AgentError
from ..endpoints import SINA_INDUSTRY
from .common import source_result


def decimal_value(value):
    if value in {"", "-", "--"}:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise AgentError("INVALID_RESPONSE", "行业数值字段无法解析") from exc
    if not parsed.is_finite() or abs(parsed) > Decimal("1e30"):
        raise AgentError("INVALID_RESPONSE", "行业数值不是有限范围内的数值")
    return parsed


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AgentError("DATA_CONFLICT", "行业响应包含重复代码")
        result[key] = value
    return result


def industry_snapshot(http, ctx):
    raw = http.get("sina_industry", SINA_INDUSTRY, ctx, encoding="gbk")
    match = re.fullmatch(r"\s*var\s+S_Finance_bankuai_sinaindustry\s*=\s*(\{.*\})\s*;?\s*", raw.text, re.S)
    if not match:
        raise AgentError("INVALID_RESPONSE", "新浪行业响应不是预期的行业对象")
    try:
        body = json.loads(match[1], object_pairs_hook=unique_object)
    except ValueError as exc:
        raise AgentError("INVALID_RESPONSE", "新浪行业响应不是有效 JSON") from exc
    if not isinstance(body, dict) or not body:
        raise AgentError("INVALID_RESPONSE", "新浪行业返回空对象，不能解释为没有强势行业")
    rows, unranked = [], []
    for code, text in body.items():
        if not isinstance(text, str):
            raise AgentError("INVALID_RESPONSE", "行业条目不是文本字段")
        fields = text.split(",")
        if len(fields) != 13 or fields[0] != code or not re.fullmatch(r"new_[a-z0-9_]+", code) or not fields[1].strip():
            raise AgentError("IDENTITY_MISMATCH", "行业代码、名称或字段数量不符合源合同")
        count = decimal_value(fields[2])
        change = decimal_value(fields[5])
        price, volume, amount = (decimal_value(fields[index]) for index in (3, 6, 7))
        if count is None or count < 0 or count != count.to_integral_value() or any(value is not None and value < 0 for value in (price, volume, amount)):
            raise AgentError("INVALID_RESPONSE", "行业公司数量、均价或成交量额无效")
        if change is None or count == 0:
            unranked.append({"code": code, "name": fields[1], "reason": "缺少涨跌幅或公司数量为零"})
            continue
        rows.append({"code": code, "name": fields[1], "company_count": int(count),
            "vendor_change_percent": str(change), "average_price_cny": str(price) if price is not None else None,
            "vendor_volume": str(volume) if volume is not None else None, "amount_cny": str(amount) if amount is not None else None,
            "locator": f"$.{code}", "data_time": None})
    rows.sort(key=lambda item: (-Decimal(item["vendor_change_percent"]), item["code"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    result = source_result(raw, None, {"kind": "industry_ranking", "classification": "sina_industry",
        "classification_label": "新浪行业分类", "metric": "vendor_change_percent", "order": "descending",
        "rows": rows, "provider_row_count": len(body), "ranked_count": len(rows), "unranked": unranked,
        "ranking_complete_for_returned_set": not unranked, "market_coverage_verified": False,
        "data_time_status": "unknown", "realtime_verified": False,
        "constituents_available": False,
        "coverage": "仅对供应商此次返回的行业集合排序；不等于申万一级、东财行业或全市场统一分类",
        "interpretation": "涨跌幅为供应商字段；无行情时间，只能报告获取时返回的排序，不能据此断言今日或实时强弱"},
        units={"vendor_change_percent": "percent", "average_price_cny": "CNY", "vendor_volume": "sina_vendor_volume", "amount_cny": "CNY"},
        warnings=["接口未提供行情日期或时间；fetched_at只是获取时间，不能替代行情时间",
                  "不支持历史排名回填，不证明最终收盘状态或持续走强；均价不是板块指数点位",
                  "新浪行业分类与其他供应商分类不同；成交量保留原始供应商单位，不跨源合并排名"])
    result.status = "partial" if rows else "empty"
    return result
