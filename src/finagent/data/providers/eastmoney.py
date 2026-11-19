"""Adapted from Trading/tradingagents/dataflows/a_stock.py:_fetch_news_eastmoney."""
import json
import re

from ...errors import AgentError
from ..endpoints import EASTMONEY_NEWS, EASTMONEY_SEARCH
from ..symbols import normalize_symbol, clean_name
from .common import source_result


class EastmoneyProvider:
    def __init__(self, http):
        self.http = http

    def search(self, query, ctx):
        raw = self.http.get("eastmoney_security_search", EASTMONEY_SEARCH, ctx,
                            params={"input": query, "type": 14, "count": 100},
                            headers={"Referer": "https://so.eastmoney.com/"})
        try:
            table = raw.json()["QuotationCodeTable"]
            if table["Status"] != 0:
                raise ValueError("Search failed")
            total = int(table["TotalCount"])
            rows = table["Data"]
            if rows is None and total == 0:
                rows = []
            if not isinstance(rows, list):
                raise ValueError("Missing search rows")
            if total < len(rows) or total < 0:
                raise ValueError("Invalid search count")
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentError("INVALID_RESPONSE", "东方财富证券搜索结构变更或查询失败") from exc
        candidates, excluded = {}, 0
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                raise AgentError("INVALID_RESPONSE", "证券搜索候选格式错误")
            if item.get("Classify") != "AStock" or str(item.get("SecurityType")) not in ("1", "2") or str(item.get("MktNum")) not in ("0", "1"):
                excluded += 1
                continue
            try:
                code = item["Code"]
                if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
                    raise ValueError("Invalid code")
                exchange = "SH" if str(item["MktNum"]) == "1" else "SZ"
                if str(item["SecurityType"]) != ("1" if exchange == "SH" else "2"):
                    raise ValueError("Conflicting security type")
                symbol = normalize_symbol(f"{code}.{exchange}")
                if item.get("QuoteID") != f"{item['MktNum']}.{code}":
                    raise ValueError("Conflicting market")
                name = item["Name"]
                if not isinstance(name, str) or not name.strip() or len(name) > 80:
                    raise ValueError("Invalid name")
            except (AgentError, ValueError, KeyError, TypeError) as exc:
                raise AgentError("INVALID_RESPONSE", "证券搜索返回的沪深 A 股身份字段不一致") from exc
            candidates[symbol] = {"symbol": symbol, "name": name, "identity_verified": False,
                                  "match_type": "exact" if clean_name(query) == clean_name(name) else "partial",
                                  "locator": f"$.QuotationCodeTable.Data[{index}]"}
        ordered = sorted(candidates.values(), key=lambda item: item["match_type"] != "exact")
        result = source_result(raw, None, {"candidates": ordered[:20], "excluded_count": excluded,
                                          "truncated": total > len(rows) or len(ordered) > 20, "total_count": total})
        result.status = "partial" if candidates else "empty"
        result.warnings = ["当前第三方证券名称索引，仅保留沪深 A 股；不代表历史名称或上市状态已核验，继续用行情/公司资料核验"]
        return result

    def news(self, symbol, ctx):
        query = {"uid": "", "keyword": symbol[:6], "type": ["cmsArticleWebOld"], "client": "web",
                 "clientType": "web", "clientVersion": "curr", "param": {"cmsArticleWebOld": {
                     "searchScope": "default", "sort": "default", "pageIndex": 1, "pageSize": 20, "preTag": "", "postTag": ""}}}
        raw = self.http.get("eastmoney_news", EASTMONEY_NEWS, ctx,
                            params={"cb": "callback", "param": json.dumps(query, ensure_ascii=False), "_": "1"},
                            headers={"Referer": "https://so.eastmoney.com/"})
        match = re.fullmatch(r"\s*callback\((.*)\);?\s*", raw.text, re.S)
        if not match:
            raise AgentError("INVALID_RESPONSE", "东方财富 JSONP 结构变更")
        try:
            items = json.loads(match[1])["result"]["cmsArticleWebOld"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentError("INVALID_RESPONSE", "东方财富新闻结果结构变更") from exc
        if not isinstance(items, list):
            raise AgentError("INVALID_RESPONSE", "东方财富新闻列表格式错误")
        articles = [{"title": item.get("title", ""), "content": item.get("content", ""),
                     "time": item.get("date", ""), "source": item.get("mediaName") or "东方财富",
                     "url": item.get("url", ""), "locator": f"$.result.cmsArticleWebOld[{i}]"} for i, item in enumerate(items)]
        return source_result(raw, symbol, {"articles": articles})
