from __future__ import annotations

from datetime import date, datetime
import json
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator, field_validator

from ..config import StrictModel
from .symbols import normalize_symbol

RawID = Annotated[str, Field(pattern=r"^raw_[a-f0-9]{64}$")]


class ErrorInfo(StrictModel):
    code: str
    message: str
    retryable: bool = False


class Observation(StrictModel):
    status: Literal["ok", "partial", "empty", "error"]
    data: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None
    source_url: str | None = None
    fetched_at: datetime | None = None
    data_time: str | None = None
    market: str | None = None
    symbol: str | None = None
    currency: str | None = None
    units: dict[str, str] = Field(default_factory=dict)
    price_basis: str | None = None
    snapshot_id: RawID | None = None
    snapshot_ids: list[RawID] = Field(default_factory=list)
    result_id: str | None = None
    cache_hit: bool = False
    stale: bool = False
    warnings: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def check_contract(self):
        # Any-valued payloads still must be finite, JSON-serializable data;
        # reject them before persistence could leave a tool call unmatched.
        json.dumps(self.data, ensure_ascii=False, allow_nan=False)
        if self.fetched_at and self.fetched_at.tzinfo is None:
            raise ValueError("获取时间必须包含时区")
        if self.status == "error" and self.error is None:
            raise ValueError("错误结果必须有错误信息")
        if self.source_url and self.status != "error" and not (self.snapshot_id and self.fetched_at):
            raise ValueError("来源结果必须绑定原始快照和获取时间")
        if self.stale and self.status == "ok":
            raise ValueError("过期数据不能标为正常")
        return self


def error_observation(code: str, message: str, retryable: bool = False) -> Observation:
    return Observation(status="error", error=ErrorInfo(code=code, message=message, retryable=retryable))


class Quote(StrictModel):
    symbol: str
    name: str = Field(min_length=1)
    price: float = Field(gt=0)
    previous_close: float | None = Field(None, gt=0)
    open: float | None = Field(None, ge=0)
    high: float | None = Field(None, ge=0)
    low: float | None = Field(None, ge=0)
    volume_shares: float | None = Field(None, ge=0)
    amount_cny: float | None = Field(None, ge=0)
    pe_ttm: float | None = None
    pb: float | None = None
    data_time: datetime

    @field_validator("symbol")
    @classmethod
    def canonical(cls, value):
        return normalize_symbol(value)

    @model_validator(mode="after")
    def consistent_prices(self):
        if self.data_time.tzinfo is None:
            raise ValueError("行情时间必须包含时区")
        if self.high and self.low and self.high < self.low:
            raise ValueError("最高价小于最低价")
        if self.high and self.high < self.price or self.low and self.low > self.price:
            raise ValueError("现价超出高低价范围")
        return self


class DailyBar(StrictModel):
    date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume_shares: float = Field(ge=0)
    amount_cny: float | None = Field(None, ge=0)
    # The public source does not provide a trustworthy final-bar flag.
    finality: Literal["unknown"] = "unknown"

    @model_validator(mode="after")
    def ohlc(self):
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError("OHLC 关系异常")
        return self


class NewsItem(StrictModel):
    title: str = Field(min_length=1)
    snippet: str
    published_at: datetime
    time_precision: Literal["minute", "second", "date"]
    available_at: datetime
    publisher: str
    url: str
    locator: str
