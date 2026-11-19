from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import json
import re

from pydantic import Field

from ..config import StrictModel
from ..data.contracts import Observation
from ..data.evidence import write_json
from ..data.symbols import price_unit
from ..errors import AgentError
from ..finish import FinalAnswer, finish_errors
from ..runtime import TZ
from .registry import Tool


class CalculateArgs(StrictModel):
    result_id: str = Field(pattern=r"^result_[a-f0-9]{32}$", description="本任务 get_quote 或 get_daily_bars 返回的完整结果 ID，禁止自行填入价格")
    trade_date: date | None = Field(None, description="日线计算必须指定目标日期；报价快照时留空")
    previous_result_id: str | None = Field(None, pattern=r"^result_[a-f0-9]{32}$", description="首份日线结果缺少前一条交易记录时，补查日线所得结果 ID")


def registered_result(store, ctx, key):
    if key not in ctx.result_tools or key not in ctx.evidence_ids:
        raise AgentError("EVIDENCE_DENIED", "结果未在当前任务登记，不能作为计算输入")
    value = store.load_result(key)
    if value.status not in ("ok", "partial") or not value.data or not value.snapshot_id or not store.has_raw(value.snapshot_id):
        raise AgentError("INVALID_CALCULATION_INPUT", "输入没有可用数据及原始证据")
    if not value.symbol or value.market != "CN_A":
        raise AgentError("SCOPE_MISMATCH", "计算输入缺少有效证券或市场标识")
    if value.currency != "CNY" or value.units.get("price") != price_unit(value.symbol):
        raise AgentError("INCOMPARABLE_PRICES", "价格币种或单位不一致")
    return value


def calculate(args, ctx, store):
    value = registered_result(store, ctx, args.result_id)
    # The registered input identifies the instrument, even if another stock
    # was queried more recently. Never label its numbers with the current focus.
    ctx.bind_symbol(value.symbol)
    name = ctx.result_tools[args.result_id]
    inputs = [(args.result_id, value)]
    previous_date = None
    if name == "get_quote":
        if args.trade_date is not None or args.previous_result_id is not None:
            raise AgentError("SNAPSHOT_NOT_HISTORICAL", "报价快照不能计算指定历史交易日，请获取日线")
        stamp = datetime.fromisoformat(value.data["data_time"])
        if stamp > ctx.as_of or stamp.astimezone(TZ).date() != ctx.as_of.astimezone(TZ).date():
            raise AgentError("STALE_QUOTE_DATE", "报价不是截止自然日的行情，不能冒充该日收盘表现")
        if value.price_basis != "unadjusted_snapshot":
            raise AgentError("INCOMPARABLE_PRICES", "报价价格口径不受支持")
        current, previous = value.data["price"], value.data.get("previous_close")
        trade_date = stamp.date().isoformat()
        locators = ["quote.price", "quote.previous_close"]
        warnings = ["当前行情快照计算；未确认最终收盘，昨收日期未由交易日历独立核验"]
    elif name == "get_daily_bars":
        if not args.trade_date or args.trade_date >= ctx.as_of.astimezone(TZ).date():
            raise AgentError("INVALID_TRADE_DATE", "日线计算必须指定截止日之前的交易日期")
        if value.price_basis != "unadjusted":
            raise AgentError("INCOMPARABLE_PRICES", "仅支持同口径不复权日线")
        trade_date = args.trade_date.isoformat()
        target = next((bar for bar in value.data["bars"] if bar["date"] == trade_date), None)
        if not target:
            raise AgentError("TARGET_BAR_MISSING", "当前结果没有目标交易日；不能使用最近一日冒充")
        previous_value = value
        if args.previous_result_id:
            previous_value = registered_result(store, ctx, args.previous_result_id)
            if previous_value.symbol != value.symbol:
                raise AgentError("INCOMPARABLE_PRICES", "前后价格必须属于同一证券，不能混用不同公司的日线")
            if ctx.result_tools[args.previous_result_id] != "get_daily_bars":
                raise AgentError("INCOMPARABLE_PRICES", "历史前收必须来自日线，不能混入当前报价昨收")
            if (previous_value.source, previous_value.price_basis, previous_value.currency, previous_value.units) != (value.source, value.price_basis, value.currency, value.units):
                raise AgentError("INCOMPARABLE_PRICES", "前后价格的来源、单位或口径不一致")
            inputs.append((args.previous_result_id, previous_value))
        earlier = [bar for bar in previous_value.data["bars"] if bar["date"] < trade_date]
        if not earlier:
            raise AgentError("MISSING_PREVIOUS_CLOSE", "缺少前一可比交易记录，请扩大日线窗口补查后使用 previous_result_id")
        prior = max(earlier, key=lambda bar: bar["date"])
        # Check adjacency and operands against the source sequence, not a calendar guess.
        original = {}
        for _, item in inputs:
            raw = json.loads((store.root / "evidence" / f"{item.snapshot_id}.bin").read_bytes())
            if not isinstance(raw, list):
                raise AgentError("INVALID_CALCULATION_INPUT", "原始日线不是记录数组")
            for row in raw:
                day = str(row["day"])[:10]
                close = Decimal(str(row["close"]))
                if day in original and original[day] != close:
                    raise AgentError("PRICE_REVISION_CONFLICT", "两份原始序列价格冲突，不能跨版本拼接")
                original[day] = close
        known_prior = [day for day in original if day < trade_date]
        if not known_prior or prior["date"] != max(known_prior):
            raise AgentError("NON_ADJACENT_PRICES", "所给前收不是原始序列中的前一条可比交易记录，请补查")
        current, previous = target["close"], prior["close"]
        if Decimal(str(current)) != original.get(trade_date) or Decimal(str(previous)) != original.get(prior["date"]):
            raise AgentError("PRICE_EVIDENCE_MISMATCH", "规范价格与原始证据字段不一致")
        previous_date = prior["date"]
        locators = [target["locator"] + ".close", prior["locator"] + ".close"]
        warnings = ["前收按同来源原始序列的前一条记录选取；未由交易日历确认完整覆盖", "源站日线最终状态未知，不能宣称最终收盘已独立验证"]
    else:
        raise AgentError("INVALID_CALCULATION_INPUT", "只允许已登记行情或日线结果作为价格输入")
    if previous is None:
        raise AgentError("MISSING_PREVIOUS_CLOSE", "来源缺少昨收，不计算涨跌幅；请补查同口径前收")
    current, previous = Decimal(str(current)), Decimal(str(previous))
    if not current.is_finite() or not previous.is_finite() or current <= 0 or previous <= 0:
        raise AgentError("INVALID_PRICE", "价格必须是有限正数，前收不能为零")
    difference = current - previous
    percent = difference / previous * Decimal(100)
    snapshots = list(dict.fromkeys(item.snapshot_id for _, item in inputs))
    operands = [{"result_id": inputs[0][0], "snapshot_id": inputs[0][1].snapshot_id, "locator": locators[0], "value": str(current)},
                {"result_id": inputs[-1][0], "snapshot_id": inputs[-1][1].snapshot_id, "locator": locators[1], "value": str(previous)}]
    return Observation(status="partial", source="deterministic_decimal", market="CN_A", symbol=value.symbol,
                       currency="CNY", units={"price": price_unit(value.symbol), "change_amount": price_unit(value.symbol), "change_percent": "%"},
                       price_basis=value.price_basis, data_time=trade_date, snapshot_ids=snapshots,
                       stale=any(item.stale for _, item in inputs), warnings=warnings + [w for _, item in inputs for w in item.warnings],
                       data={"kind": "market_metrics", "trade_date": trade_date, "previous_trade_date": previous_date,
                             "current_price": float(current), "previous_close": float(previous),
                             "change_amount": float(difference.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)),
                             "change_percent": float(percent.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)),
                             "formula": {"change_amount": "current_price - previous_close", "change_percent": "(current_price / previous_close - 1) * 100"},
                             "rounding": "ROUND_HALF_UP; amount=8 decimal places; percent=6 decimal places",
                             "operands": operands, "input_result_ids": [key for key, _ in inputs], "finality": "unknown"})


def export_result(args, ctx, store):
    if not ctx.run_id or not ctx.run_dir or not re.fullmatch(r"[a-f0-9]{32}", ctx.run_id):
        raise AgentError("NO_ACTIVE_RUN", "导出只能在宿主管理的任务中执行")
    expected = store.root / "runs" / ctx.run_id
    if expected.resolve() != ctx.run_dir.resolve() or not ctx.run_dir.resolve().is_relative_to(store.root.resolve()):
        raise AgentError("EXPORT_DENIED", "任务导出目录越界")
    errors = finish_errors(args, ctx, store, required_tools=ctx.required_tools - {"export_research_result"})
    if errors:
        raise AgentError("EXPORT_VALIDATION_FAILED", "; ".join(errors))
    directory = expected / "exports"
    paths = [directory / "research.json", directory / "research.md"]
    if not directory.resolve().is_relative_to(expected.resolve()) or any(not p.resolve().is_relative_to(expected.resolve()) for p in paths):
        raise AgentError("EXPORT_DENIED", "导出路径越界")
    from .. import __version__
    body = {"run_id": ctx.run_id, "finalized": False, "symbol": ctx.symbol,
            "as_of": ctx.as_of.isoformat(), "scope": ctx.scope, "version": __version__, **args.model_dump(mode="json")}
    write_json(paths[0], body)
    paths[1].write_text("# FinSight 研究草稿\n\n研究范围：" + str(ctx.symbol) + "；截止时间：" + ctx.as_of.isoformat() + "\n\n" + args.answer + "\n\n状态：" + args.status + "\n\n原始证据：" + ", ".join(args.evidence_ids) + "\n\n计算结果：" + ", ".join(args.calculation_ids), encoding="utf-8")
    return Observation(status="ok", source="local_export", data={"run_id": ctx.run_id, "finalized": False,
                       "paths": [str(p) for p in paths], "note": "可重复更新的任务草稿；宿主结束核验后另写最终 report.md/result.json"})


def register_research_tools(registry, service):
    registry.register(Tool("calculate_market_metrics", "只从本任务已核验结果及原始证据计算涨跌额/涨跌幅；缺少前收时返回缺口，先补查再重试。", CalculateArgs,
                           lambda a, c: calculate(a, c, service.store), False))
    registry.register(Tool("export_research_result", "将已校验结构化回答导出为本任务固定路径内的 Markdown/JSON 草稿；不接受路径，不代表宿主已完成。", FinalAnswer,
                           lambda a, c: export_result(a, c, service.store), False))
