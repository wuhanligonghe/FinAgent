from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import re
from typing import Literal

from pydantic import Field

from ..config import StrictModel
from ..data.contracts import Observation
from ..data.endpoints import ALLOWED_HOSTS
from ..errors import AgentError
from ..runtime import TZ, now
from ..tools.registry import Tool
from .materials import ResearchMaterials

FUND_FLOW = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
ATTENTION = "https://zx.10jqka.com.cn/event/api/getharden/"
MACRO = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
CLS = "https://www.cls.cn/nodeapi/telegraphList"
ALLOWED_HOSTS.update({"push2his.eastmoney.com", "zx.10jqka.com.cn", "np-weblist.eastmoney.com", "www.cls.cn", "finance.eastmoney.com"})


class WindowArgs(StrictModel):
    start: date
    end: date
    mode: Literal["auto", "online", "replay"] = "auto"


class PolicySearchArgs(StrictModel):
    query: str = Field("", max_length=300)
    days: int = Field(90, ge=1, le=365)


class PolicyReadArgs(StrictModel):
    policy_id: str = Field(pattern=r"^policy_[a-f0-9]{64}$")
    locator: str = Field("text:0:1200", max_length=100)


class ToneItem(StrictModel):
    result_id: str
    article_index: int = Field(ge=0)
    label: Literal["positive", "negative", "neutral", "unknown"]


class ToneArgs(StrictModel):
    items: list[ToneItem] = Field(min_length=1, max_length=50)


def count_tone(args, ctx, store):
    identities, counts, snapshots = set(), {label: 0 for label in ("positive", "negative", "neutral", "unknown")}, set()
    for item in args.items:
        if item.result_id not in ctx.result_tools or ctx.result_tools[item.result_id] not in ("get_stock_news", "get_macro_news"):
            raise AgentError("EVIDENCE_DENIED", "倾向统计只接受本任务已登记文章")
        result = store.load_result(item.result_id)
        articles = result.data.get("articles", [])
        if item.article_index >= len(articles):
            raise AgentError("INVALID_ARTICLE", "文章索引无效")
        article = articles[item.article_index]
        identity = re.sub(r"\W+", "", article["title"]).casefold()
        if identity in identities:
            continue
        identities.add(identity)
        counts[item.label] += 1
        snapshots.update(result.snapshot_ids)
        if result.snapshot_id:
            snapshots.add(result.snapshot_id)
    total = sum(counts.values())
    return Observation(status="ok", source="local_tone_counter", snapshot_ids=sorted(snapshots),
                       data={"kind": "tone_statistics", "counts": counts, "total": total,
                             "percentages": {k: str((Decimal(v) * 100 / total).quantize(Decimal("0.01"))) for k, v in counts.items()},
                             "labels_provenance": "模型标注，非真实投资者调查", "deduplication": "normalized_title"})


def signal(service, kind, args, ctx):
    if args.start > args.end or args.end > ctx.as_of.astimezone(TZ).date() or (args.end - args.start).days > 365:
        raise AgentError("INVALID_DATE_RANGE", "研究信号窗口无效：起日不晚于止日，截止不晚于任务日期，跨度不超过一年")
    if kind == "fund_flow" and not ctx.symbol:
        raise AgentError("SYMBOL_REQUIRED", "资金流工具需要个股对象；全局行业比较请使用 get_sector_ranking，不要反复缩短窗口")
    local = ResearchMaterials(service.store).signal(kind, ctx, args.start, args.end)
    if local and args.mode != "online":
        return local
    if args.mode == "replay":
        raise AgentError("REPLAY_UNAVAILABLE", "没有匹配的已导入真实信号")
    if kind == "fund_flow":
        code = ctx.symbol[:6]
        raw = service.http.get("eastmoney_fund_flow", FUND_FLOW, ctx, params={"secid": ("1." if ctx.symbol.endswith("SH") else "0.") + code,
            "lmt": 500, "klt": 101, "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56,f57"}, headers={"Referer": "https://quote.eastmoney.com/"})
        body = raw.json().get("data")
        if not isinstance(body, dict) or body.get("code") != code or not isinstance(body.get("klines"), list):
            raise AgentError("INVALID_RESPONSE", "资金流结构或证券不匹配")
        rows = []
        for line in body["klines"]:
            fields = line.split(",")
            day = date.fromisoformat(fields[0])
            available = datetime.combine(day + timedelta(days=1), time.min, TZ)
            if args.start <= day <= args.end and available <= ctx.as_of:
                number = Decimal(fields[1])
                if not number.is_finite():
                    raise AgentError("INVALID_RESPONSE", "资金流非有限值")
                rows.append({"date": str(day), "main_net_cny": str(number), "available_at": available.isoformat()})
        data = {"rows": rows, "definition": "东方财富按订单规模统计的主力净流入代理；不是投资者身份识别", "coverage": "接口最近最多500条，按日期筛选", "historical_version": False}
        return sourced(raw, data, "ok" if rows else "empty", ctx.symbol, {"main_net_cny": "CNY"})
    if kind == "attention":
        url = ATTENTION + f"date/{args.end}/orderby/date/orderway/desc/charset/GBK/"
        raw = service.http.get("ths_attention", url, ctx, encoding="gbk")
        body = raw.json()
        if body.get("errocode", 0) != 0 or not isinstance(body.get("data"), list):
            raise AgentError("INVALID_RESPONSE", "热度榜结构无效")
        # Editorial daily attribution has no intraday publication timestamp.
        available = datetime.combine(args.end + timedelta(days=1), time.min, TZ)
        if available > ctx.as_of:
            raise AgentError("AFTER_CUTOFF", "日期级榜单在当日截止前不可确认可用")
        items = []
        for row in body["data"]:
            code = str(row.get("code", ""))
            if re.fullmatch(r"[036]\d{5}", code):
                items.append({"symbol": code + (".SH" if code.startswith("6") else ".SZ"), "name": row.get("name", ""), "topic": row.get("reason", "")})
        return sourced(raw, {"items": items, "date": str(args.end), "available_at": available.isoformat(), "coverage": "同花顺强势股归因榜；非全市场关注度排名", "definition": "编辑归因标签，未上榜不代表低关注"}, "ok", None)
    errors = []
    try:
        raw = service.http.get("eastmoney_macro_news", MACRO, ctx, params={"client": "web", "biz": "web_724", "fastColumn": "102", "sortEnd": "", "pageSize": 40})
        rows = raw.json().get("data", {}).get("fastNewsList")
        if not isinstance(rows, list):
            raise AgentError("INVALID_RESPONSE", "宏观新闻结构无效")
    except AgentError as exc:
        if exc.code in ("CANCELLED", "TIME_BUDGET", "SOURCE_BUDGET", "RESEARCH_BUDGET", "STALE_RESEARCH_OWNER"):
            raise
        errors.append("eastmoney: " + exc.code)
        try:
            raw = service.http.get("cls_macro_news", CLS, ctx, params={"rn": 40, "page": 1}, headers={"Referer": "https://www.cls.cn/"})
            items = raw.json().get("data", {}).get("roll_data")
            if not isinstance(items, list):
                raise AgentError("INVALID_RESPONSE", "宏观新闻备用源结构无效")
            rows = [{"title": i.get("title") or i.get("brief") or str(i.get("content", ""))[:80], "summary": i.get("content") or i.get("brief", ""),
                     "showTime": datetime.fromtimestamp(int(i["ctime"]), TZ).isoformat(), "url": "https://www.cls.cn/detail/" + str(i.get("id", ""))} for i in items]
        except AgentError as fallback_error:
            if fallback_error.code in ("CANCELLED", "TIME_BUDGET", "SOURCE_BUDGET", "RESEARCH_BUDGET", "STALE_RESEARCH_OWNER"):
                raise
            errors.append("cls: " + fallback_error.code)
            return macro_html(service, args, ctx, errors)
    from ..data.providers.common import publication_time
    articles = []
    for i, item in enumerate(rows):
        stamp, precision, available = publication_time(item["showTime"])
        if args.start <= stamp.date() <= args.end and available <= ctx.as_of:
            articles.append({"title": item["title"], "snippet": str(item.get("summary", ""))[:1500], "published_at": stamp.isoformat(),
                "available_at": available.isoformat(), "time_precision": precision, "publisher": "东方财富快讯", "locator": f"articles/{i}", "url": item.get("url", "")})
    result = sourced(raw, {"articles": articles, "coverage": "最新一页按时间过滤；无历史查询保证，空结果不代表历史无新闻", "requested_start": str(args.start), "requested_end": str(args.end)}, "partial" if articles else "empty", None)
    result.warnings.extend(errors)
    return result


def macro_html(service, args, ctx, errors):
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse
    listing = service.http.get("eastmoney_macro_html", "https://finance.eastmoney.com/a/cywjh.html", ctx, headers={"Referer": "https://finance.eastmoney.com/"})
    soup = BeautifulSoup(listing.content, "html.parser")
    urls = list(dict.fromkeys(a["href"] for a in soup.find_all("a", href=True)
                if re.fullmatch(r"https://finance\.eastmoney\.com/a/20\d{16}\.html", a["href"])))[:5]
    articles, snapshots, parsed_count = [], [listing.snapshot_id], 0
    for url in urls:
        raw = service.http.get("eastmoney_macro_article", url, ctx, headers={"Referer": "https://finance.eastmoney.com/"})
        article = BeautifulSoup(raw.content, "html.parser")
        times = article.select(".time, .infos .item")
        stamp = re.search(r"(20\d{2})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2}):(\d{2})", times[0].get_text(" ", strip=True) if times else "")
        title = article.find("h1") or article.find("title")
        if not stamp or not title:
            continue
        parsed_count += 1
        published = datetime(*map(int, stamp.groups()), tzinfo=TZ)
        if not args.start <= published.date() <= args.end or published > ctx.as_of:
            continue
        body = article.select_one("#ContentBody")
        snapshots.append(raw.snapshot_id)
        articles.append({"title": title.get_text(" ", strip=True), "snippet": body.get_text(" ", strip=True)[:1500] if body else "",
            "published_at": published.isoformat(), "available_at": published.isoformat(), "time_precision": "minute", "publisher": "东方财富转载",
            "url": url, "snapshot_id": raw.snapshot_id, "locator": ".time / h1 / #ContentBody"})
    if not urls or not parsed_count:
        raise AgentError("INVALID_RESPONSE", "宏观要闻页缺少可核对的文章/发布时间，不能当作无新闻")
    result = sourced(listing, {"articles": articles, "coverage": "东方财富要闻页前5条的正文时间校验；非全网/历史检索", "requested_start": str(args.start), "requested_end": str(args.end)}, "partial" if articles else "empty")
    result.snapshot_ids = snapshots
    result.warnings = errors + ["备用HTML源，仍须核对原始报道与事件相关性"]
    return result


def sourced(raw, data, status, symbol=None, units=None):
    return Observation(status=status, source=raw.source, source_url=raw.url, fetched_at=raw.fetched_at,
                       snapshot_id=raw.snapshot_id, symbol=symbol, market="CN_A", units=units or {}, data=data)


def register_data_tools(registry, service):
    materials = ResearchMaterials(service.store)
    for name, kind, description in (("get_fund_flow", "fund_flow", "获取研究窗口资金流代理，保留供应商定义；支持真实导入回放"),
            ("get_market_attention", "attention", "获取日期级强势股题材榜；未上榜不等于低关注"),
            ("get_macro_news", "macro_news", "取得宏观快讯并严格按窗口过滤；仅最新一页，不保证历史覆盖")):
        registry.register(Tool(name, description, WindowArgs, lambda a, c, k=kind: signal(service, k, a, c)))
    registry.register(Tool("search_policy_documents", "检索本地带来源政策；获得policy_id及原文blocks定位；主题匹配需另证业务关联", PolicySearchArgs,
                           lambda a, c: materials.search_policy(c, a.query, a.days)))
    registry.register(Tool("read_policy_document", "读取已授权政策原文段落；定位可从read_evidence回读政策检索记录取得", PolicyReadArgs,
                           lambda a, c: materials.read_policy(c, a.policy_id, a.locator)))
    registry.register(Tool("calculate_news_tone", "按原始文章绑定的模型标签去重并确定性统计比例，不能代表全部投资者情绪", ToneArgs,
                           lambda a, c: count_tone(a, c, service.store), False))
