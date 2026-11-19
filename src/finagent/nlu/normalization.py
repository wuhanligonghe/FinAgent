"""Only normalize model-marked spans; never scan the complete request for intent."""
from datetime import datetime, date, time, timedelta
import copy
import re
from ..runtime import TZ, now
from ..errors import AgentError


def number(text):
    if text.isdecimal():
        return int(text)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits:
        return digits[text]
    if "十" in text and text.count("十") == 1:
        a, b = text.split("十")
        return (digits[a] if a else 1) * 10 + (digits[b] if b else 0)
    raise AgentError("NEEDS_CLARIFICATION", "无法确定时间表达中的数量")


def date_value(text, anchor):
    value = text.strip()
    if value in ("今天", "今日"):
        return anchor.date()
    if value in ("昨天", "昨日"):
        return anchor.date() - timedelta(days=1)
    value = value.removeprefix("截至到").removeprefix("截止到").removeprefix("截至").removeprefix("截止").strip()
    match = re.fullmatch(r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})日?", value)
    if not match:
        raise AgentError("NEEDS_CLARIFICATION", "请明确日期，建议使用 YYYY-MM-DD")
    try:
        return date(*map(int, match.groups()))
    except ValueError as exc:
        raise AgentError("INVALID_CUTOFF", "日期无效") from exc


def normalize(parsed, *, as_of=None, inherited=None):
    anchor = now()
    explicit = datetime.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    if explicit is not None and (explicit.tzinfo is None or explicit > anchor):
        raise AgentError("INVALID_CUTOFF", "截止时间必须带时区且不晚于当前时间")
    cutoff = explicit or anchor
    cutoff_source = "explicit" if explicit else "default"
    scope = copy.deepcopy(inherited or {}) if parsed.dialogue_act == "follow_up" else {}
    scope.pop("research_id", None)
    scope.pop("nlu", None)
    constraints, sources = {}, {}
    news_days, policy_days, horizon = 7, 90, "未来 1—3 个月"
    entities = parsed.entities
    cutoffs = [e for e in entities if e.type == "TIME_CUTOFF"]
    if len(cutoffs) > 1:
        raise AgentError("NEEDS_CLARIFICATION", "存在多个证据截止时间，请明确其中一个")
    if cutoffs:
        day = date_value(cutoffs[0].text, anchor)
        if day > anchor.date():
            raise AgentError("INVALID_CUTOFF", "证据截止不能晚于当前时间")
        value = min(datetime.combine(day, time(23, 59, 59), TZ), anchor)
        if explicit and explicit.astimezone(TZ).date() != day:
            raise AgentError("SCOPE_MISMATCH", "显式截止时间与正文中的截止时间冲突")
        cutoff, cutoff_source = explicit or value, "explicit"
    windows = [e.text for e in entities if e.type == "LOOKBACK_WINDOW"]
    if len(set(windows)) > 1:
        raise AgentError("NEEDS_CLARIFICATION", "存在多个检索窗口，请明确各自对应的维度")
    if windows and windows[0] not in ("最近", "近期", "近来"):
        match = re.fullmatch(r"(?:最近|过去|近)?\s*([一二两三四五六七八九十零\d]+)\s*(?:个)?(天|日|周|星期|月)", windows[0])
        if not match:
            raise AgentError("NEEDS_CLARIFICATION", "请用最近几天或几周描述检索窗口")
        count = number(match[1]) * {"天": 1, "日": 1, "周": 7, "星期": 7, "月": 30}[match[2]]
        if count < 1:
            raise AgentError("NEEDS_CLARIFICATION", "检索窗口必须大于0")
        if parsed.dimension_state["news"] == "include":
            if count > 30:
                raise AgentError("NEEDS_CLARIFICATION", "新闻窗口最多30天，请缩小范围")
            news_days = count
        if parsed.dimension_state["policy"] == "include":
            if count > 365:
                raise AgentError("NEEDS_CLARIFICATION", "政策窗口最多365天，请缩小范围")
            policy_days = count
        scope["lookback_days"] = count; sources["lookback_days"] = "explicit"
    for e in entities:
        if e.type == "DECISION_HORIZON":
            horizon = e.text; sources["decision_horizon"] = "explicit"
        elif e.type == "DISPLAY_UNIT":
            if e.text not in ("元", "万元", "亿元"):
                raise AgentError("NEEDS_CLARIFICATION", "显示单位暂支持元、万元和亿元")
            scope.setdefault("display_preferences", {})["display_unit"] = e.text
        elif e.type == "FINANCIAL_METRIC":
            maps = {"归母净利润": "attributable_net_profit", "归属于母公司股东的净利润": "attributable_net_profit", "合并净利润": "net_profit", "全部净利润": "net_profit"}
            if e.text in maps:
                constraints.setdefault("profit_metrics", []).append(maps[e.text])
                scope["metric"] = e.text
        elif e.type == "TRADE_DATE":
            day = date_value(e.text, anchor)
            if day > anchor.date():
                raise AgentError("INVALID_CUTOFF", "交易日期不能晚于当前日期")
            scope["trade_date"] = day.isoformat(); sources["trade_date"] = "explicit"
            constraints["requested_trade_date"] = day.isoformat()
            if not explicit and not cutoffs and day < anchor.date():
                from ..market import TradingCalendar
                actual, _ = TradingCalendar().session(day)
                cutoff = datetime.combine(actual, time(23, 59, 59), TZ)
                cutoff_source = "explicit"
    periods = []
    for e in entities:
        if e.type != "REPORT_PERIOD":
            continue
        match = re.fullmatch(r"(20\d{2}|去年|上一年|上年|今年)(?:年)?(年报|年度|半年报|半年度|一季报|三季报)?", e.text)
        if not match:
            raise AgentError("NEEDS_CLARIFICATION", "无法确定财报期间，请明确年度及季度")
        raw_year, period = match.groups()
        previous = scope.get("report_period")
        year = (int(previous[:4]) - 1 if raw_year in ("上一年", "上年") and previous else
                int(raw_year) if raw_year.isdecimal() else (cutoff.year if raw_year == "今年" else cutoff.year - 1))
        suffix = (previous[4:] if period is None and previous and raw_year in ("上一年", "上年") else
                  {None: "-12-31", "年报": "-12-31", "年度": "-12-31", "半年报": "-06-30", "半年度": "-06-30", "一季报": "-03-31", "三季报": "-09-30"}[period])
        periods.append(str(year) + suffix)
    if periods:
        scope["report_period"] = periods[0]; sources["report_period"] = "explicit"
    elif scope.get("report_period"):
        periods = [scope["report_period"]]; sources["report_period"] = "context"
    comparisons = [e.text for e in entities if e.type == "COMPARISON_MODE"]
    if comparisons:
        if not periods:
            raise AgentError("NEEDS_CLARIFICATION", "比较财务指标前请明确财报期间")
        for value in comparisons:
            if value in ("同比", "与上一年比较", "较上年同期"):
                current = date.fromisoformat(periods[0])
                periods.append(current.replace(year=current.year - 1, day=min(current.day, 28) if current.month == 2 else current.day).isoformat())
            elif value == "环比":
                raise AgentError("NEEDS_CLARIFICATION", "请明确环比的可比期间及单季／累计口径")
            else:
                raise AgentError("NEEDS_CLARIFICATION", "请明确比较方式")
    if periods:
        constraints.update(report_period=periods[0], allowed_report_periods=sorted(set(periods)))
    return {"as_of": cutoff, "cutoff_explicit": cutoff_source == "explicit", "news_days": news_days, "policy_days": policy_days,
            "horizon": horizon, "scope": scope, "constraints": constraints,
            "sources": {"as_of": cutoff_source, "news_days": "explicit" if news_days != 7 else "default", "policy_days": "explicit" if policy_days != 90 else "default", **sources}}
