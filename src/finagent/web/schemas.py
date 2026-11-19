from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from ..config import StrictModel
from ..data.symbols import require_stock
from ..errors import AgentError


class SessionCreate(StrictModel):
    title: str = Field("新对话", min_length=1, max_length=100)


class MessageCreate(StrictModel):
    text: str = Field(min_length=1, max_length=12000)

    @field_validator("text")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("请输入问题")
        return value.strip()


class ResearchCreate(MessageCreate):
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    symbol: str
    mode: Literal["deep_research", "full_decision"] = "deep_research"
    as_of: datetime | None = None

    @field_validator("symbol")
    @classmethod
    def company(cls, value):
        try:
            return require_stock(value)
        except AgentError as exc:
            raise ValueError(exc.message) from exc

    @field_validator("as_of")
    @classmethod
    def cutoff(cls, value):
        from ..runtime import now
        if value is not None and (value.tzinfo is None or value > now()):
            raise ValueError("截止时间必须带时区且不晚于当前时间")
        return value
