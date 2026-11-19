from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal
from importlib.resources import files
import json

from pydantic import Field, field_validator

from ..config import StrictModel
from ..data.contracts import Observation
from ..data.symbols import is_index, normalize_symbol
from ..errors import AgentError
from ..market import get_market, review_metrics, resolve_market_day
from ..runtime import TZ, now
from .registry import Tool


class MarketArgs(StrictModel):
    trade_date: date
    window: int = Field(120, ge=2, le=250)
    mode: Literal["online", "replay"] = "online"


class BenchmarkArgs(MarketArgs):
    symbol: str = Field("000300.SH", description="基准指数代码，默认沪深300；支持000001.SH上证指数、399001.SZ深证成指、399006.SZ创业板指等；不会切换当前研究对象")

    @field_validator("symbol")
    @classmethod
    def index_symbol(cls, value):
        try:
            symbol = normalize_symbol(value)
        except AgentError as exc:
            raise ValueError(exc.message) from exc
        if not is_index(symbol):
            raise ValueError("基准必须为已支持的指数，代码需明确交易所")
        return symbol


class ReviewArgs(StrictModel):
    result_id: str = Field(pattern=r"^result_[a-f0-9]{32}$")
    benchmark_result_id: str | None = Field(None, pattern=r"^result_[a-f0-9]{32}$")


class ScopeArgs(StrictModel):
    trade_date: date | None = None
    report_period: date | None = None
    metric: str | None = Field(None, max_length=80)
    task_type: str = Field("financial_query", max_length=80)
    plan: list[str] = Field(default_factory=list, max_length=8)


class GuideArgs(StrictModel):
    name: Literal["growth", "margin", "closing_review", "event_check"]


def resolve_scope(args, ctx):
    fixed_period = ctx.scope_constraints.get("report_period")
    if args.report_period and fixed_period and str(args.report_period) != fixed_period:
        raise AgentError("SCOPE_MISMATCH", f"宿主已经解析本轮报告期为{fixed_period}；相对年份已处理，不能再次偏移")
    if args.metric in ("利润", "profit") and ctx.scope.get("metric") not in ("net_profit", "attributable_net_profit", "operating_profit"):
        ctx.clarification = "请明确利润口径：全部净利润、归母净利润，还是营业利润？"
        return Observation(status="partial", data={"needs_clarification": True, "question": ctx.clarification})
    update = args.model_dump(mode="json", exclude_none=True)
    if args.trade_date:
        # Requests with explicit historical dates default to end of that market day.
        market_scope = resolve_market_day(args.trade_date, ctx)
        if not ctx.cutoff_explicit and args.trade_date < now().astimezone(TZ).date():
            ctx.as_of = datetime.combine(date.fromisoformat(market_scope["trade_date"]), time(23, 59, 59), TZ)
        update.update(market_scope)
    if args.report_period and args.report_period > ctx.as_of.astimezone(TZ).date():
        raise AgentError("INVALID_REPORT_PERIOD", "报告期晚于截止日期")
    if any(k in ctx.scope and ctx.scope[k] != v for k, v in update.items() if k in ("trade_date", "report_period", "metric")):
        ctx.evidence_ids.clear()
        ctx.document_ids.clear()
        ctx.result_tools.clear()
        ctx.perception_results.clear()
    ctx.scope.update(update)
    return Observation(status="ok", source="host_scope", symbol=ctx.symbol, market="CN_A",
                       data={"scope": ctx.scope, "as_of": ctx.as_of.isoformat(),
                             "historical_replay": not bool(args.trade_date and ctx.as_of.astimezone(TZ).date() > args.trade_date),
                             "note": "显式允许目标日之后信息时属于含后续信息的事后复盘"})


def register_analysis_tools(registry, service):
    registry.register(Tool("get_research_guide", "按需读取同比比较、利润率、收盘复盘或事件查证的版本化分析步骤、公式及失败条件；不改变工具权限。", GuideArgs,
                           lambda a, c: Observation(status="ok", source="versioned_analysis_guide", data={
                               "name": a.name, **json.loads(files("finagent").joinpath("resources/analysis_guides.json").read_text(encoding="utf-8"))[a.name]}), False))
    registry.register(Tool("resolve_task_scope", "确认任务报告期/指标/交易日。历史行情无显式截止时固定为目标自然日23:59:59；使用XSHG日历解析休市，拒绝未来。切换期间清空依赖结果。", ScopeArgs, resolve_scope, False))
    registry.register(Tool("get_market_data", "按已支持交易所日历获取当前所选股票或指数的收盘日线；名称先resolve_entity。指数价格单位index_point。休市说明实际交易日，默认120交易日；最终性未知保持partial。", MarketArgs,
                           lambda a, c: get_market(service, c, a.trade_date, a.window, mode=a.mode)))
    registry.register(Tool("get_benchmark_data", "获取指定主要指数对应日期和窗口，默认沪深300，可选上证、深证成指、创业板指等；不切换当前研究对象。单位index_point，不复权。", BenchmarkArgs,
                           lambda a, c: get_market(service, c, a.trade_date, a.window, True, a.mode, a.symbol)))
    registry.register(Tool("calculate_closing_review", "从已登记证券/基准序列及原始字段核验涨跌、MA5/20/60、前20日均量倍数、波动和基准百分点差。长度/日期/公司行动/最终性不足保留缺口。", ReviewArgs,
                           lambda a, c: review_metrics(a, c, service.store), False))
    from ..technical import DivergenceArgs, calculate_divergences
    registry.register(Tool("calculate_technical_divergence", "从本任务get_market_data原始证据计算日线MACD/RSI顶底背离；返回双枢轴、确认时间、候选与失效状态。缺数据不判断，无跨周期推断。", DivergenceArgs,
                           lambda a, c: calculate_divergences(a, c, service.store), False))
    from ..finish import FinalAnswer, finish_errors
    registry.register(Tool("verify_analysis", "核验结构化结论、证据、计算、条件化建议和范围；不能用模型自述代替核验。语义支持仍需人工审阅。", FinalAnswer,
                           lambda a, c: Observation(status="ok", source="deterministic_verifier", data={
                               "passed": not (errors := finish_errors(a, c, service.store, required_tools=c.required_tools - {"verify_analysis", "export_research_result"})),
                               "errors": errors, "manual_review": ["原文会计口径、解释假设和自然语言原子事实的完整支持"]}), False))
