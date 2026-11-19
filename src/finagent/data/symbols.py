from __future__ import annotations

from importlib.resources import files
import json
from pathlib import Path
import re
import unicodedata

from ..errors import AgentError


INDICES = {
    "000001.SH": {"name": "上证指数", "aliases": ["沪指", "上证", "上证综指", "上证综合指数"]},
    "399001.SZ": {"name": "深证成指", "aliases": ["深成指", "深证成份指数", "深证成分指数"]},
    "399006.SZ": {"name": "创业板指", "aliases": ["创业板指数"]},
    "000300.SH": {"name": "沪深300", "aliases": ["沪深300指数", "沪深三百"]},
    "000016.SH": {"name": "上证50", "aliases": ["上证50指数"]},
    "000905.SH": {"name": "中证500", "aliases": ["中证500指数"]},
    "000852.SH": {"name": "中证1000", "aliases": ["中证1000指数"]},
    "000688.SH": {"name": "科创50", "aliases": ["科创50指数", "科创板50指数"]},
    "399986.SZ": {"name": "中证银行指数", "aliases": ["中证银行"],
                  "sector": "银行", "scope_note": "以中证银行指数的样本表现作为银行板块代表口径，不等于全部银行个股或其他供应商的行业板块"},
}

SECTOR_INDEX_PROXIES = {
    "银行": {"symbol": "399986.SZ", "aliases": ["银行板块", "银行行业", "银行业"]},
}


def is_index(symbol: str) -> bool:
    return symbol in INDICES


def price_unit(symbol: str) -> str:
    return "index_point" if is_index(symbol) else "CNY/share"


def volume_unit(symbol: str) -> str:
    return "vendor_index_volume" if is_index(symbol) else "share"


def require_stock(symbol: str) -> str:
    symbol = normalize_symbol(symbol)
    if is_index(symbol):
        raise AgentError("UNSUPPORTED_INSTRUMENT", "指数支持行情、日线和技术指标；不支持公司财报、公司资料或个股新闻")
    return symbol


def normalize_symbol(raw: str) -> str:
    text = raw.strip().upper()
    if re.search(r"(?:\.HK|^HK|\.US|\.BJ|^BJ)", text) or re.fullmatch(r"\d{5}", text):
        raise AgentError("UNSUPPORTED_MARKET", "当前适配器仅支持沪深 A 股，港股等需单独接入")
    match = re.fullmatch(r"(?:(SH|SZ))?(\d{6})(?:\.(SH|SZ))?", text)
    if not match or (match[1] and match[3] and match[1] != match[3]):
        raise AgentError("INVALID_SYMBOL", "请使用六位沪深 A 股代码，例如 600519.SH")
    code = match[2]
    explicit_exchange = match[1] or match[3]
    # Preserve bare A-share codes: 000001 remains Ping An Bank, never SSE Composite.
    if explicit_exchange and f"{code}.{explicit_exchange}" in INDICES:
        return f"{code}.{explicit_exchange}"
    if not explicit_exchange and code.startswith("399") and f"{code}.SZ" in INDICES:
        return f"{code}.SZ"
    if code.startswith("399"):
        raise AgentError("UNSUPPORTED_INSTRUMENT", "该深市指数尚未接入，不能作为A股股票读取")
    exchange = "SH" if code.startswith("6") else "SZ" if code.startswith(("0", "3")) else None
    if exchange is None:
        raise AgentError("UNSUPPORTED_MARKET", "当前仅支持沪深 A 股")
    if (match[1] or match[3]) and (match[1] or match[3]) != exchange:
        raise AgentError("INVALID_SYMBOL", "证券代码与交易所不一致")
    return f"{code}.{exchange}"


def vendor_symbol(symbol: str) -> str:
    code, exchange = normalize_symbol(symbol).split(".")
    return exchange.lower() + code


def resolve(query: str, catalog: Path | None = None) -> list[dict]:
    query = clean_name(query)
    indices = [{"symbol": symbol, "name": entry["name"], "identity_verified": False,
                "instrument_type": "index", "match_type": "exact"}
               for symbol, entry in INDICES.items()
               if query in [clean_name(n) for n in [entry["name"], *entry["aliases"]]]]
    if indices:
        return indices
    for sector, proxy in SECTOR_INDEX_PROXIES.items():
        if query in [clean_name(name) for name in [sector, *proxy["aliases"]]]:
            symbol = proxy["symbol"]
            return [{"symbol": symbol, "name": INDICES[symbol]["name"], "identity_verified": False,
                     "instrument_type": "index", "match_type": "sector_proxy", "requested_sector": sector,
                     "requested_query": query, "proxy_note": INDICES[symbol]["scope_note"]}]
    if re.fullmatch(r"(?i)(?:SH|SZ|HK|BJ)?\d+(?:\.[A-Z]+)?", query):
        symbol = normalize_symbol(query)
        if is_index(symbol):
            return [{"symbol": symbol, "name": INDICES[symbol]["name"], "identity_verified": False,
                     "instrument_type": "index", "match_type": "code"}]
        return [{"symbol": symbol, "name": None, "identity_verified": False, "match_type": "code"}]
    content = catalog.read_text(encoding="utf-8") if catalog else files("finagent").joinpath("resources/securities.json").read_text(encoding="utf-8")
    entries = json.loads(content)
    exact = [e for e in entries if query in [clean_name(n) for n in [e["name"], *e.get("aliases", [])]]]
    matches = exact or [e for e in entries if query and query in clean_name(e["name"])]
    unique = {normalize_symbol(e["symbol"]): e for e in matches}
    return [{"symbol": symbol, "name": e["name"], "identity_verified": False,
             "match_type": "exact" if exact else "partial"} for symbol, e in unique.items()]


def clean_name(query: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", query)).casefold()
