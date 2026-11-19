"""Adapted from Trading's structured adapters and sina_finance parser (Apache-2.0)."""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
import re

from bs4 import BeautifulSoup

from ...errors import AgentError
from ...runtime import TZ, ExecutionContext
from ..contracts import DailyBar, Quote
from .. import endpoints
from ..symbols import vendor_symbol, is_index, price_unit, volume_unit
from .common import number, publication_time, source_result

STATEMENTS = {"income": "lrb", "balance": "fzb", "cashflow": "llb"}
METRICS = {
    "income": {"revenue": ("BIZINCO",), "total_revenue": ("BIZTOTINCO",), "net_profit": ("NETPROFIT",),
               "attributable_net_profit": ("PARENETP", "NETPARECOMPPROF"), "eps": ("BASICEPS",)},
    "balance": {"total_assets": ("TOTASSET",), "total_liabilities": ("TOTLIAB",)},
    "cashflow": {"operating_cashflow": ("MANANETR", "NETCASHOPERATE", "NETCASHOPERACT")},
}

METRIC_LABELS = {"revenue": ("营业收入",), "total_revenue": ("营业总收入",), "net_profit": ("净利润",),
                 "attributable_net_profit": ("归属于母公司", "归属于上市公司", "归母"),
                 "eps": ("每股收益",), "total_assets": ("资产总计", "总资产", "资产总额"),
                 "total_liabilities": ("负债合计", "总负债", "负债总计", "负债总额"),
                 "operating_cashflow": ("经营活动产生的现金流量净额", "经营活动现金流量净额")}


class SinaProvider:
    def __init__(self, http):
        self.http = http

    def quote(self, symbol: str, ctx: ExecutionContext):
        vendor = vendor_symbol(symbol)
        raw = self.http.get("sina_quote", endpoints.SINA_QUOTE, ctx, params={"list": vendor}, encoding="gbk")
        match = re.search(r'hq_str_' + re.escape(vendor) + r'="([^"]+)"', raw.text)
        if not match:
            raise AgentError("SYMBOL_NOT_FOUND", "新浪未返回匹配证券的行情")
        values = match.group(1).split(",")
        if len(values) < 32 or not values[0]:
            raise AgentError("INVALID_RESPONSE", "新浪行情字段不足")
        quote = Quote(symbol=symbol, name=values[0], price=number(values[3]), previous_close=number(values[2]),
                      open=number(values[1]), high=number(values[4]), low=number(values[5]),
                      volume_shares=number(values[8]), amount_cny=number(values[9]),
                      data_time=datetime.fromisoformat(f"{values[30]}T{values[31]}+08:00"))
        return source_result(raw, symbol, quote.model_dump(mode="json"), currency="CNY", price_basis="unadjusted_snapshot",
                             data_time=quote.data_time.isoformat(),
                             warnings=["指数成交量保留供应商原始口径，不与股票或其他来源直接比较"] if is_index(symbol) else [], units={"price": price_unit(symbol), "volume_shares": volume_unit(symbol), "amount_cny": "CNY"})

    def daily(self, symbol: str, ctx: ExecutionContext):
        raw = self.http.get("sina_daily", endpoints.SINA_DAILY, ctx,
                            params={"symbol": vendor_symbol(symbol), "scale": "240", "ma": "no", "datalen": "800"})
        rows = raw.json()
        if not isinstance(rows, list):
            raise AgentError("INVALID_RESPONSE", "新浪日线结构变更，应为记录数组")
        bars = {}
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                raise AgentError("INVALID_RESPONSE", "日线条目格式错误")
            bar = DailyBar(date=date.fromisoformat(str(row["day"])[:10]),
                           **{k: number(row.get(k)) for k in ("open", "high", "low", "close")},
                           volume_shares=number(row.get("volume")))
            key = bar.date.isoformat()
            value = {**bar.model_dump(mode="json"), "locator": f"$[{i}]"}
            if key in bars and {k: v for k, v in bars[key].items() if k != "locator"} != {k: v for k, v in value.items() if k != "locator"}:
                raise AgentError("CONFLICTING_BARS", "同日出现互相冲突的日线")
            bars[key] = value
        return source_result(raw, symbol, {"bars": [bars[key] for key in sorted(bars)]}, currency="CNY",
                             units={"price": price_unit(symbol), "volume_shares": volume_unit(symbol), "amount_cny": "CNY"}, price_basis="unadjusted",
                             warnings=["接口不回显证券身份；按请求参数关联，非独立身份核验", "接口未提供成交额及可信收盘完整标记；不声明最终收盘", "序列最多约 800 条；不保证任意历史覆盖"] +
                             (["指数成交量保留供应商原始口径，不与股票或其他来源直接比较"] if is_index(symbol) else []))

    def financials(self, symbol: str, statement: str, ctx: ExecutionContext):
        raw = self.http.get("sina_finance", endpoints.SINA_FINANCIALS, ctx,
                            params={"paperCode": vendor_symbol(symbol), "source": STATEMENTS[statement], "type": "0", "page": "1", "num": "40"})
        body = raw.json()
        try:
            reports = body["result"]["data"]["report_list"]
        except (KeyError, TypeError) as exc:
            raise AgentError("INVALID_RESPONSE", "新浪财报缺少 report_list") from exc
        if not isinstance(reports, dict):
            raise AgentError("INVALID_RESPONSE", "新浪财报报告结构变更")
        result = []
        for period, report in reports.items():
            day = datetime.strptime(str(period).replace("-", ""), "%Y%m%d").date()
            if not isinstance(report, dict) or not isinstance(report.get("data"), list):
                raise AgentError("INVALID_RESPONSE", "财务科目结构错误")
            if day > ctx.as_of.astimezone(TZ).date():
                continue
            try:
                published, precision, available = publication_time(report.get("publish_date"))
            except AgentError:
                continue  # No trustworthy publication time: excluded, never invented.
            if available > ctx.as_of:
                continue
            fields = {item["item_field"]: (i, item) for i, item in enumerate(report["data"])
                      if isinstance(item, dict) and item.get("item_field")}
            field_names = [item["item_field"] for item in report["data"] if isinstance(item, dict) and item.get("item_field")]
            if len(fields) != len(field_names):
                raise AgentError("DUPLICATE_FINANCIAL_FIELD", "同份报告出现重复科目，不能静默覆盖")
            if not fields:
                raise AgentError("INVALID_RESPONSE", "财报科目为空")
            raw_currency = report.get("rCurrency")
            currency_map = {"CNY": "CNY", "RMB": "CNY", "人民币": "CNY", "人民币元": "CNY", "USD": "USD", "HKD": "HKD"}
            currency = currency_map.get(raw_currency)
            if currency is None:
                raise AgentError("UNKNOWN_CURRENCY", "财报未提供已识别的列报币种，不能默认换算或填入人民币")
            metrics = {}
            for metric, candidates in METRICS[statement].items():
                selected = next(((field, *fields[field]) for field in candidates if field in fields and number(fields[field][1].get("item_value")) is not None), None)
                if selected:
                    field, i, item = selected
                    label = item.get("item_title")
                    if not isinstance(label, str) or not any(expected in label for expected in METRIC_LABELS[metric]):
                        raise AgentError("FINANCIAL_FIELD_MISMATCH", f"财务字段 {field} 的原文标签与指标不一致")
                    metrics[metric] = {"value": number(item["item_value"]), "raw_value": item["item_value"],
                                       "raw_field": field, "label": label,
                                       "unit": "currency/share" if metric == "eps" else "currency",
                                       "locator": f"$.result.data.report_list['{period}'].data[{i}].item_value"}
                else:
                    metrics[metric] = None
            result.append({"report_period": day.isoformat(), "published_at": published.isoformat(),
                           "time_precision": precision, "available_at": available.isoformat(), "currency": currency,
                           "basis": "point_in_time" if statement == "balance" else "year_to_date", "metrics": metrics,
                           "raw_fields": {field: item for field, (_, item) in fields.items()}})
        result.sort(key=lambda item: item["report_period"], reverse=True)
        return source_result(raw, symbol, {"statement": statement, "reports": result},
                             warnings=["聚合三表，非原始年报；历史披露版本/重述未独立核验", "金额单位沿用 Trading 对该接口的元口径；原字段可回读，未与原始财报逐项核验", "无可验证披露时间或晚于截止时间的报告已排除", "接口不回显证券身份，按请求参数关联"])

    def profile(self, symbol: str, ctx: ExecutionContext):
        code = symbol[:6]
        raw = self.http.get("sina_f10", f"{endpoints.SINA_PROFILE}vCI_CorpInfo/stockid/{code}.phtml", ctx, encoding="gbk")
        soup = BeautifulSoup(raw.text, "html.parser")
        title = soup.title.get_text() if soup.title else ""
        if code not in title:
            raise AgentError("IDENTITY_MISMATCH", "公司资料页面标题未匹配证券代码")
        labels = {}
        for row in soup.select("tr"):
            cells = [cell.get_text(" ", strip=True).rstrip("：:") for cell in row.find_all(["td", "th"], recursive=False)]
            for i in range(0, len(cells) - 1, 2):
                labels[cells[i]] = cells[i + 1]
        name, listing = labels.get("公司名称"), labels.get("上市日期")
        if not name or not listing:
            raise AgentError("INVALID_RESPONSE", "公司全称或上市日期缺失")
        date.fromisoformat(listing)
        return source_result(raw, symbol, {"full_name": name, "listed_at": listing,
                             "main_business": labels.get("主营业务"), "locator": "HTML tr: 公司名称/上市日期/主营业务"},
                             warnings=["当前公司资料快照，不支持将当前主营业务回填到历史时点"])

    def news(self, symbol: str, ctx: ExecutionContext):
        raw = self.http.get("sina_news", endpoints.SINA_NEWS, ctx, params={"symbol": vendor_symbol(symbol), "Page": "1"}, encoding="gb18030")
        soup = BeautifulSoup(raw.text, "html.parser")
        articles = []
        blocks = soup.select(".datelist")
        # A single <ul> contains many timestamp/link pairs. Looking at the
        # anchor's parent text assigns the first timestamp to every article.
        # Match each immediately preceding timestamp instead (Trading's method).
        pattern = r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*<a\b[^>]*?\bhref\s*=\s*(['\"])(.*?)\3[^>]*>(.*?)</a>"
        for block_index, block in enumerate(blocks):
            for i, match in enumerate(re.finditer(pattern, unescape(str(block)), re.S | re.I)):
                day, clock, _, url, title = match.groups()
                articles.append({"title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
                                 "content": "", "time": f"{day} {clock}", "source": "新浪财经", "url": url,
                                 "locator": f"HTML .datelist[{block_index}] entry[{i}]"})
        if not articles and not re.search(r"暂无|没有.*新闻|no news", soup.get_text(), re.I):
            raise AgentError("INVALID_RESPONSE", "新浪新闻页面未解析到时间与标题，可能结构变化")
        return source_result(raw, symbol, {"articles": articles})
