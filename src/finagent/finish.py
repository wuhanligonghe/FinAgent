from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationError

from .config import StrictModel
from .data.contracts import Quote
from .data.symbols import price_unit, volume_unit


QUOTE_FACT_UNITS = {
    "price": "CNY/share", "previous_close": "CNY/share", "open": "CNY/share",
    "high": "CNY/share", "low": "CNY/share", "volume_shares": "share",
    "amount_cny": "CNY", "pe_ttm": "multiple", "pb": "multiple",
}


class FinalAnswer(StrictModel):
    status: Literal["completed", "partial", "needs_clarification"]
    answer: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    calculation_ids: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    facts: list[dict] = Field(default_factory=list, description="可核验事实；reference为在线三表fact_id、行情快照result_id或计算result_id；快照/行情计算还需field，另含value、unit和statement")
    hypotheses: list[dict] = Field(default_factory=list, description="解释假设含statement和event_ids；不得使用盘后事件解释此前涨跌")
    recommendations: list[dict] = Field(default_factory=list, description="研究建议含type,basis_ids,action,trigger,invalidation,time_range；不含目标价/仓位")


def parse_final_answer(text: str) -> tuple[FinalAnswer, bool]:
    """Normalize only literal LF/CR/tab in JSON strings; retain all validation.

    The caller keeps the original provider message for audit and protocol replay.
    No missing delimiters, quotes, fields or evidence are synthesized here.
    """
    try:
        return FinalAnswer.model_validate_json(text), False
    except ValidationError:
        normalized = []
        in_string = escaped = changed = False
        for char in text:
            if escaped:
                normalized.append(char)
                escaped = False
                continue
            if in_string and char == "\\":
                escaped = True
            elif char == '"':
                in_string = not in_string
            if in_string and char in "\n\r\t":
                normalized.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
                changed = True
            else:
                normalized.append(char)
        if not changed:
            raise
        return FinalAnswer.model_validate_json("".join(normalized)), True


def final_validation_feedback(error: ValidationError) -> str:
    """Describe format failures without copying model output into feedback."""
    details = error.errors(include_input=False, include_context=False, include_url=False)
    if any(item["type"] == "json_invalid" for item in details):
        return ("结束输出JSON语法无效：必须只输出一个完整JSON对象；字符串内的换行、回车、制表符"
                "分别写为\\n、\\r、\\t，双引号写为\\\"；不要使用代码围栏或附加正文")
    fields = sorted({str(item["loc"][0]) for item in details
                     if item["loc"] and item["loc"][0] in FinalAnswer.model_fields})
    return ("结束输出不符合系统JSON字段协议：检查必填status、answer及各数组类型，禁止额外字段"
            + ("；需检查字段：" + "、".join(fields) if fields else ""))


def finish_errors(final, ctx, store, *, require_data=True, required_tools=()):
    usable = {key: store.load_result(key) for key in ctx.result_tools}
    usable = {key: value for key, value in usable.items() if key not in ctx.invalidated_result_ids and value.status in ("ok", "partial") and value.data}
    perceptions = {key: value for key, value in usable.items()
                   if key in ctx.perception_results and value.snapshot_id and store.has_raw(value.snapshot_id)}
    # Online name-search snapshots may support the identity mapping, but do not
    # satisfy the separate requirement to actually fetch financial observations.
    sources = {key: value for key, value in usable.items()
               if (key in perceptions or ctx.result_tools[key] == "resolve_entity")
               and value.snapshot_id and store.has_raw(value.snapshot_id)}
    raw_ids = {raw for value in sources.values() for raw in [value.snapshot_id, *value.snapshot_ids]
               if raw and store.has_raw(raw)}
    calculation_tools = {"calculate_market_metrics", "execute_program", "calculate_closing_review", "calculate_technical_divergence"}
    calculations = {key: value for key, value in usable.items()
                    if ctx.result_tools[key] in calculation_tools}
    errors = []
    if set(final.evidence_ids) - raw_ids:
        errors.append("引用了不存在或未取得可用结果的原始证据ID")
    if set(final.calculation_ids) - set(calculations):
        errors.append("计算引用不是本任务已成功执行的计算结果")
    for key in final.calculation_ids:
        if key in calculations and not set(calculations[key].snapshot_ids).issubset(final.evidence_ids):
            errors.append("计算引用必须同时引用全部输入原始证据")
    from .data.financial_facts import StatementFacts, numeric
    SCALES = {"元": 1, "千元": 1000, "万元": 10000, "百万元": 1000000, "亿元": 100000000}
    from .errors import AgentError
    library = StatementFacts(store)
    for index, claim in enumerate(final.facts):
        def fact_error(message):
            errors.append(f"facts[{index}]：{message}")

        reference = claim.get("reference", "")
        if not isinstance(reference, str):
            fact_error("事实reference必须为真实ID字符串")
            continue
        if "value" not in claim:
            fact_error("结构化事实缺少value")
            continue
        try:
            if reference.startswith("raw_"):
                fact = library.fact(reference, ctx)
                if fact["snapshot_id"] not in final.evidence_ids:
                    fact_error("财务事实未引用原始证据")
                if claim.get("unit") not in SCALES or numeric(claim["value"]) * SCALES[claim["unit"]] != numeric(fact["normalized_value"]):
                    fact_error("事实数值或单位与三表接口字段不一致")
            elif reference in perceptions and ctx.result_tools[reference] == "get_quote":
                value = perceptions[reference]
                quote = Quote.model_validate(value.data)
                if (quote.symbol != ctx.symbol or value.symbol != ctx.symbol or value.market != "CN_A"
                        or value.currency != "CNY" or value.price_basis != "unadjusted_snapshot"
                        or value.units.get("price") != price_unit(value.symbol)
                        or quote.data_time > ctx.as_of or quote.data_time.isoformat() != value.data_time):
                    fact_error("行情快照的证券、市场、币种、价格口径或数据时间与本任务不一致")
                if value.snapshot_id not in final.evidence_ids:
                    fact_error("行情快照事实未引用其原始证据，请在evidence_ids登记该快照snapshot_id")
                field = claim.get("field", "")
                if field not in QUOTE_FACT_UNITS:
                    fact_error("行情快照field只能为price、previous_close、open、high、low、volume_shares、amount_cny、pe_ttm或pb；涨跌额/幅必须引用计算结果")
                else:
                    expected = value.data.get(field)
                    unit = (price_unit(value.symbol) if field in ("price", "previous_close", "open", "high", "low")
                            else volume_unit(value.symbol) if field == "volume_shares" else QUOTE_FACT_UNITS[field])
                    if expected is None or claim.get("unit") != unit or numeric(claim["value"]) != numeric(expected):
                        fact_error(f"行情快照{field}数值或单位不一致；value应为工具原值，unit应为{unit}")
            elif reference in final.calculation_ids and reference in calculations:
                calculation = calculations[reference].data
                if calculation.get("kind") == "financial_calculation" and (numeric(claim["value"]) != numeric(calculation["value"]) or claim.get("unit") != calculation["unit"]):
                    fact_error("计算事实的数值/单位与程序结果不一致")
                elif calculation.get("kind") in ("market_metrics", "closing_review"):
                    field = claim.get("field", "")
                    unit_price = price_unit(calculations[reference].symbol)
                    allowed = {"change_amount": unit_price, "change_percent": "%", "benchmark_change_percent": "%",
                               "benchmark_difference_pp": "percentage_points", "volume_multiple_previous_20": "multiple",
                               "volatility_20_daily_std_percent": "%", "current_price": unit_price, "previous_close": unit_price}
                    data = calculation.get("metrics", calculation)
                    if field.startswith(("moving_averages.", "price_vs_ma_percent.")):
                        group, count = field.split(".", 1)
                        expected = data.get(group, {}).get(count)
                        unit = unit_price if group == "moving_averages" else "%"
                    else:
                        expected, unit = data.get(field), allowed.get(field)
                    if expected is None or unit is None or claim.get("unit") != unit or abs(numeric(claim["value"]) - numeric(expected)) > numeric("0.000001"):
                        fact_error("行情事实的字段、数值或单位与程序结果不一致")
            else:
                fact_error("结构化事实必须引用当前范围的在线三表fact_id、get_quote返回的result_id或已在calculation_ids引用的计算result_id；行情引用需提供field")
        except (AgentError, ValueError, KeyError, TypeError):
            fact_error("事实引用或数值无效，请检查reference、field、value和unit")
    event_index = {event["event_id"]: event for key, value in usable.items() if ctx.result_tools[key] == "search_company_events"
                   for event in value.data.get("events", [])}
    for hypothesis in final.hypotheses:
        refs = hypothesis.get("event_ids", [])
        if not refs or any(key not in event_index for key in refs):
            errors.append("解释假设缺少本任务事件依据")
        if any(not event_index[key]["eligible_as_possible_factor"] for key in refs if key in event_index):
            errors.append("后续事项或无明确盘前时间的事件不能作为整个此前区间涨跌的解释假设")
    for advice in final.recommendations:
        if not all(advice.get(key) for key in ("type", "basis_ids", "action", "trigger", "invalidation", "time_range")):
            errors.append("研究建议缺少依据、动作、触发、重评条件或适用时间")
        basis = advice.get("basis_ids", [])
        if not isinstance(basis, list) or not all(isinstance(key, str) for key in basis):
            errors.append("研究建议basis_ids必须是引用ID数组")
            continue
        if set(basis) - (set(final.evidence_ids) | set(final.calculation_ids)):
            errors.append("研究建议依据未被本次回答引用")
        if any(key in advice for key in ("target_price", "position", "return_probability", "buy_price")):
            errors.append("首版不支持目标价、买入价、仓位或收益承诺")
    if final.status == "completed":
        satisfied = {ctx.result_tools[key] for key in usable}
        missing_tools = set(required_tools) - satisfied
        if missing_tools:
            errors.append("尚缺必需工具的数据：" + ", ".join(sorted(missing_tools)))
        if require_data and not perceptions:
            errors.append("尚无可用感知数据，不能完成数据任务")
        if require_data and ctx.scope_constraints.get("report_period") and any(ctx.result_tools[key] in ("get_financial_statements",) for key in perceptions) and not final.facts:
            errors.append("指定期间的财务事实任务必须提供facts及三表字段引用，不能仅提交自由文本")
        if perceptions and not final.evidence_ids:
            errors.append("结束回答未引用任何原始证据")
        if final.missing:
            errors.append("仍有未完成事项，不能标记 completed；保留 missing 时请改为 partial，来源固有限制可写入 answer 并与目标缺口区分")
        if any(value.stale for value in perceptions.values()):
            errors.append("仅有或使用了过期缓存，应明确部分完成及数据时效限制")
        if "calculate_market_metrics" in required_tools and not final.calculation_ids:
            errors.append("涨跌幅任务必须引用确定性计算结果 calculation_ids")
        for name in calculation_tools.intersection(required_tools):
            if not any(ctx.result_tools.get(key) == name for key in final.calculation_ids):
                errors.append(f"缺少必需计算 {name} 的引用")
        for key in final.calculation_ids:
            if key in calculations and calculations[key].data.get("kind") == "financial_calculation" and not any(f.get("reference") == key for f in final.facts):
                errors.append("财务程序结论必须在facts中引用未舍入结果及单位，不能仅提交自由文本")
        for key in final.calculation_ids:
            if key in calculations and calculations[key].data.get("kind") == "closing_review" and not calculations[key].data.get("complete"):
                errors.append("收盘复盘尚有日期/历史/供应商完整性缺口，必须partial")
            if key in calculations and calculations[key].data.get("kind") == "technical_divergence" and not calculations[key].data.get("complete"):
                errors.append("背离结构尚有历史/供应商完整性缺口，必须partial")
        for name in required_tools:
            refs = {raw for key, value in perceptions.items() if ctx.result_tools[key] == name
                    for raw in [value.snapshot_id, *value.snapshot_ids] if raw}
            if refs and not refs.intersection(final.evidence_ids):
                errors.append(f"未引用必需工具 {name} 的证据")
    return errors
