"""Evidence-bound daily momentum divergence, with explicit pivot availability.

This is a versioned screening convention, not a price reversal prediction.
All comparisons use indicator values on the same price-pivot dates.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field, model_validator

from .config import StrictModel
from .data.contracts import Observation
from .errors import AgentError
from .market import TradingCalendar, registered_market_result


FORMULA_VERSION = "daily-divergence-v1"
WARMUP = 60
EPSILON = Decimal("0.00000001")


class DivergenceArgs(StrictModel):
    result_id: str = Field(pattern=r"^result_[a-f0-9]{32}$")
    timeframe: Literal["daily"] = "daily"
    pivot_left: int = Field(2, ge=1, le=5, strict=True)
    pivot_right: int = Field(2, ge=1, le=5, strict=True)
    min_separation: int = Field(5, ge=2, le=60, strict=True)
    max_separation: int = Field(60, ge=5, le=120, strict=True)

    @model_validator(mode="after")
    def ordered(self):
        if self.min_separation > self.max_separation:
            raise ValueError("枢轴最大间隔不能小于最小间隔")
        return self


def _display(value):
    return None if value is None else str(value.quantize(EPSILON))


def momentum(closes):
    """EMA(first-close seed), MACD(12,26,9), Wilder RSI(14)."""
    with localcontext() as precision:
        precision.prec = 40
        fast = slow = closes[0]
        dea = Decimal(0)
        dif_values, dea_values, hist_values = [], [], []
        for close in closes:
            fast += Decimal(2) / 13 * (close - fast)
            slow += Decimal(2) / 27 * (close - slow)
            dif = fast - slow
            dea += Decimal(2) / 10 * (dif - dea)
            dif_values.append(dif)
            dea_values.append(dea)
            # Chinese chart convention; explicitly twice the MACD-signal gap.
            hist_values.append(2 * (dif - dea))
        rsi_values = [None] * len(closes)
        gains, losses = [], []
        gain = loss = Decimal(0)
        for index in range(1, len(closes)):
            change = closes[index] - closes[index - 1]
            up, down = max(change, Decimal(0)), max(-change, Decimal(0))
            if index <= 14:
                gains.append(up)
                losses.append(down)
                if index < 14:
                    continue
                gain, loss = sum(gains) / 14, sum(losses) / 14
            else:
                gain, loss = (gain * 13 + up) / 14, (loss * 13 + down) / 14
            rsi_values[index] = (Decimal(50) if not gain and not loss else Decimal(100)
                                 if not loss else 100 - 100 / (1 + gain / loss))
        return {"macd_dif": dif_values, "macd_dea": dea_values,
                "macd_histogram": hist_values, "rsi_14": rsi_values}


def compare_pivots(bars, indicators, args, *, finality):
    """Compare latest eligible price pivots; never search ahead of the cutoff."""
    comparisons, signals = [], []
    count = len(bars)
    for direction, field in (("bullish", "low"), ("bearish", "high")):
        prices = [Decimal(str(bar[field])) for bar in bars]
        confirmed, pending = [], []
        for index in range(max(WARMUP - 1, args.pivot_left), count):
            neighbors = prices[index - args.pivot_left:index] + prices[index + 1:min(count, index + args.pivot_right + 1)]
            extreme = all(prices[index] < v for v in neighbors) if direction == "bullish" else all(prices[index] > v for v in neighbors)
            if extreme:
                (confirmed if index + args.pivot_right < count else pending).append(index)
        pairs = []
        if confirmed:
            second = confirmed[-1]
            earlier = [i for i in confirmed[:-1] if args.min_separation <= second - i <= args.max_separation]
            if earlier:
                pairs.append((earlier[-1], second, True))
        if pending:
            second = pending[-1]
            earlier = [i for i in confirmed if args.min_separation <= second - i <= args.max_separation]
            if earlier:
                pairs.append((earlier[-1], second, False))
        for first, second, pivot_confirmed in pairs:
            is_bull = direction == "bullish"
            price_extends = prices[second] < prices[first] if is_bull else prices[second] > prices[first]
            hits = []
            for name in ("macd_dif", "macd_histogram", "rsi_14"):
                before, after = indicators[name][first], indicators[name][second]
                improves = after - before > EPSILON if is_bull else before - after > EPSILON
                same_zone = (before < 0 and after < 0 if is_bull else before > 0 and after > 0) if name != "rsi_14" else True
                if price_extends and improves and same_zone:
                    hits.append(name)

            def anchor(index):
                return {"date": bars[index]["date"], "bar_index": index, "price_field": field,
                        "price": str(prices[index]), "close": str(bars[index]["close"]),
                        "indicators": {name: _display(indicators[name][index]) for name in ("macd_dif", "macd_histogram", "rsi_14")}}

            after_second = list(range(second + 1, count))
            invalidation = next((i for i in after_second if (prices[i] < prices[second] if is_bull else prices[i] > prices[second])), None)
            # Price confirmation is separate from pivot confirmation.
            middle = bars[first + 1:second]
            level = (max(Decimal(str(b["high"])) for b in middle) if is_bull
                     else min(Decimal(str(b["low"])) for b in middle))
            breakout = next((i for i in after_second if (Decimal(str(bars[i]["close"])) > level if is_bull
                                                         else Decimal(str(bars[i]["close"])) < level)), None)
            comparison = {"direction": direction, "type": "regular", "first": anchor(first), "second": anchor(second),
                          "separation_bars": second - first, "price_extends": price_extends,
                          "pivot_confirmed": pivot_confirmed,
                          "pivot_confirmation_date": bars[second + args.pivot_right]["date"] if pivot_confirmed else None,
                          "observed_through": bars[-1]["date"], "matched_indicators": hits,
                          "price_confirmation": {"level": str(level), "confirmed": breakout is not None,
                                                 "date": bars[breakout]["date"] if breakout is not None else None},
                          "invalidation_date": bars[invalidation]["date"] if invalidation is not None else None}
            comparisons.append(comparison)
            if hits:
                status = "invalidated" if invalidation is not None else "confirmed" if pivot_confirmed and finality == "final" else "candidate"
                signals.append({**comparison, "status": status})
    return comparisons, signals


def calculate_divergences(args, ctx, store):
    if args.result_id in ctx.invalidated_result_ids:
        raise AgentError("INVALIDATED_EVIDENCE", "行情输入已经失效")
    stock = registered_market_result(args.result_id, "get_market_data", ctx, store)
    if stock.stale:
        raise AgentError("DATA_STALE", "不能用过期行情识别当前结构")
    ctx.bind_symbol(stock.symbol)
    bars, scope = stock.data["bars"], stock.data["scope"]
    target = scope["trade_date"]
    if not bars or bars[-1]["date"] != target:
        raise AgentError("DATA_STALE", "行情没有覆盖目标日")
    if not scope["session_ended"]:
        raise AgentError("SESSION_INCOMPLETE", "日线尚未收盘，不能确认日线结构")
    dates = [bar["date"] for bar in bars]
    if dates != sorted(set(dates)) or any(day > target for day in dates):
        raise AgentError("CONFLICTING_BARS", "日线须按日期唯一递增且不晚于目标日")
    missing = []
    minimum = WARMUP + args.min_separation + args.pivot_right
    if len(bars) < minimum:
        missing.append(f"有效日线不足{minimum}条，不能覆盖指标预热和枢轴间隔")
    calendar = TradingCalendar()
    if any(calendar.session(date.fromisoformat(day))[0].isoformat() != day for day in dates):
        raise AgentError("INVALID_SESSION_BAR", "序列包含非交易日日线")
    if any(calendar.previous(date.fromisoformat(dates[i])).isoformat() != dates[i - 1] for i in range(1, len(dates))):
        missing.append("日线交易日不连续，不能跨停牌或缺失数据识别结构")
    if any(Decimal(str(bar["volume_shares"])) <= 0 for bar in bars):
        missing.append("序列含无成交日，暂停背离识别")
    if stock.data["corporate_actions"]:
        missing.append("窗口内有原始行情标记的公司行动，未复权价格不可直接比较")
    finality = "final" if stock.data.get("complete") else "unknown"
    data = {"kind": "technical_divergence", "timeframe": "daily", "formula_version": FORMULA_VERSION,
            "trade_date": target, "window_start": dates[0], "bar_count": len(bars),
            "input_result_ids": [args.result_id], "parameters": {**args.model_dump(exclude={"result_id"}),
                "macd": [12, 26, 9], "histogram_scale": 2, "rsi_period": 14, "warmup_bars": WARMUP},
            "price_basis": stock.price_basis, "data_finality": finality, "missing": missing,
            "comparisons": [], "signals": [], "outcome": "unavailable", "complete": False,
            "formulas": {"ema": "first close seed; EMA_t=EMA_prev+2/(N+1)*(close_t-EMA_prev)",
                         "macd_dif": "EMA12-EMA26", "macd_dea": "EMA9(DIF), seed=0",
                         "macd_histogram": "2*(DIF-DEA)", "rsi_14": "Wilder SMA seed then (prev*13+change)/14; flat=50, no loss=100",
                         "pairing": "latest eligible price pivots, indicator values on identical pivot dates"},
            "limitations": ["仅筛查当前窗口的最近合格价格枢轴对，不是穷尽历史形态或缠论分型/笔/中枢识别",
                            "枢轴需右侧交易日才能确认，确认时点不得回填到枢轴日",
                            "指标同源于价格，不是独立证据；背离不等于反转，不给执行参数",
                            "EMA起始点和窗口改变会影响指标；不自动修复未知公司行动"]}
    if not missing:
        indicators = momentum([Decimal(str(bar["close"])) for bar in bars])
        comparisons, signals = compare_pivots(bars, indicators, args, finality=finality)
        data.update(comparisons=comparisons, signals=signals, outcome="detected" if signals else "not_detected",
                    latest_indicators={name: _display(values[-1]) for name, values in indicators.items()})
        if finality == "unknown":
            missing.append("供应商日线最终完整状态未知，结构仅为快照候选")
        data["complete"] = not missing
    return Observation(status="partial" if missing else "ok", source="deterministic_technical_divergence",
                       symbol=stock.symbol, market=stock.market, currency=stock.currency,
                       price_basis=stock.price_basis, snapshot_ids=[stock.snapshot_id], data=data)
