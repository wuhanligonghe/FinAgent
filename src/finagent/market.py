"""Calendar-aware closing review. Source finality is never inferred from the clock."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from math import sqrt
from hashlib import sha256
from pathlib import Path

from pydantic import Field

from .config import StrictModel
from .data.evidence import write_json

from .data.contracts import DailyBar, Observation
from .data.symbols import is_index, normalize_symbol, price_unit, volume_unit
from .errors import AgentError
from .runtime import TZ, now


class TradingCalendar:
    def __init__(self):
        try:
            import exchange_calendars as xcals
            # XSHG includes exchange holiday rules; never fall back to weekdays.
            self.calendar = xcals.get_calendar("XSHG")
            self.version = "exchange_calendars/XSHG/" + xcals.__version__
        except ImportError as exc:
            raise AgentError("CALENDAR_UNAVAILABLE", "请安装 finagent[calendar]；不能用周一至周五代替交易日历") from exc

    def session(self, requested: date):
        try:
            session = self.calendar.date_to_session(str(requested), direction="previous")
            close = self.calendar.session_close(session).to_pydatetime().astimezone(TZ)
            return session.date(), close
        except (ValueError, KeyError) as exc:
            raise AgentError("CALENDAR_OUT_OF_RANGE", "目标日期超出已验证日历覆盖，需更新日历") from exc

    def previous(self, day: date):
        return self.session(day - timedelta(days=1))[0]


class MarketImport(StrictModel):
    instrument: str
    benchmark: bool = False
    source: str = Field(min_length=1)
    source_url: str
    fetched_at: datetime
    finality_field: str | None = Field(None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,50}$", description="原始JSON记录中明确表示最终完整状态的布尔字段；没有则保持unknown")
    synthetic: bool = False


def import_market(path: Path, spec: MarketImport, store):
    from urllib.parse import urlparse
    from .data.symbols import normalize_symbol
    parsed = urlparse(spec.source_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise AgentError("INVALID_SOURCE", "行情导入需要不含凭证的公开HTTPS来源")
    if spec.fetched_at.tzinfo is None or spec.fetched_at > now():
        raise AgentError("INVALID_TIME", "获取时间必须带时区且不在未来")
    if spec.benchmark:
        spec.instrument = normalize_symbol(spec.instrument)
        if not is_index(spec.instrument):
            raise AgentError("UNSUPPORTED_BENCHMARK", "基准必须为已支持的指数，代码需明确交易所")
    else:
        spec.instrument = normalize_symbol(spec.instrument)
    content = path.read_bytes()
    rows = json.loads(content)
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise AgentError("INVALID_RESPONSE", "行情原文必须为非空记录数组且不超过10000行")
    bars = []
    for i, row in enumerate(rows):
        bar = DailyBar(date=str(row["day"])[:10], **{k: row[k] for k in ("open", "high", "low", "close")}, volume_shares=row["volume"])
        value = {**bar.model_dump(mode="json"), "locator": f"$[{i}]"}
        # A human manifest cannot manufacture a flag absent from original bytes.
        value["finality"] = "final" if spec.finality_field and row.get(spec.finality_field) is True else "unknown"
        bars.append(value)
    if len({b["date"] for b in bars}) != len(bars):
        raise AgentError("CONFLICTING_BARS", "同日存在重复记录")
    meta = {**spec.model_dump(mode="json"), "encoding": "utf-8", "format": "market_json"}
    raw = store.save_raw(content, meta)
    key = sha256((raw + spec.model_dump_json()).encode()).hexdigest()
    value = Observation(status="partial", source=spec.source, source_url=spec.source_url, fetched_at=spec.fetched_at,
                        market="CN_A", symbol=spec.instrument, currency="CNY", snapshot_id=raw,
                        price_basis="unadjusted", units={"price": price_unit(spec.instrument), "volume_shares": volume_unit(spec.instrument)},
                        data={"bars": bars, "import_metadata": meta}, warnings=["显式本地行情回放；不是在线请求；不证明未记录的历史修订"])
    write_json(store.root / "market_library" / f"{key}.json", value.model_dump(mode="json"))
    return value


def resolve_market_day(requested: date, ctx, calendar=None):
    current = now().astimezone(TZ)
    if requested > current.date():
        raise AgentError("FUTURE_DATE", "不能将未来或截止后的日期作为已完成复盘")
    calendar = calendar or TradingCalendar()
    actual, close = calendar.session(requested)
    if actual > ctx.as_of.astimezone(TZ).date():
        raise AgentError("FUTURE_DATE", "目标交易日晚于截止日期")
    fixed = ctx.scope_constraints.get("requested_trade_date")
    if fixed and calendar.session(date.fromisoformat(fixed))[0] != actual:
        raise AgentError("SCOPE_MISMATCH", "目标交易日与宿主本轮解析不一致")
    return {"requested_date": requested.isoformat(), "trade_date": actual.isoformat(),
            "market_timezone": "Asia/Shanghai", "calendar_version": calendar.version,
            "session_close": close.isoformat(), "session_ended": close <= min(current, ctx.as_of),
            "holiday_fallback": actual != requested}


def get_market(service, ctx, requested, window=120, benchmark=False, mode="online", benchmark_symbol="000300.SH"):
    calendar = TradingCalendar()
    scope = resolve_market_day(requested, ctx, calendar)
    day = date.fromisoformat(scope["trade_date"])
    if not benchmark and not ctx.symbol:
        raise AgentError("NEEDS_CLARIFICATION", "请先确认公司")
    instrument = normalize_symbol(benchmark_symbol if benchmark else ctx.symbol)
    if benchmark and not is_index(instrument):
        raise AgentError("UNSUPPORTED_BENCHMARK", "基准必须为已支持的指数，代码需明确交易所")
    if not benchmark:
        ctx.bind_symbol(instrument)
    if mode == "replay":
        candidates = []
        for path in (service.store.root / "market_library").glob("*.json"):
            item = Observation.model_validate_json(path.read_text(encoding="utf-8"))
            if item.symbol == instrument and service.store.has_raw(item.snapshot_id):
                candidates.append(item)
        if not candidates:
            raise AgentError("NO_REPLAY_DATA", "该证券没有显式导入的行情快照")
        value = max(candidates, key=lambda x: x.fetched_at)
    else:
        value = service.providers["sina"].daily(instrument, ctx)
    rows = [r for r in value.data["bars"] if r["date"] <= str(day)]
    dates = [r["date"] for r in rows]
    if len(dates) != len(set(dates)):
        raise AgentError("CONFLICTING_BARS", "同日重复价格条，无法确认版本")
    rows.sort(key=lambda b: b["date"])
    for row in rows:
        if calendar.session(date.fromisoformat(row["date"]))[0].isoformat() != row["date"]:
            raise AgentError("INVALID_SESSION_BAR", "来源包含非交易日价格条")
    rows = rows[-(window + 1):]
    latest = rows[-1]["date"] if rows else None
    complete = scope["session_ended"] and latest == str(day) and rows[-1].get("finality") == "final"
    value.data = {"kind": "benchmark_series" if benchmark else "market_series", "bars": rows, "instrument": instrument,
                  "scope": scope, "window": window, "latest_available_date": latest, "complete": complete,
                  "mode": mode,
                  "finality": "final" if complete else "unknown", "stale_sessions": latest != str(day)}
    value.status = "ok" if complete else "partial" if rows else "empty"
    value.data_time = latest
    if not complete:
        value.warnings.append("来源最终完整状态未知或交易未结束，收盘复盘保持部分完成")
    if is_index(instrument):
        value.warnings.append("指数成交量保留供应商定义，不与股票成交量比较")
    ctx.evidence_ids.update([value.snapshot_id])
    ctx.scope.update(scope)
    return value


def registered_market_result(key, tool, ctx, store):
    """Validate a registered series against original OHLCV and session metadata."""
    if key not in ctx.evidence_ids or ctx.result_tools.get(key) != tool:
        raise AgentError("EVIDENCE_DENIED", "指标输入必须为本任务登记的证券/基准序列")
    value = store.load_result(key)
    if value.status not in ("ok", "partial") or not store.has_raw(value.snapshot_id):
        raise AgentError("INVALID_CALCULATION_INPUT", "缺少原始行情证据")
    if tool == "get_market_data" and (not value.symbol or value.data.get("instrument") != value.symbol):
        raise AgentError("IDENTITY_MISMATCH", "证券序列标识与结果中的证券不一致")
    expected_price_unit = price_unit(value.symbol)
    if value.market != "CN_A" or value.currency != "CNY" or value.price_basis != "unadjusted" or value.units.get("price") != expected_price_unit:
        raise AgentError("DATA_CONFLICT", "行情币种、单位、市场或价格口径不一致")
    if tool == "get_benchmark_data" and (not is_index(value.symbol) or value.data.get("instrument") != value.symbol):
        raise AgentError("SCOPE_MISMATCH", "基准不是已支持的指数或证券标识不一致")
    # Check every OHLCV operand against original raw fields.
    raw = json.loads((store.root / "evidence" / f"{value.snapshot_id}.bin").read_bytes())
    original = {str(r["day"])[:10]: r for r in raw}
    metadata = json.loads((store.root / "evidence" / f"{value.snapshot_id}.json").read_text(encoding="utf-8"))
    if metadata.get("source") != value.source:
        raise AgentError("SOURCE_EVIDENCE_MISMATCH", "规范来源与原始记录不同")
    if metadata.get("instrument", value.symbol) != value.symbol:
        raise AgentError("IDENTITY_MISMATCH", "行情结果证券与原始导入记录不一致")
    scope = value.data["scope"]
    checked_scope = resolve_market_day(date.fromisoformat(scope["requested_date"]), ctx)
    if any(scope.get(k) != checked_scope[k] for k in ("trade_date", "session_close", "session_ended")):
        raise AgentError("SESSION_EVIDENCE_MISMATCH", "行情交易日/收盘状态与日历及截止时间不一致")
    if value.data.get("complete"):
        field = metadata.get("finality_field")
        if not field or original.get(scope["trade_date"], {}).get(field) is not True:
            raise AgentError("FINALITY_EVIDENCE_MISMATCH", "原始供应商记录没有最终完整标记")
    for row in value.data["bars"]:
        source = original.get(row["date"], {})
        for field in ("open", "high", "low", "close", "volume_shares"):
            if Decimal(str(row[field])) != Decimal(str(source.get("volume" if field == "volume_shares" else field))):
                raise AgentError("PRICE_EVIDENCE_MISMATCH", "行情操作数与原始字段不一致")
    # Only explicit flags in the original market response may supply this context.
    # Retired local disclosure documents are no longer consulted.
    value.data["corporate_actions"] = [row["date"] for row in value.data["bars"]
        if original.get(row["date"], {}).get("corporate_action") is True]
    return value


def review_metrics(args, ctx, store):

    stock = registered_market_result(args.result_id, "get_market_data", ctx, store)
    ctx.bind_symbol(stock.symbol)
    bars, scope = stock.data["bars"], stock.data["scope"]
    target = scope["trade_date"]
    if not bars or bars[-1]["date"] != target:
        raise AgentError("DATA_STALE", "没有目标交易日日线，不能用旧数据冒充")
    if not scope["session_ended"]:
        raise AgentError("SESSION_INCOMPLETE", "市场尚未收盘")
    calendar = TradingCalendar()
    previous_date = calendar.previous(date.fromisoformat(target)).isoformat()
    if len(bars) < 2 or bars[-2]["date"] != previous_date:
        raise AgentError("MISSING_PREVIOUS_CLOSE", "缺少交易日历规定的上一交易日价格，可能停牌或覆盖不足")
    current, previous = (Decimal(str(b["close"])) for b in bars[-1:-3:-1])
    percent = (current / previous - 1) * 100
    missing, warnings = [], []
    # Require contiguous exchange sessions; a suspended stock is not a normal day.
    contiguous = all(calendar.previous(date.fromisoformat(bars[i]["date"])).isoformat() == bars[i-1]["date"] for i in range(1, len(bars)))
    actions = stock.data["corporate_actions"]
    warnings.append("未接完整公司行动数据；未发现标记不代表期间没有除权除息")
    metrics = {"change_amount": str(current - previous), "change_percent": str(percent), "moving_averages": {}, "price_vs_ma_percent": {},
               "volume_multiple_previous_20": None, "benchmark_difference_pp": None, "volatility_20_daily_std_percent": None}
    if bars[-1]["volume_shares"] <= 0:
        raise AgentError("SUSPENDED_OR_INCOMPLETE", "目标日无成交量，不当作正常交易日")
    if actions or not contiguous:
        warnings.append("CORPORATE_ACTION_CONTEXT" if actions else "历史序列存在交易日缺口")
        missing.append("可比且连续的历史价格；暂停均线/波动与趋势")
    else:
        for count in (5, 20, 60):
            if len(bars) >= count:
                average = sum(Decimal(str(b["close"])) for b in bars[-count:]) / count
                metrics["moving_averages"][str(count)] = str(average)
                metrics["price_vs_ma_percent"][str(count)] = str((current / average - 1) * 100)
            else:
                missing.append(f"MA{count}历史长度不足")
        if len(bars) >= 21:
            volumes = [Decimal(str(b["volume_shares"])) for b in bars[-21:-1]]
            mean = sum(volumes) / 20
            if mean > 0:
                metrics["volume_multiple_previous_20"] = str(Decimal(str(bars[-1]["volume_shares"])) / mean)
            returns = [(Decimal(str(bars[i]["close"])) / Decimal(str(bars[i-1]["close"])) - 1) * 100 for i in range(len(bars)-20, len(bars))]
            avg = sum(returns) / len(returns)
            metrics["volatility_20_daily_std_percent"] = sqrt(float(sum((r-avg)**2 for r in returns) / (len(returns)-1)))
        else:
            missing.append("前20个已完成交易日成交量/收益")
    inputs = [args.result_id]
    snapshots = [stock.snapshot_id]
    if args.benchmark_result_id:
        benchmark = registered_market_result(args.benchmark_result_id, "get_benchmark_data", ctx, store)
        if benchmark.data["scope"]["trade_date"] != target or benchmark.price_basis != stock.price_basis or benchmark.currency != stock.currency:
            raise AgentError("DATA_CONFLICT", "证券与基准日期或价格口径不一致")
        mapping = {b["date"]: b for b in benchmark.data["bars"]}
        if not actions and target in mapping and previous_date in mapping:
            base = (Decimal(str(mapping[target]["close"])) / Decimal(str(mapping[previous_date]["close"])) - 1) * 100
            metrics["benchmark_change_percent"] = str(base)
            metrics["benchmark_difference_pp"] = str(percent - base)
        else:
            missing.append("可比基准日期/公司行动背景")
        inputs.append(args.benchmark_result_id)
        snapshots.append(benchmark.snapshot_id)
        if not benchmark.data["complete"]:
            missing.append("基准供应商最终完整标记")
    else:
        missing.append("市场基准")
    if actions:
        metrics["change_amount"] = metrics["change_percent"] = None
        missing.append("公司行动影响下的可比涨跌幅")
    if not stock.data["complete"]:
        missing.append("供应商最终完整标记")
    return Observation(status="partial" if missing else "ok", source="deterministic_market_review", symbol=stock.symbol, market="CN_A",
                       currency=stock.currency, price_basis=stock.price_basis, snapshot_ids=snapshots, warnings=warnings,
                       units={"price": price_unit(stock.symbol), "change_amount": price_unit(stock.symbol), "change_percent": "%"},
                       data={"kind": "closing_review", "trade_date": target, "scope": scope, "metrics": metrics,
                             "missing": missing, "complete": not missing, "input_result_ids": inputs,
                             "formulas": {"change_percent": "(close / same-source previous close - 1) * 100",
                                          "moving_averages": "mean(last N closes, including target)",
                                          "volume_multiple_previous_20": "target volume / mean(previous 20 completed sessions)",
                                          "benchmark_difference_pp": "stock change_percent - benchmark change_percent"},
                             "note": "成交量倍数不称供应商量比；价格收益不含分红；趋势不代表未来预测"})
