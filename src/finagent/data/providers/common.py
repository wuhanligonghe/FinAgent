from __future__ import annotations

from datetime import datetime, timedelta
import math
import re

from ...errors import AgentError
from ...runtime import TZ
from ..contracts import Observation
from ..http import RawResponse


def number(value):
    if value in (None, "", "--", "-", "None"):
        return None
    try:
        value = float(str(value).replace(",", ""))
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def source_result(raw: RawResponse, symbol: str | None, data: dict, **kwargs) -> Observation:
    return Observation(status="ok", data=data, source=raw.source, source_url=raw.url,
                       fetched_at=datetime.fromisoformat(raw.fetched_at), market="CN_A", symbol=symbol,
                       snapshot_id=raw.snapshot_id, snapshot_ids=[raw.snapshot_id], **kwargs)


def publication_time(value) -> tuple[datetime, str, datetime]:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        text = datetime.strptime(text, "%Y%m%d").date().isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        day = datetime.fromisoformat(text).replace(tzinfo=TZ)
        return day, "date", day + timedelta(days=1)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentError("UNKNOWN_PUBLICATION_TIME", "来源未提供可验证的发布时间") from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TZ)
    precision = "second" if re.search(r":\d{2}:\d{2}", text) else "minute"
    return stamp, precision, stamp
