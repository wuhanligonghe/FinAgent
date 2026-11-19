"""Deterministic final-answer checks against registered structured observations.

No model judgment, new network request, global number bag, or inferred causality.
Supported claims are bound to entity, metric, period, unit and source locator.
Unparsed prose is explicitly outside the coverage of these rules.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from importlib.resources import files
import json
import re


ALIASES = {
    "price": ("最新价", "现价", "股价", "价格", "报价", "点位", "报"),
    "previous_close": ("前收盘价", "前收", "昨收", "昨日收盘价"),
    "open": ("开盘价", "开盘"), "high": ("最高价", "最高"), "low": ("最低价", "最低"),
    "close": ("收盘价", "收盘"), "volume_shares": ("成交量",), "amount_cny": ("成交额",),
    "change_percent": ("涨跌幅", "涨幅", "跌幅"), "change_amount": ("涨跌额", "涨跌", "上涨", "下跌"),
    "revenue": ("营业收入", "营收"), "attributable_net_profit": ("归母净利润", "归母净利"),
    "deducted_attributable_net_profit": ("扣非归母净利润", "扣非净利润", "扣非净利"),
    "net_profit": ("净利润", "净利"), "operating_profit": ("营业利润",),
    "operating_cashflow": ("经营活动现金流净额", "经营活动产生的现金流量净额", "经营现金流"),
    "total_assets": ("资产总计", "总资产"), "total_liabilities": ("负债合计", "总负债"),
    "pe_ttm": ("市盈率", "PE"), "pb": ("市净率", "PB"),
    "calculation:growth": ("同比增长率", "同比增长", "同比", "增长率"),
    "calculation:net_margin": ("净利率",), "calculation:operating_margin": ("营业利润率",),
    "calculation:debt_ratio": ("资产负债率",),
    "ma5": ("MA5", "五日均线", "5日均线"), "ma20": ("MA20", "二十日均线", "20日均线"),
    "ma60": ("MA60", "六十日均线", "60日均线"),
    "data_time": ("行情时间", "数据时间", "报价时间"),
    "fetched_at": ("抓取时间", "获取时间", "检索时间"),
    "published_at": ("披露时间", "发布时间", "发布日期", "披露日期", "披露", "发布于"), "report_period": ("报告期",),
}
METRIC = re.compile("|".join(re.escape(a) for a in sorted((a for v in ALIASES.values() for a in v), key=len, reverse=True)),re.I)
CANONICAL = {alias: key for key, aliases in ALIASES.items() for alias in aliases}
NUMBER = re.compile(r"(?<![A-Za-z0-9_.])(?P<n>[+-]?\d[\d,]*(?:\.\d+)?)(?:\s*)(?P<u>亿元|百万元|万元|千元|万股|亿股|元(?:/股)?|美元|港元|USD|HKD|股|%|％|个百分点|倍|点)?")
DATE = re.compile(r"(?<!\d)(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?")
TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![:\d])")
CODE = re.compile(r"(?<!\d)(\d{6})\.(SH|SZ)(?!\w)|[（(](\d{6})[）)]", re.I)
SCALE = {"亿元": Decimal("1e8"), "百万元": Decimal("1e6"), "万元": Decimal("1e4"),
         "千元": Decimal("1e3"), "万股": Decimal("1e4"), "亿股": Decimal("1e8")}
MONEY = {"price", "previous_close", "open", "high", "low", "close", "change_amount", "revenue",
         "attributable_net_profit", "deducted_attributable_net_profit", "net_profit", "operating_profit",
         "operating_cashflow", "total_assets", "total_liabilities", "amount_cny"}
ALLOWED_TOOLS = {"get_quote", "get_daily_bars", "get_market_data", "get_benchmark_data", "get_financial_statements",
    "execute_program", "calculate_market_metrics",
    "calculate_closing_review", "calculate_technical_divergence", "get_stock_news",
    "get_fund_flow", "get_market_attention", "get_macro_news", "get_company_profile", "get_sector_ranking",
    "search_policy_documents", "read_policy_document", "calculate_news_tone"}


@dataclass
class Fact:
    result_id: str
    locator: str
    symbol: str | None
    field: str
    value: str
    unit: str = ""
    period: str | None = None


def metric(text):
    matches = list(METRIC.finditer(text))
    return CANONICAL.get(matches[-1][0],CANONICAL.get(matches[-1][0].upper())) if matches else None


def date_value(text):
    match = DATE.search(str(text))
    if not match:
        return None
    try:
        y,m,d,h,minute,second = match.groups()
        stamp = datetime(int(y),int(m),int(d),int(h or 0),int(minute or 0),int(second or 0))
        return stamp.strftime("%Y-%m-%d") + (stamp.strftime(" %H:%M:%S") if h else "")
    except ValueError:
        return None


def unit_for(field):
    return "元" if field in MONEY or field.startswith("ma") else "%" if field.endswith(("percent", "_pct")) else "股" if field == "volume_shares" else "倍" if field in {"pb", "pe_ttm"} else ""


def evidence_index(ctx, store):
    facts, names, unavailable = [], {}, []
    def add(result_id, path, symbol, field, value, period=None, unit=""):
        if value is None or isinstance(value, (dict,list,bool)):
            return
        try:
            number = Decimal(str(value).replace(",", ""))
            if not number.is_finite(): return
            from .data.symbols import is_index
            index_price = symbol and is_index(symbol) and field in {"price","previous_close","open","high","low","close","change_amount","ma5","ma20","ma60"}
            inferred = "点" if index_price else unit_for(field)
            if index_price:unit="点"
            facts.append(Fact(result_id,path,symbol,field,str(number),unit or inferred,period))
        except InvalidOperation:
            stamp = date_value(value)
            if stamp: facts.append(Fact(result_id,path,symbol,field,stamp,"date",period))

    def walk(value, result_id, symbol, path="/data", period=None, depth=0, currency=None):
        if depth>14 or len(facts)>20000: return
        if isinstance(value,list):
            for i,item in enumerate(value):walk(item,result_id,symbol,path+"/"+str(i),period,depth+1,currency)
        elif isinstance(value,dict):
            period = value.get("period_end") or value.get("report_period") or value.get("date") or period
            currency=value.get("currency") or currency
            field = value.get("metric")
            if field and "normalized_value" in value:
                add(result_id,path+"/normalized_value",symbol,field,value["normalized_value"],period,
                    "元" if value.get("currency") == "CNY" else value.get("currency", ""))
            for key,item in value.items():
                if key in {"raw_fields","raw_value","program","operands","step_values","counts","percentages"}:
                    continue
                if key == "moving_averages" and isinstance(item,dict):
                    for days,number in item.items():add(result_id,path+"/moving_averages/"+days,symbol,"ma"+days,number,period,"元" if currency=="CNY" else currency or "unknown_currency")
                if key in ALIASES or key in {"published_at","available_at","trade_date","date","report_period"}:
                    scalar = item.get("value") if isinstance(item,dict) else item
                    unit=("元" if currency=="CNY" else currency or "unknown_currency") if key in MONEY else ""
                    add(result_id,path+"/"+key,symbol,key,scalar,period,unit)
                elif key in {"value","display_value"} and value.get("kind") == "financial_calculation":
                    add(result_id,path+"/"+key,symbol,"calculation:"+value.get("purpose","arithmetic"),item,period,value.get("unit",""))
                walk(item,result_id,symbol,path+"/"+key,period,depth+1,currency)

    for result_id, tool in ctx.result_tools.items():
        if tool not in ALLOWED_TOOLS:continue
        try: observation = store.load_result(result_id)
        except Exception:
            unavailable.append(result_id);continue
        snapshots = set(observation.snapshot_ids) | ({observation.snapshot_id} if observation.snapshot_id else set())
        if (observation.status not in {"ok","partial"} or observation.stale or result_id in ctx.invalidated_result_ids
                or not snapshots or not snapshots <= ctx.evidence_ids or not all(store.has_raw(s) for s in snapshots)):
            unavailable.append(result_id);continue
        if observation.data_time:
            try:
                stamp = datetime.fromisoformat(observation.data_time)
                if stamp.tzinfo is None:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", observation.data_time):
                        unavailable.append(result_id);continue
                    after_cutoff = stamp.date() > ctx.as_of.date()
                else:
                    after_cutoff = stamp > ctx.as_of
                if after_cutoff:
                    unavailable.append(result_id);continue
            except (ValueError,TypeError):
                unavailable.append(result_id);continue
        symbol=observation.symbol
        if observation.data.get("name") and symbol:names[observation.data["name"]]=symbol
        add(result_id,"/data_time",symbol,"data_time",observation.data_time)
        if observation.fetched_at:add(result_id,"/fetched_at",symbol,"fetched_at",observation.fetched_at.isoformat())
        currency=observation.currency or ("CNY" if any("CNY" in u for u in observation.units.values()) else None)
        walk(observation.data,result_id,symbol,currency=currency)
    return facts,names,unavailable


def inspect_answer(answer, ctx, store, state, extracted_claims=None):
    facts, names, unavailable = evidence_index(ctx,store)
    catalog=json.loads(files("finagent").joinpath("resources/securities.json").read_text())
    from .data.symbols import INDICES
    for row in [*catalog,*[{"symbol":s,"name":v["name"],"aliases":v.get("aliases",[])} for s,v in INDICES.items()]]:
        for name in [row["name"],*row.get("aliases",[])]:
            if name:names.setdefault(name,row["symbol"])
    # Longer aliases win over contained aliases, e.g. 格力电器 vs 格力.
    name_pattern=re.compile("|".join(re.escape(n) for n in sorted(names,key=len,reverse=True))) if names else None
    supported_symbols={f.symbol for f in facts if f.symbol}
    default_symbol=ctx.symbol if len(supported_symbols)<=1 else None
    issues,checked,unverified=[],[],[]
    heading_symbol=None
    header=[]

    def symbols(text):
        return {names[m[0]] for m in name_pattern.finditer(text)} if name_pattern else set()

    def failure(kind,text,**details):
        issues.append({"kind":kind,"text":text[:200],**details})

    def support(text,field,symbol,period,value,unit,scale=Decimal(1)):
        if field is None:
            failure("METRIC_NOT_IDENTIFIED",text);return
        pool=[f for f in facts if (symbol is None or f.symbol==symbol) and (not field or f.field==field)
              and (not period or f.period and f.period.startswith(period))]
        if field and field in MONEY and unit not in {"元","元/股","万元","亿元","千元","百万元","点","美元","USD","港元","HKD"}:
            pool=[]
        elif unit in {"%","％"}:pool=[f for f in pool if f.unit in {"%","percent"}]
        elif unit == "个百分点":pool=[f for f in pool if f.unit == "percentage_points"]
        elif unit in {"股","万股","亿股"}:pool=[f for f in pool if f.unit=="股"]
        elif unit == "点":pool=[f for f in pool if f.unit=="点"]
        elif unit in {"美元","USD"}:pool=[f for f in pool if f.unit=="USD"]
        elif unit in {"港元","HKD"}:pool=[f for f in pool if f.unit=="HKD"]
        elif unit in {"元","元/股","万元","亿元","千元","百万元"}:pool=[f for f in pool if f.unit in {"元","CNY"}]
        if symbol is None and len({f.symbol for f in pool if f.symbol})>1:
            failure("AMBIGUOUS_ENTITY",text);return
        claim=Decimal(value.replace(",",""))
        quantum=Decimal(1).scaleb(claim.as_tuple().exponent)
        matched=[]
        for f in pool:
            if f.unit=="date":continue
            try:
                if (Decimal(f.value)/scale).quantize(quantum,rounding=ROUND_HALF_UP)==claim:matched.append(f)
            except InvalidOperation:pass
        if matched:
            checked.append({"text":text[:200],"field":field,"symbol":symbol,"sources":[asdict(f) for f in matched[:3]]})
        else:
            failure("NUMBER_NOT_SUPPORTED",text,field=field,symbol=symbol,period=period,
                    expected=[f.value for f in pool[:5] if f.unit!="date"])

    analysis_text=re.sub(r"((?:按|根据)你提供的(?:信息|数据|数字|资料)|你(?:提到|说))[，,]\s*",r"\1",answer)
    for raw in re.split(r"\n|(?<=[。！？；;])\s*|(?<!\d)[，,](?!\d)|(?:但是|但|然而|不过)",analysis_text):
        line=re.sub(r"[*`]","",raw).strip()
        if not line:continue
        uncertainty=re.search(r"不知道|无法确认|不能确认|未核实|没有证据|并非|不是",line)
        first_number=NUMBER.search(line)
        if (uncertainty and (first_number is None or uncertainty.start()<first_number.start())
                and not re.search(r"不是没有|并非没有|不是不能|并非不知道",line)):
            unverified.append({"text":line[:200],"reason":"explicitly_not_asserted_as_fact"});continue
        if re.search(r"^(?:如果|假设|例如|举例|若(?=股价|价格|指数|未来|出现|发生|跌|涨|在|以|突破|回落|能|有|无|开盘)).{0,100}(?:\d|价格|股价)",line):
            unverified.append({"text":line[:200],"reason":"hypothetical_not_observed_fact"});continue
        if re.match(r"^(?:按|根据)你提供|^你(?:提到|说)",line):
            user=(state.get("user_updates") or [{}])[-1].get("text","")
            numbers=list(NUMBER.finditer(line))
            if numbers and all(n[0].strip() in user for n in numbers):
                unverified.append({"text":line[:200],"reason":"attributed_user_statement_not_external_verification"});continue
        entity_set=symbols(line)
        if len(entity_set)==1:heading_symbol=next(iter(entity_set))
        elif len(entity_set)>1:heading_symbol=None
        if re.match(r"^#{1,6}\s",line) and not entity_set:heading_symbol=None
        codes=list(CODE.finditer(line))
        for code in codes:
            canonical=(code[1]+"."+code[2].upper()) if code[1] else None
            bound=symbols(line[max(0,code.start()-35):code.start()])
            if len(bound)==1 and ((canonical and canonical not in bound) or (not canonical and not any(s.startswith(code[3]+".") for s in bound))):
                failure("ENTITY_CODE_MISMATCH",code[0],names=sorted(bound))
            elif canonical and canonical not in supported_symbols and canonical not in ctx.queried_symbols:
                unverified.append({"text":code[0],"reason":"identity_only_no_observation"})
        cells=[c.strip() for c in line.strip("|").split("|")] if "|" in line else []
        if cells and all(re.fullmatch(r":?-+:?",c.replace(" ","")) for c in cells if c):continue
        if cells and not any(re.search(r"\d",c) for c in cells[1:]):header=cells
        if cells and any(re.fullmatch(r"20\d{2}年?",c) for c in cells[1:]):header=cells;continue
        date_spans=[]
        for d in DATE.finditer(line):
            date_spans.append(d.span())
            value=date_value(d[0])
            if value and d[4] and not d[6]:value=value[:-3]
            hint=metric(line[:d.start()])
            hint=hint or metric(line[d.end():d.end()+12])
            hint=hint if hint in {"data_time","fetched_at","published_at","report_period"} else None
            date_symbol=next(iter(entity_set)) if len(entity_set)==1 else heading_symbol or default_symbol
            pool=[f for f in facts if f.unit=="date" and (not hint or f.field==hint)
                  and (not date_symbol or f.symbol==date_symbol)]
            if value and any(f.value.startswith(value) for f in pool):
                checked.append({"text":d[0],"field":hint or "date","sources":[asdict(f) for f in pool if f.value.startswith(value)][:3]})
            elif re.search(r"截至|截止|查询|预测|范围|是否|确认",line[:d.start()]) and any(d[0] in u["text"] for u in state.get("user_updates",[])):
                unverified.append({"text":d[0],"reason":"user_requested_scope_not_observed_date"})
            else:failure("DATE_NOT_SUPPORTED",d[0],field=hint)
        for t in TIME.finditer(line):
            if any(a<=t.start()<b for a,b in date_spans):continue
            stamp=f"{int(t[1]):02d}:{t[2]}"+(":"+t[3] if t[3] else "")
            hint=metric(line[:t.start()]);hint=hint if hint in {"data_time","fetched_at","published_at"} else None
            date_symbol=next(iter(entity_set)) if len(entity_set)==1 else heading_symbol or default_symbol
            pool=[f for f in facts if f.unit=="date" and (not hint or f.field==hint) and (not date_symbol or f.symbol==date_symbol)]
            if not any(stamp in f.value for f in pool):failure("TIME_NOT_SUPPORTED",t[0],field=hint)
            else:checked.append({"text":t[0],"field":hint or "time","sources":[asdict(f) for f in pool if stamp in f.value][:3]})
            date_spans.append(t.span())
        for number in NUMBER.finditer(line):
            if any(a<=number.start()<b for a,b in date_spans) or any(c.start()<=number.start()<c.end() for c in codes):continue
            prefix=line[:number.start()]
            field=metric(prefix)
            unit=number["u"]
            column=prefix.count("|")-(1 if line.startswith("|") else 0)
            header_cell=header[column] if cells and 0<=column<len(header) else ""
            field=field or metric(header_cell)
            if not unit and not (cells and field in ALIASES and field not in {"data_time","fetched_at","published_at","report_period"}):continue
            if not unit:unit=unit_for(field)
            if not unit:continue
            value=number["n"]
            suffix_field=metric(line[number.end():number.end()+10])
            if unit in {"%","％","个百分点"} and suffix_field in {"change_percent","calculation:growth","calculation:net_margin","calculation:operating_margin","calculation:debt_ratio"}:
                field=suffix_field
            if field=="change_amount" and unit in {"%","％"}:field="change_percent"
            if field in {"change_amount","change_percent"} and re.search(r"(?:下跌|跌幅)(?:为|是|约|：|:|\s)*$",prefix) and Decimal(value.replace(",",""))>0:
                value="-"+value.lstrip("+")
            scope_symbols=symbols(header_cell) or entity_set
            symbol=next(iter(scope_symbols)) if len(scope_symbols)==1 else heading_symbol or default_symbol
            if not scope_symbols and re.search(r"[\u4e00-\u9fffA-Za-z]{2,16}(?:控股|集团|银行|科技|股份|公司)",prefix):
                failure("ENTITY_NOT_BOUND_TO_SOURCE",number[0]);continue
            years=re.findall(r"(20\d{2})年",header_cell or prefix)
            period=years[-1] if years else None
            # A last/current share price has no annual financial report period.
            if field in {"price","previous_close","open","high","low","change_percent","change_amount"}:period=None
            support(number[0],field,symbol,period,value,unit,SCALE.get(unit,Decimal(1)))
    # Model output extends recognition only. It cannot waive a rule conflict or
    # introduce evidence, arithmetic results, or an authoritative entity mapping.
    for claim in extracted_claims or []:
        quote, field, raw = claim["quote"], claim["metric"], claim["value"]
        if answer[claim["start"]:claim["end"]] != quote or (raw is not None and raw not in quote):
            failure("CLAIM_SOURCE_MISMATCH", quote)
            continue
        from .claim_extraction import claim_number, claim_prefix
        before_value = quote[:quote.find(raw)] if raw else quote
        clause_prefix = claim_prefix(answer, claim["start"], quote, raw)
        kind = claim["kind"]
        nonasserted = bool(re.search(r"不知道|无法确认|不能确认|未核实|没有证据", clause_prefix))
        hypothetical = bool(re.search(r"^(?:如果|假设|例如|举例|若(?:股价|价格|指数|未来))", clause_prefix.strip()))
        prediction = bool(re.search(r"预计|预测|有望|可能|将会|目标价|假定", clause_prefix))
        attributed = bool(re.search(r"^(?:按|根据)你提供|^你(?:提到|说)", clause_prefix.strip())) and (
            raw is None or raw in (state.get("user_updates") or [{}])[-1].get("text", ""))
        if nonasserted or hypothetical or (kind == "prediction" and prediction) or attributed:
            unverified.append({"text": quote[:200], "reason": "nonfactual_or_attributed_extracted_claim", "kind": kind})
            continue
        if raw is None:
            unverified.append({"text": quote[:200], "reason": "qualitative_claim_no_semantic_verifier", "kind": kind})
            continue
        subject = claim["subject"]
        bound = symbols(quote)
        symbol = names.get(subject) if subject else None
        if subject and re.fullmatch(r"\d{6}\.(?:SH|SZ)", subject, re.I):
            symbol = subject.upper()
        if len(bound) == 1:
            explicit = next(iter(bound))
            if symbol and symbol != explicit:
                failure("CLAIM_ENTITY_MISMATCH", quote, subject=subject)
                continue
            symbol = explicit
        if subject and not symbol and subject not in {"它", "该股票", "该股", "该公司"}:
            failure("ENTITY_NOT_BOUND_TO_SOURCE", quote, subject=subject)
            continue
        symbol = symbol or default_symbol
        if symbol is None and len(bound) > 1:
            failure("AMBIGUOUS_ENTITY", quote)
            continue
        raw_period = claim["period"]
        period = None
        if raw_period:
            if date_value(raw_period):
                period = date_value(raw_period)
            else:
                from .data.periods import resolve_period
                basis = ctx.scope_constraints.get("financial_basis")
                parsed = resolve_period(raw_period, ctx.as_of.date(), basis)
                # Relative claims need an explicit year; never reinterpret old
                # relative wording using a resumed session's current clock.
                if (not re.search(r"20\d{2}", raw_period) or not parsed
                        or parsed.get("basis") == "unspecified"):
                    failure("CLAIM_PERIOD_UNRESOLVED", quote, period=raw_period)
                    continue
                period = parsed["end"]
        if field in {"data_time", "fetched_at", "published_at", "report_period"}:
            pool = [f for f in facts if f.unit == "date" and f.field == field
                    and (not symbol or f.symbol == symbol)]
            stamp = date_value(raw)
            if not stamp and TIME.fullmatch(raw):
                stamp = raw
                matches = [f for f in pool if stamp in f.value]
            else:
                matches = [f for f in pool if stamp and f.value.startswith(stamp)]
            if not matches:
                failure("DATE_NOT_SUPPORTED", quote, field=field, symbol=symbol)
            else:
                checked.append({"text": quote[:200], "field": field, "symbol": symbol,
                                "sources": [asdict(f) for f in matches[:3]], "recognizer": "qwen"})
            continue
        # An unsupported quantitative financial claim remains a conflict. Code
        # literals and general nonfinancial numbers are outside financial scope.
        if field is None and claim["unit"] is None:
            unverified.append({"text": quote[:200], "reason": "unsupported_extracted_claim"})
            continue
        value_start = quote.find(raw)
        numeral = r"[\d零〇一二两三四五六七八九十百千万亿点负正.,]"
        after_value = quote[value_start + len(raw):].lstrip()
        actual_unit = next((u for u in sorted(SCALE.keys() | {"元/股", "元", "美元", "港元", "USD", "HKD", "股", "点", "%", "％", "个百分点", "倍"}, key=len, reverse=True)
                            if after_value.startswith(u)), None)
        if ((value_start and re.fullmatch(numeral, quote[value_start - 1]))
                or (not actual_unit and after_value and re.match(numeral, after_value))):
            failure("CLAIM_VALUE_MISMATCH", quote, value=raw)
            continue
        unit = claim["unit"]
        if actual_unit and unit and actual_unit.replace("％", "%") != unit.replace("％", "%"):
            failure("CLAIM_UNIT_MISMATCH", quote, unit=unit, expected=actual_unit)
            continue
        unit = actual_unit or unit
        if unit == "百分点":
            unit = "个百分点"
        if unit == "百分之":
            if not before_value.rstrip().endswith("百分之"):
                failure("CLAIM_UNIT_MISMATCH", quote, unit=unit)
                continue
            unit = "%"
        try:
            value = claim_number(raw)
        except ValueError:
            failure("CLAIM_NUMBER_UNRESOLVED", quote, value=raw)
            continue
        if field in {"change_amount", "change_percent"} and value > 0 and re.search(r"下跌|跌幅", clause_prefix):
            value = -value
        explicit_fields = {CANONICAL.get(m[0], CANONICAL.get(m[0].upper())) for m in METRIC.finditer(quote)}
        if len(explicit_fields) == 1 and field not in explicit_fields:
            failure("CLAIM_METRIC_MISMATCH", quote, field=field)
            continue
        if field in {"price", "previous_close", "open", "high", "low", "change_percent", "change_amount"}:
            period = None
        previous_checked = len(checked)
        support(quote, field, symbol, period, str(value), unit, SCALE.get(unit, Decimal(1)))
        for item in checked[previous_checked:]:
            item["recognizer"] = "qwen"

    return {"engine":"structured_evidence_rules_v1","status":"conflict" if issues else "checked_supported_patterns" if checked else "not_verified",
            "issues":issues[:50],"checked":checked[:100],"unverified":unverified[:50],
            "unavailable_evidence":unavailable[:30],"coverage":"数字/日期/证券代码的可识别表达；不验证未解析自由文本或因果推断，不代表全文事实正确"}
