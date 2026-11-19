"""Deterministic financial-answer checks. No LLM judge or oracle model calls."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Literal

from pydantic import Field, field_validator

from ..config import StrictModel
from .fixtures import truth_index


class Citation(StrictModel):
    document_id: str
    locator: str


class FinancialClaim(StrictModel):
    symbol: str
    metric: str
    period_end: str
    scope: Literal["consolidated", "parent"]
    basis: Literal["annual", "ytd", "quarter", "instant"]
    currency: Literal["CNY", "USD", "HKD"] = "CNY"
    value: Decimal
    unit: Literal["元", "万元", "亿元", "%"] = "元"
    citations: list[Citation] = Field(default_factory=list)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value):
        if not value.is_finite() or abs(value) > Decimal("1e60"):
            raise ValueError("数值必须有限")
        return value


class InterpretedRequest(StrictModel):
    symbols: list[str]
    periods: list[str]
    metrics: list[str]
    scope: Literal["consolidated", "parent"] = "consolidated"
    basis: Literal["annual", "ytd", "quarter", "instant"] = "annual"
    currency: str = "CNY"
    unit: str = "元"
    exclude: list[str] = Field(default_factory=list)


class BenchmarkAnswer(StrictModel):
    action: Literal["answer", "clarify", "unavailable", "explain"]
    text: str = Field(min_length=1)
    request: InterpretedRequest | None = None
    claims: list[FinancialClaim] = Field(default_factory=list, max_length=30)


def answer_contract():
    return """本次是冻结合成财报的受控实验，所有金额都是虚构数据。最终回答仅返回一个JSON对象，不加代码围栏：
{"action":"answer|clarify|unavailable|explain","text":"给用户的中文简短答复","request":{"symbols":["600519.SH"],"periods":["2024-12-31"],"metrics":["revenue"],"scope":"consolidated","basis":"annual","currency":"CNY","unit":"元","exclude":[]},"claims":[{"symbol":"600519.SH","metric":"revenue","period_end":"2024-12-31","scope":"consolidated","basis":"annual","currency":"CNY","value":"从资料取得的金额","unit":"元","citations":[{"document_id":"真实入库doc_ID","locator":"table:1或精确单元格定位"}]}]}。
该示例只说明格式，不代表用户本次要求。request写你对最新请求及继承条件的理解。指标规范名：revenue营业收入、net_profit全部净利润、attributable_net_profit归母净利润、operating_cashflow经营现金流、operating_profit营业利润、total_assets总资产、total_liabilities总负债。同比指标在原指标后加_yoy，单位%，必须引用本期与上年同期两份资料。金额按元精确到分，不自行舍入；增长率至少保留两位小数。排除股价规范为exclude=["price"]。默认合并报表、人民币、年度口径；用户明确更正优先。裸“利润”和未说明累计/单季的“三季度”需澄清。澄清时request可null，claims=[]。没有对应资料时action=unavailable，claims=[]，禁止补数。数字引用必须定位到包含对应科目的原文表格或单元格。声明的每个数值都列入claims，text不得增加未列入claims的金额。
只需完成当前查询与必要计算，不运行深度研究、后台任务或其他模型。获得匹配原文后简短回答；资料缺失最多补查一次后说明缺口。"""


def expected_value(spec, index):
    metric = spec["metric"]
    key = (spec["symbol"], metric.removesuffix("_yoy"), spec["period_end"], spec["scope"], spec["basis"])
    current = index.get(key)
    if not current:
        return None, [], None
    value = Decimal(current["normalized_value"])
    if not metric.endswith("_yoy"):
        return value, [current], "元"
    end = spec["period_end"]
    prior_end = f"{int(end[:4])-1}{end[4:]}"
    prior = index.get((key[0], key[1], prior_end, key[3], key[4]))
    if not prior or Decimal(prior["normalized_value"]) <= 0:
        return None, [], None
    return (value / Decimal(prior["normalized_value"]) - 1) * 100, [current, prior], "%"


def _key(claim):
    return tuple(claim[k] for k in ("symbol", "metric", "period_end", "scope", "basis"))


def _parse(text):
    # Fences are accepted as a formatting deviation, not silently repaired JSON.
    content = text.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    return BenchmarkAnswer.model_validate_json(content)


def _clarification_question(text, field):
    question = any(marker in text for marker in ("？", "?", "请明确", "请确认", "请补充", "哪种", "哪一个"))
    words = {"profit_basis": ("归母", "营业利润", "净利润", "利润口径"), "period_basis": ("单季", "累计")}.get(field, ())
    return question and any(word in text for word in words)


def text_amounts(text, claims):
    """Check explicit money/percent tokens; unitless numbers need human review."""
    pattern = r"(?<![A-Za-z0-9_])([+-]?\d[\d,]*(?:\.\d+)?)\s*(亿元|万元|人民币元|美元|港元|元|%|％|个百分点)"
    scales = {"亿元": Decimal(100000000), "万元": Decimal(10000)}
    found = []
    for match in re.finditer(pattern, text):
        number = Decimal(match[1].replace(",", "")) * scales.get(match[2], Decimal(1))
        percent = match[2] in {"%", "％", "个百分点"}
        tolerance = Decimal(".005") if percent else Decimal(".01")
        declared = any((c.unit == "%") == percent and
                       abs(c.value * scales.get(c.unit, Decimal(1)) - number) <= tolerance for c in claims)
        found.append({"text": match[0], "value_base_unit": str(number), "declared": declared})
    return found


def grade(answer, gold, documents, *, accessible=None, cutoff, runtime_status="completed"):
    """Accessible is a set of actually shown (doc_id, locator), or all for inline."""
    result = {"format_valid": False, "task_success": False, "slot_correct": 0, "slot_total": len(gold.get("request") or {}),
              "claim_correct": 0, "claim_required": len(gold["claims"]), "claim_emitted": 0,
              "citation_supported": 0, "citation_claims": 0, "unsupported_numeric_claims": 0,
              "text_amount_count": 0, "undeclared_text_amounts": 0, "unsupported_text_amounts": 0, "contract_complete": False,
              "unnecessary_clarification": False, "checks": [], "claim_checks": []}
    try:
        parsed = _parse(answer)
        result["format_valid"] = True
        payload = parsed.model_dump(mode="json")
    except (ValueError, TypeError):
        if gold["action"] == "clarify" and runtime_status in {"completed", "needs_clarification"}:
            result["task_success"] = _clarification_question(answer, gold.get("clarification_field"))
        result["checks"].append({"check": "json_contract", "passed": False})
        return result
    action_ok = payload["action"] == gold["action"]
    amounts = text_amounts(payload["text"], parsed.claims)
    result["text_amount_count"] = len(amounts)
    result["undeclared_text_amounts"] = sum(not item["declared"] for item in amounts)
    result["text_amount_checks"] = amounts
    result["contract_complete"] = result["undeclared_text_amounts"] == 0
    result["unnecessary_clarification"] = payload["action"] == "clarify" and gold["action"] != "clarify"
    result["checks"].append({"check": "action", "passed": action_ok, "actual": payload["action"], "expected": gold["action"]})
    expected_request = gold.get("request")
    if expected_request:
        actual = payload["request"] or {}
        for field, expected in expected_request.items():
            value = actual.get(field)
            same = sorted(value) == sorted(expected) if isinstance(value, list) and isinstance(expected, list) else value == expected
            result["slot_correct"] += int(same)
            result["checks"].append({"check": "request." + field, "passed": same, "actual": value, "expected": expected})
    index = truth_index(documents)
    wanted = {_key(c) for c in gold["claims"]}
    emitted, correct = set(), set()
    cutoff_time = datetime.fromisoformat(cutoff)
    # A correct contextual amount omitted from claims is a declaration defect,
    # not automatically a hallucination. Keep these two findings separate.
    visible_amounts = []
    for fact in index.values():
        shown = accessible is None or any(fact["document_id"] == doc_id and
            (fact["locator"] == locator or fact["locator"].startswith(locator + "/")) for doc_id, locator in accessible)
        if shown and datetime.fromisoformat(fact["available_at"]) <= cutoff_time:
            visible_amounts.append(Decimal(fact["normalized_value"]))
    for item in amounts:
        percent = item["text"].rstrip().endswith(("%", "％", "个百分点"))
        item["grounded_in_visible_sources"] = item["declared"] or (not percent and any(
            abs(Decimal(item["value_base_unit"]) - value) <= Decimal(".01") for value in visible_amounts))
    result["unsupported_text_amounts"] = sum(not item["grounded_in_visible_sources"] for item in amounts)
    scales = {"元": Decimal(1), "万元": Decimal(10000), "亿元": Decimal(100000000), "%": Decimal(1)}
    for claim in parsed.claims:
        raw = claim.model_dump(mode="json")
        key = _key(raw)
        value, facts, unit = expected_value(raw, index)
        actual_value = claim.value * scales[claim.unit]
        tolerance = Decimal(".005") if unit == "%" else Decimal(".01")
        number_ok = (value is not None and claim.currency == "CNY" and (claim.unit == "%") == (unit == "%")
                     and abs(actual_value - value) <= tolerance and all(datetime.fromisoformat(f["available_at"]) <= cutoff_time for f in facts))
        valid_citations = []
        for citation in claim.citations:
            valid = any(citation.document_id == f["document_id"] and
                        (citation.locator == f["locator"] or f["locator"].startswith(citation.locator + "/")) for f in facts)
            shown = accessible is None or any(citation.document_id == doc_id and
                        (citation.locator == locator or citation.locator.startswith(locator + "/")) for doc_id, locator in accessible)
            valid_citations.append(valid and shown)
        covered = all(any(c.document_id == f["document_id"] and
                          (c.locator == f["locator"] or f["locator"].startswith(c.locator + "/"))
                          and valid for c, valid in zip(claim.citations, valid_citations)) for f in facts)
        support_ok = bool(facts and claim.citations) and all(valid_citations) and covered
        unique = key not in emitted
        expected = key in wanted
        result["claim_emitted"] += 1
        result["citation_claims"] += 1
        result["citation_supported"] += int(support_ok and number_ok)
        result["unsupported_numeric_claims"] += int(not (number_ok and support_ok))
        if number_ok and expected and unique:
            correct.add(key)
        result["claim_checks"].append({"key": list(key), "value_correct": number_ok, "citation_supported": support_ok,
            "expected_claim": expected, "duplicate": not unique, "actual_value_base_unit": str(actual_value),
            "expected_value_base_unit": str(value) if value is not None else None})
        emitted.add(key)
    result["claim_correct"] = len(correct)
    if gold["action"] == "clarify":
        clarification_ok = _clarification_question(payload["text"], gold.get("clarification_field"))
    else:
        clarification_ok = True
    result["task_success"] = (runtime_status in {"completed", "needs_clarification"} and action_ok and clarification_ok
        and result["slot_correct"] == result["slot_total"] and correct == wanted and emitted == wanted
        and result["claim_emitted"] == len(wanted) and result["unsupported_numeric_claims"] == 0)
    result["task_success"] = result["task_success"] and result["unsupported_text_amounts"] == 0
    return result
