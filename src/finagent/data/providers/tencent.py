"""Adapted from Trading/backend/providers.py (Apache-2.0); identity checks added."""
from datetime import datetime
import re

from ...errors import AgentError
from ...runtime import TZ, ExecutionContext
from ..contracts import Quote
from ..endpoints import TENCENT_QUOTE
from ..symbols import vendor_symbol, is_index, price_unit, volume_unit
from .common import number, source_result


class TencentProvider:
    def __init__(self, http):
        self.http = http

    def quote(self, symbol: str, ctx: ExecutionContext):
        code = symbol[:6]
        vendor = vendor_symbol(symbol)
        raw = self.http.get("tencent_quote", TENCENT_QUOTE, ctx, params={"q": vendor}, encoding="gbk")
        match = re.search(r'v_' + re.escape(vendor) + r'="([^"]+)"', raw.text)
        if not match:
            raise AgentError("SYMBOL_NOT_FOUND", "腾讯未返回匹配证券的行情")
        values = match.group(1).split("~")
        if len(values) < 49 or values[2] != code or not values[1]:
            raise AgentError("IDENTITY_MISMATCH", "腾讯行情字段不足或证券代码不匹配")
        parts = values[35].split("/")
        exact = number(parts[2]) if len(parts) == 3 else None
        amount_10k, lots = number(values[37]), number(values[36])
        quote = Quote(symbol=symbol, name=values[1], price=number(values[3]), previous_close=number(values[4]),
                      open=number(values[5]), high=number(values[33]), low=number(values[34]),
                      volume_shares=lots if is_index(symbol) else lots * 100 if lots is not None else None,
                      amount_cny=exact if exact is not None else amount_10k * 10000 if amount_10k is not None else None,
                      pe_ttm=None if is_index(symbol) else number(values[39]), pb=None if is_index(symbol) else number(values[46]),
                      data_time=datetime.strptime(values[30], "%Y%m%d%H%M%S").replace(tzinfo=TZ))
        return source_result(raw, symbol, quote.model_dump(mode="json"), currency="CNY", price_basis="unadjusted_snapshot",
                             data_time=quote.data_time.isoformat(),
                             units={"price": price_unit(symbol), "volume_shares": volume_unit(symbol), "amount_cny": "CNY",
                                    "source_volume": "vendor_index_volume" if is_index(symbol) else "lot=100 shares"},
                             warnings=["指数成交量保留供应商原始口径，不与股票或其他来源直接比较"] if is_index(symbol) else [])
