"""Explicit compatibility profile for historical offline tests, never loaded by the product."""
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import re
from pydantic import Field
from ..config import StrictModel
from ..data.contracts import Observation
from ..errors import AgentError
from ..runtime import TZ
from ..market import resolve_market_day
from ..tools.registry import Tool
from .legacy_library import KnowledgeBase
from .legacy_financial import register_financial_tools

class EventArgs(StrictModel):
    trade_date: date
    lookback_days: int = Field(7, ge=1, le=30)

def events(args, ctx, store):
    scope = resolve_market_day(args.trade_date, ctx)
    day = date.fromisoformat(scope["trade_date"])
    start = day - timedelta(days=args.lookback_days)
    ctx.event_search_rounds += 1
    if ctx.event_search_rounds > 2:
        raise AgentError("RETRIEVAL_BUDGET", "公告检索已达两轮上限（7日至30日）")
    grouped = {}
    for doc in KnowledgeBase(store).visible(ctx):
        stamp = datetime.fromisoformat(doc["published_at"]).astimezone(TZ)
        if not start <= stamp.date() <= ctx.as_of.astimezone(TZ).date():
            continue
        if doc["time_precision"] == "date":
            relation = "date_only_unknown_order" if stamp.date() == day else "subsequent" if stamp.date() > day else "before_session"
        elif stamp > datetime.fromisoformat(scope["session_close"]):
            relation = "subsequent"
        elif stamp < datetime.combine(day, time(9, 30), TZ):
            relation = "before_session"
        else:
            relation = "during_session"
        key = doc["event_key"] or sha256(re.sub(r"\s|[：:，,。]", "", doc["title"]).encode()).hexdigest()
        candidate = {"event_id": key, "document_id": doc["document_id"], "snapshot_id": doc["snapshot_id"],
                     "title": doc["title"], "published_at": doc["published_at"], "time_precision": doc["time_precision"],
                     "available_at": doc["available_at"], "source_url": doc["source_url"], "source_type": doc["source_type"],
                     "publisher": doc["publisher"], "locator": doc["blocks"][0]["locator"], "event_type": doc["event_type"],
                     "relation": relation, "eligible_as_possible_factor": relation == "before_session",
                     "interpretation": "后续事项，禁止用于解释此前涨跌" if relation == "subsequent" else "相关事件，不能证明对整个区间涨跌的贡献",
                     "reprints": 0}
        old = grouped.get(key)
        if old:
            if old["source_type"] == "third_party" and candidate["source_type"] != "third_party":
                candidate["reprints"] = old["reprints"] + 1
                grouped[key] = candidate
            else:
                old["reprints"] += 1
        else:
            grouped[key] = candidate
    items = sorted(grouped.values(), key=lambda e: e["published_at"])
    snapshots = sorted({e["snapshot_id"] for e in items})
    ctx.document_ids.update(e["document_id"] for e in items)
    ctx.evidence_ids.update(snapshots)
    return Observation(status="partial" if items else "empty", source="local_public_disclosures", symbol=ctx.symbol, market="CN_A",
                       snapshot_id=snapshots[0] if snapshots else None, snapshot_ids=snapshots,
                       data={"events": items, "window_start": str(start), "window_end": ctx.as_of.isoformat(), "scope": scope,
                             "coverage": "仅已导入原始披露；不包含全网公告覆盖，无结果不等于没有利空"},
                       warnings=["时间先后不等于因果；日期级消息按次日零点保守可用，不能推断盘中顺序"])



def register_legacy_tools(registry, service):
    for name in ("generate_program", "execute_program"):
        registry.tools.pop(name, None)
    register_financial_tools(registry, service)
    registry.register(Tool("search_company_events", "Archived offline disclosure search", EventArgs,
        lambda a, c: events(a, c, service.store)))
