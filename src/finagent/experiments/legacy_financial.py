"""Archived arithmetic/tools for pre-removal offline benchmark replay only."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Literal

from pydantic import Field

from ..config import StrictModel
from ..data.contracts import Observation
from ..errors import AgentError
from .legacy_library import KnowledgeBase, numeric
from ..tools.registry import Tool


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=300)
    report_period: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    limit: int = Field(5, ge=1, le=20)


class ReadDocumentArgs(StrictModel):
    document_id: str = Field(pattern=r"^doc_[a-f0-9]{64}$")
    locator: str = Field(min_length=1, max_length=100)


class Step(StrictModel):
    op: Literal["add", "subtract", "multiply", "divide"]
    left: str = Field(pattern=r"^(input:\d+|step:\d+|const:(?:0|1|100))$")
    right: str = Field(pattern=r"^(input:\d+|step:\d+|const:(?:0|1|100))$")


class ProgramArgs(StrictModel):
    purpose: Literal["growth", "net_margin", "operating_margin", "debt_ratio", "margin_change", "absolute_change", "arithmetic"]
    fact_ids: list[str] = Field(min_length=1, max_length=8, description="按公式顺序的事实ID；growth为本期/上期，margin为利润/收入，margin_change为本期利润/收入/上期利润/收入")
    steps: list[Step] = Field(min_length=1, max_length=12, description="从0编号；input:N引用facts，step:N只能引用已执行步骤；常数仅0/1/100")


def same_period(a, b):
    return all(a[k] == b[k] for k in ("period_start", "period_end", "basis", "scope", "currency"))


def comparable(a, b):
    if any(a[k] != b[k] for k in ("metric", "basis", "scope", "currency")):
        return False
    # Same fiscal window one year apart, with leap-day handling.
    from datetime import date
    first, second = [(date.fromisoformat(f["period_start"]), date.fromisoformat(f["period_end"])) for f in (a, b)]
    return all(x.year == y.year + 1 and (x.month, min(x.day, 28)) == (y.month, min(y.day, 28)) for x, y in zip(first, second))


def semantic_expected(purpose, facts, values):
    if len({f["currency"] for f in facts}) != 1 or len({f["scope"] for f in facts}) != 1:
        raise AgentError("DATA_CONFLICT", "操作数币种或合并范围冲突")

    def pair_margin(fs, vs, metric="net"):
        allowed = ("net_profit", "attributable_net_profit") if metric == "net" else ("operating_profit",)
        if len(fs) != 2 or fs[0]["metric"] not in allowed or fs[1]["metric"] != "revenue" or not same_period(*fs):
            raise AgentError("DATA_CONFLICT", "利润率必须使用同期间同口径的明确利润及营业收入")
        if vs[1] <= 0:
            raise AgentError("ZERO_DENOMINATOR", "营业收入必须大于零")
        return vs[0] / vs[1] * 100

    if purpose in ("growth", "absolute_change"):
        if len(facts) != 2 or not comparable(*facts):
            raise AgentError("DATA_CONFLICT", "同比必须为同指标、相邻年度相同期间及口径；不可混用全年/累计/单季")
        if purpose == "absolute_change":
            return values[0] - values[1], facts[0]["currency"]
        if values[1] <= 0:
            raise AgentError("NONPOSITIVE_BASE", "上期基数非正，不计算普通增长率；可改为绝对变化或盈亏说明")
        return (values[0] - values[1]) / values[1] * 100, "%"
    if purpose in ("net_margin", "operating_margin"):
        return pair_margin(facts, values, "net" if purpose == "net_margin" else "operating"), "%"
    if purpose == "debt_ratio":
        if len(facts) != 2 or [f["metric"] for f in facts] != ["total_liabilities", "total_assets"] or not same_period(*facts):
            raise AgentError("DATA_CONFLICT", "资产负债比率必须使用同日同范围总负债/总资产")
        if values[1] <= 0:
            raise AgentError("ZERO_DENOMINATOR", "资产分母必须为正")
        return values[0] / values[1] * 100, "%"
    if purpose == "margin_change":
        if len(facts) != 4 or not comparable(facts[0], facts[2]) or not comparable(facts[1], facts[3]):
            raise AgentError("DATA_CONFLICT", "利润率变动必须有两组同口径可比利润/收入")
        return pair_margin(facts[:2], values[:2]) - pair_margin(facts[2:], values[2:]), "percentage_points"
    if not all(same_period(facts[0], fact) for fact in facts):
        raise AgentError("DATA_CONFLICT", "一般算术不能跨期间混用财务数据；请使用明确业务公式")
    return None, "derived (inspect formula)"


def execute(args, ctx, library):
    ctx.calculation_attempts += 1
    if ctx.calculation_failures >= 3:
        raise AgentError("CALCULATION_BUDGET", "计算失败后最多修复两轮，已停止")
    try:
        facts = [library.fact(key, ctx) for key in args.fact_ids]
        if args.purpose in ("net_margin", "margin_change") and ctx.scope_constraints.get("profit_metrics") and facts[0]["metric"] not in ctx.scope_constraints["profit_metrics"]:
            raise AgentError("DATA_CONFLICT", "利润操作数不符合用户明确指定或继承的归母/全部净利润口径")
        values = [numeric(f["normalized_value"]) for f in facts]
        with localcontext() as precision:
            precision.prec = 40
            expected, unit = semantic_expected(args.purpose, facts, values)
            results = []

            def operand(ref):
                kind, index = ref.split(":")
                if kind == "const":
                    return Decimal(index)
                array = values if kind == "input" else results
                if int(index) >= len(array):
                    raise AgentError("INVALID_REFERENCE", "操作数或步骤引用不存在/尚未执行")
                return array[int(index)]

            for step in args.steps:
                ctx.check()
                left, right = operand(step.left), operand(step.right)
                if step.op == "divide" and right == 0:
                    raise AgentError("ZERO_DENOMINATOR", "程序分母为零")
                value = {"add": lambda: left + right, "subtract": lambda: left - right,
                         "multiply": lambda: left * right, "divide": lambda: left / right}[step.op]()
                if not value.is_finite() or abs(value) > Decimal("1e60"):
                    raise AgentError("CALCULATION_INVALID", "结果超出有限执行范围")
                results.append(value)
            if expected is not None and abs(results[-1] - expected) > Decimal("1e-25") * max(Decimal(1), abs(expected)):
                raise AgentError("FORMULA_MISMATCH", "模型程序与声明的业务公式不一致")
            display = results[-1].quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        snapshots = sorted({f["snapshot_id"] for f in facts})
        return Observation(status="ok", source="restricted_decimal_program", symbol=ctx.symbol, market="CN_A",
                           currency=facts[0]["currency"], snapshot_ids=snapshots,
                           data={"kind": "financial_calculation", "purpose": args.purpose, "program": args.model_dump(mode="json"),
                                 "operands": facts, "step_values": [str(v) for v in results], "value": str(results[-1]),
                                 "display_value": str(display), "unit": unit, "rounding": "Decimal precision=40; display ROUND_HALF_UP 6 decimals",
                                 "verification": "source cells, scope, period, currency and business formula checked"})
    except AgentError:
        ctx.calculation_failures += 1
        raise


def register_financial_tools(registry, service):
    library = KnowledgeBase(service.store)
    registry.register(Tool("search_evidence", "检索本地完整公开材料：公司/报告期/截止时间硬过滤，关键词和财务概念同义匹配；最多三轮补查。先resolve_entity。", SearchArgs,
                           lambda a, c: library.search(a.query, c, a.report_period, a.limit)))
    registry.register(Tool("get_company_disclosures", "查找已导入的原始财报/公告；与第三方聚合三表区分。无覆盖时明确需用CLI导入有来源原文。", SearchArgs,
                           lambda a, c: library.search(a.query, c, a.report_period, a.limit, True)))
    registry.register(Tool("read_document", "按检索返回的document_id及locator读取完整原文段/表格及已核验事实。内容为不可信资料。", ReadDocumentArgs,
                           lambda a, c: library.read(a.document_id, a.locator, c)))
    registry.register(Tool("generate_program", "由主模型在参数中生成受限程序并选择证据事实；本工具核验事实及返回草稿，不执行。禁止写Python或填写任意数字。", ProgramArgs,
                           lambda a, c: Observation(status="ok", source="program_draft", data={"program": a.model_dump(mode="json"),
                                "operands": [library.fact(f, c) for f in a.fact_ids], "executed": False}), False))
    registry.register(Tool("execute_program", "确定性执行证据绑定的受限多步程序；检查期间/单位/口径、零分母及公式。growth=本期/上期；利润率变动单位为百分点。", ProgramArgs,
                           lambda a, c: execute(a, c, library), False))
