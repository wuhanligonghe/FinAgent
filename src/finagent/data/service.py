from __future__ import annotations

from datetime import date, datetime
import re
from time import monotonic
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from pydantic import ValidationError

from ..config import Settings
from ..errors import AgentError
from ..runtime import ExecutionContext, TZ, now
from .contracts import NewsItem, Observation
from .evidence import EvidenceStore
from .http import SourceHTTP
from .providers.common import publication_time
from .providers.tencent import TencentProvider
from .providers.sina import SinaProvider
from .providers.eastmoney import EastmoneyProvider
from .symbols import INDICES, normalize_symbol, resolve, clean_name, require_stock

ABORT_ERRORS = {"CANCELLED", "TIME_BUDGET", "SOURCE_BUDGET"}


class DataService:
    """Unified source selection, validation, cache and normalized observations."""

    def __init__(self, settings: Settings, http: SourceHTTP | None = None):
        self.settings = settings
        self.store = EvidenceStore(settings.data_dir)
        self.http = http or SourceHTTP(self.store, settings.request_timeout, settings.source_retries, proxy_url=settings.data_proxy_url)
        self.providers = {"tencent": TencentProvider(self.http), "sina": SinaProvider(self.http), "eastmoney": EastmoneyProvider(self.http)}
        self._cache: dict[tuple, tuple[float, Observation]] = {}

    def close(self):
        self.http.close()

    def sector_ranking(self, limit, ctx):
        from .providers.sectors import industry_snapshot
        if ctx.as_of.astimezone(TZ).date() < now().astimezone(TZ).date() or ctx.cutoff_explicit:
            raise AgentError("SNAPSHOT_NOT_HISTORICAL", "行业汇总没有可核对的行情时间，不能用于显式历史截止查询")
        value = self._cached(("industry_ranking", "sina"), 30, lambda: industry_snapshot(self.http, ctx), ctx)
        rows = value.data["rows"]
        value.data = {**value.data, "rows": rows[:limit], "requested_limit": limit,
                      "returned_count": min(limit, len(rows)), "historical_snapshot_supported": False}
        return value

    def resolve_entity(self, query: str, ctx: ExecutionContext):
        ctx.check()
        query = clean_name(query)
        if not query or len(query) > 80 or re.search(r"[/\\:]", query):
            raise AgentError("INVALID_ARGUMENTS", "请输入公司名称、简称或沪深 A 股代码")
        if any(market in query for market in ("港股", "美股", "北交所", ".hk", ".us", ".bj")):
            raise AgentError("UNSUPPORTED_MARKET", "名称搜索仅支持沪深 A 股，不能替换为其他市场证券")
        candidates = resolve(query, self.settings.securities_file)
        exact = [c for c in candidates if c["match_type"] in ("exact", "code", "sector_proxy")]
        if exact:
            value = Observation(status="partial", source="local_security_catalog", market="CN_A",
                                data={"candidates": candidates, "truncated": False},
                                warnings=["本地目录仅作名称映射，不证明证券存在；需行情/公司资料继续核验"])
        else:
            # Cache only source observations, never task selection or clarification.
            value = self._cached(("security_search", query), 300,
                                 lambda: self.providers["eastmoney"].search(query, ctx), ctx)
            if not value.data["candidates"] and candidates:
                value.data["candidates"] = candidates
                value.status = "partial"
                value.warnings.append("在线未匹配，列出有限本地目录的近似候选，需用户确认")
            candidates = value.data["candidates"]
            exact = [c for c in candidates if c["match_type"] == "exact"]
        selected = exact[0] if len(exact) == 1 and not value.data.get("truncated") and not value.stale else None
        if selected:
            ctx.bind_symbol(selected["symbol"])
            if selected.get("proxy_note"):
                value.warnings.append(selected["proxy_note"] + "；答复必须明确采用该指数代表口径")
        elif candidates:
            options = "；".join(f"{i}. {c['name']}（{c['symbol']}）" for i, c in enumerate(candidates[:10], 1))
            note = "候选较多或未完整返回，可补充更完整的名称缩小范围。" if value.data.get("truncated") or len(candidates) > 10 else ""
            ctx.clarification = f"请确认“{query}”对应的证券对象：{options}。{note}可回复名称或选项序号，也可提供代码。"
        else:
            ctx.clarification = f"未匹配到“{query}”的已接入对象。在线检索只覆盖沪深A股，其他指数可能尚未接入，不能据此断言不存在；请补充更完整名称或已支持代码。"
        value.symbol = selected["symbol"] if selected else None
        value.data.update(query=query, selected=selected, needs_clarification=ctx.clarification)
        return value

    def _cached(self, key, ttl, loader, ctx):
        ctx.check()
        entry = self._cache.get(key)
        if entry and monotonic() - entry[0] < ttl:
            value = entry[1].model_copy(deep=True)
            value.cache_hit = True
        else:
            try:
                value = loader()
                self._cache[key] = (monotonic(), value.model_copy(deep=True))
            except AgentError as exc:
                if exc.code in ABORT_ERRORS or not entry:
                    raise
                value = entry[1].model_copy(deep=True)
                value.cache_hit, value.stale, value.status = True, True, "partial"
                value.warnings.append(f"刷新失败（{exc.code}），返回旧缓存；获取时间未更新")
        ctx.evidence_ids.update(value.snapshot_ids)
        if value.snapshot_id:
            ctx.evidence_ids.add(value.snapshot_id)
        return value

    def _fetch(self, sources, method, symbol, ctx, transform=None):
        errors, last_empty = [], None
        for source in sources:
            ctx.check()
            try:
                value = getattr(self.providers[source], method)(symbol, ctx)
                if transform:
                    value = transform(value)
                if value.symbol != symbol or value.market != "CN_A":
                    raise AgentError("IDENTITY_MISMATCH", "规范结果与请求证券不一致")
                if value.status == "empty":
                    last_empty = value
                    continue
                value.warnings = errors + value.warnings
                if value.warnings:
                    value.status = "partial"
                return value
            except AgentError as exc:
                if exc.code in ABORT_ERRORS:
                    raise
                errors.append(f"{source}: {exc.code} ({exc.message})")
            except (ValidationError, ValueError, KeyError, TypeError, UnicodeError, IndexError, AttributeError) as exc:
                errors.append(f"{source}: INVALID_RESPONSE ({type(exc).__name__})")
        if last_empty is not None:
            last_empty.warnings = errors + last_empty.warnings
            return last_empty
        raise AgentError("ALL_SOURCES_FAILED", "; ".join(errors) or "没有可用来源")

    @staticmethod
    def _symbol(symbol, ctx, stocks_only=False):
        symbol = require_stock(symbol) if stocks_only else normalize_symbol(symbol)
        ctx.bind_symbol(symbol)
        return symbol

    def quote(self, symbol: str, ctx: ExecutionContext, source=None):
        symbol = self._symbol(symbol, ctx)
        if ctx.as_of.astimezone(TZ).date() < now().date():
            raise AgentError("SNAPSHOT_NOT_HISTORICAL", "当前报价不支持历史截止时间，请改用历史日线")
        sources = [source] if source else self.settings.quote_sources

        def validate(value):
            stamp = datetime.fromisoformat(value.data_time)
            if stamp > ctx.as_of:
                raise AgentError("AFTER_CUTOFF", "行情时间晚于任务截止时间")
            if stamp.date() < ctx.as_of.astimezone(TZ).date():
                value.warnings.append("行情日期早于截止自然日；可能休市、停牌或滞后，未据此判定交易日")
            value.warnings.append("第三方行情快照，延迟及最终收盘状态未独立确认")
            value.warnings.append("单次OHLC快照不能确定日内高低点先后、早盘或尾盘走势；未取得分时证据时不作路径复盘")
            if INDICES.get(symbol, {}).get("scope_note"):
                value.data["index_scope"] = INDICES[symbol]["scope_note"]
                value.warnings.append(INDICES[symbol]["scope_note"])
            return value

        value = self._cached(("quote", symbol, tuple(sources)), 30,
                             lambda: self._fetch(sources, "quote", symbol, ctx, validate), ctx)
        # Recheck cache against this task's cutoff.
        if datetime.fromisoformat(value.data_time) > ctx.as_of:
            raise AgentError("AFTER_CUTOFF", "缓存行情晚于截止时间")
        return value

    def daily(self, symbol: str, start: date, end: date, ctx: ExecutionContext):
        symbol = self._symbol(symbol, ctx)
        if start > end or (end - start).days > 1096 or end > ctx.as_of.astimezone(TZ).date():
            raise AgentError("INVALID_DATE_RANGE", "日线需起日不晚于止日、窗口不超过三年，且不晚于任务截止日")
        value = self._cached(("daily", symbol), 60, lambda: self._fetch(["sina"], "daily", symbol, ctx), ctx)
        all_bars = value.data["bars"]
        value.data = {"bars": [b for b in all_bars if start.isoformat() <= b["date"] <= end.isoformat()
                               and date.fromisoformat(b["date"]) < ctx.as_of.astimezone(TZ).date()],
                      "requested_start": start.isoformat(), "requested_end": end.isoformat(),
                      "source_start": all_bars[0]["date"] if all_bars else None,
                      "source_end": all_bars[-1]["date"] if all_bars else None}
        value.warnings.append("仅返回截止日之前的日线；未接交易日历，不能据此确认完整交易日覆盖或历史修订版本")
        if INDICES.get(symbol, {}).get("scope_note"):
            value.data["index_scope"] = INDICES[symbol]["scope_note"]
            value.warnings.append(INDICES[symbol]["scope_note"])
        value.status = "partial" if value.data["bars"] else "empty"
        if value.data["bars"]:
            value.data_time = value.data["bars"][-1]["date"]
        return value

    def financials(self, symbol: str, statement: str, report_period: date | None, limit: int, ctx: ExecutionContext):
        symbol = self._symbol(symbol, ctx, stocks_only=True)
        if ctx.scope_constraints.get("report_period"):
            report_period = report_period or date.fromisoformat(ctx.scope_constraints["report_period"])
            if str(report_period) not in ctx.scope_constraints["allowed_report_periods"]:
                raise AgentError("SCOPE_MISMATCH", "财务期间不在宿主已解析的本轮期间范围")
        if report_period and report_period > ctx.as_of.astimezone(TZ).date():
            raise AgentError("INVALID_REPORT_PERIOD", "报告期晚于截止日")
        # Cutoff is part of the key: historical filtering must never reuse a later view.
        key = ("financials", symbol, statement, ctx.as_of.isoformat())

        def load():
            try:
                value = self.providers["sina"].financials(symbol, statement, ctx)
                value.status = "partial"
                return value
            except AgentError:
                raise
            except (ValidationError, ValueError, KeyError, TypeError, AttributeError) as exc:
                raise AgentError("INVALID_RESPONSE", "财务字段或期间格式校验失败") from exc

        value = self._cached(key, 21600, load, ctx)
        reports = value.data["reports"]
        reports = [r for r in reports if not report_period or r["report_period"] == report_period.isoformat()][:limit]
        value.data["reports"] = reports
        currencies = {r["currency"] for r in reports}
        value.currency = next(iter(currencies)) if len(currencies) == 1 else None
        value.units = {"financial_amount": "列报币种元（接口约定，待原始财报核验）", "eps": "列报币种元/股"}
        value.data_time = reports[0]["report_period"] if reports else None
        if not reports or not any(any(m is not None for m in r["metrics"].values()) for r in reports):
            value.status = "empty"
            value.warnings.append("请求报告期或已映射指标无覆盖；原始字段不能冒充已核验指标")
        from .financial_facts import attach_facts
        return attach_facts(value)

    def profile(self, symbol: str, ctx: ExecutionContext):
        symbol = self._symbol(symbol, ctx, stocks_only=True)
        if ctx.as_of.astimezone(TZ).date() < now().date():
            raise AgentError("SNAPSHOT_NOT_HISTORICAL", "公司资料仅支持当前快照，不支持历史回填")
        return self._cached(("profile", symbol), 86400, lambda: self._fetch(["sina"], "profile", symbol, ctx), ctx)

    def news(self, symbol: str, start: date, end: date, limit: int, ctx: ExecutionContext, source=None):
        symbol = self._symbol(symbol, ctx, stocks_only=True)
        if start > end or (end - start).days > 365 or end > ctx.as_of.astimezone(TZ).date():
            raise AgentError("INVALID_DATE_RANGE", "新闻窗口需有效、不超过一年，且不晚于任务截止日")
        sources = [source] if source else self.settings.news_sources

        def filter_news(value):
            articles, excluded, seen = [], 0, set()
            for item in value.data["articles"]:
                try:
                    stamp, precision, available = publication_time(item["time"])
                    if not start <= stamp.astimezone(TZ).date() <= end or available > ctx.as_of:
                        excluded += 1
                        continue
                    url = str(item["url"])
                    parsed = urlparse(url)
                    if parsed.scheme not in ("http", "https") or not parsed.hostname:
                        excluded += 1
                        continue
                    title = BeautifulSoup(str(item["title"]), "html.parser").get_text(" ", strip=True)
                    snippet = BeautifulSoup(str(item["content"]), "html.parser").get_text(" ", strip=True)[:1000]
                    article = NewsItem(title=title, snippet=snippet, published_at=stamp, time_precision=precision,
                                       available_at=available, publisher=str(item["source"]), url=url, locator=item["locator"])
                except (AgentError, ValidationError, ValueError, TypeError, KeyError):
                    excluded += 1
                    continue
                identity = (title, stamp.isoformat())
                if identity not in seen:
                    articles.append(article.model_dump(mode="json"))
                    seen.add(identity)
            articles.sort(key=lambda item: item["published_at"], reverse=True)
            value.data = {"articles": articles[:limit], "excluded_count": excluded, "requested_start": start.isoformat(), "requested_end": end.isoformat()}
            value.status = "partial" if articles else "empty"
            value.data_time = articles[0]["published_at"] if articles else None
            value.warnings = ["仅搜索近期第一页再按时间筛选；空结果不代表当时没有新闻", "新闻属于第三方报道/摘要，证券相关性及原始披露未独立核验"]
            return value

        return self._cached(("news", symbol, start, end, limit, ctx.as_of.isoformat(), tuple(sources)), 300,
                            lambda: self._fetch(sources, "news", symbol, ctx, filter_news), ctx)
