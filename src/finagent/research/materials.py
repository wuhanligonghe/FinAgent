"""Versioned policy and signal imports. Models never supply filesystem paths."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re

from ..config import StrictModel
from ..data.contracts import Observation
from ..data.evidence import write_json
from ..errors import AgentError
from .text_material import parse_material
from ..runtime import TZ, now
from .contracts import PolicyImport, digest
from pydantic import Field, model_validator
from typing import Literal


class SignalImport(StrictModel):
    kind: Literal["fund_flow", "attention", "macro_news"]
    source: str = Field(min_length=1)
    source_url: str
    symbol: str | None = None
    available_at: datetime
    data_time: datetime
    definition: str = Field(min_length=1)
    units: dict[str, str] = Field(default_factory=dict)
    historical_version: bool = False

    @model_validator(mode="after")
    def valid(self):
        from urllib.parse import urlparse
        from ..data.symbols import normalize_symbol
        p = urlparse(self.source_url)
        if p.scheme != "https" or not p.hostname or p.username or p.password:
            raise ValueError("导入信号须有无凭证HTTPS来源")
        if self.available_at.tzinfo is None or self.data_time.tzinfo is None or self.data_time > self.available_at or self.available_at > now():
            raise ValueError("信号时间无效")
        if self.kind == "fund_flow" and not self.symbol:
            raise ValueError("资金流必须有证券")
        if self.symbol:
            self.symbol = normalize_symbol(self.symbol)
        return self


def validate_signal(kind, data):
    from decimal import Decimal
    if kind == "fund_flow":
        if not data.get("rows"):
            raise ValueError("资金流缺少rows")
        for row in data["rows"]:
            datetime.fromisoformat(row["date"])
            if not Decimal(str(row["main_net_cny"])).is_finite():
                raise ValueError("资金流不是有限金额")
    elif kind == "attention":
        if not isinstance(data.get("items"), list) or not data.get("coverage"):
            raise ValueError("关注度需要items及覆盖说明")
        for item in data["items"]:
            if not re.fullmatch(r"\d{6}\.(SH|SZ)", item["symbol"]):
                raise ValueError("热度证券无效")
    else:
        if not isinstance(data.get("articles"), list):
            raise ValueError("宏观新闻需要articles")
        for item in data["articles"]:
            if not item.get("title") or datetime.fromisoformat(item["available_at"]).tzinfo is None:
                raise ValueError("宏观新闻时间/标题无效")
    json.dumps(data, allow_nan=False)


class ResearchMaterials:
    def __init__(self, store):
        self.store = store

    def import_policy(self, path, spec: PolicyImport):
        path = Path(path)
        if path.stat().st_size > 50 * 1024 * 1024 or spec.published_at > now():
            raise AgentError("INVALID_POLICY", "政策过大或发布日期在未来")
        blocks, _ = parse_material(path)
        available = spec.published_at
        if spec.time_precision == "date":
            available = available.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        body = spec.model_dump(mode="json")
        policy_id = "policy_" + digest({"spec": body, "content": path.read_bytes().hex()})
        snapshot = self.store.save_raw(path.read_bytes(), {"source": "policy_import", "url": spec.source_url, "fetched_at": now().isoformat(), "encoding": "utf-8", "published_at": spec.published_at.isoformat()})
        body.update(policy_id=policy_id, snapshot_id=snapshot, available_at=available.isoformat(), blocks=blocks,
                    fetched_at=now().isoformat(), provenance="人工导入，来源身份与适用性需审阅")
        write_json(self.store.root / "policies" / f"{policy_id}.json", body)
        return body

    def policies(self, ctx, query="", days=90):
        found = []
        for path in sorted((self.store.root / "policies").glob("policy_*.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            if datetime.fromisoformat(doc["available_at"]) > ctx.as_of:
                continue
            # Explicit company mapping is a retrieval association, not proof of
            # business exposure; the analyst must cite the exposure separately.
            text = doc["title"] + " " + " ".join(doc["topics"] + doc["industries"])
            direct = ctx.symbol in doc["symbols"]
            thematic = bool(query and any(t in text for t in re.findall(r"[\w]+", query)))
            nationwide = "CN" in doc["regions"]
            if not (direct or thematic and nationwide):
                continue
            status = self.policy_status(doc, ctx.as_of)
            recent = datetime.fromisoformat(doc["published_at"]) >= ctx.as_of - timedelta(days=days)
            if recent or status in ("effective", "announced", "draft"):
                found.append({**doc, "as_of_status": status, "association": "explicit_import_mapping" if direct else "topic_match_unverified"})
        # Select a later revision only if it was already available by cutoff.
        superseded = {d["supersedes"] for d in found if d.get("supersedes")}
        return [d for d in found if d["policy_id"] not in superseded]

    @staticmethod
    def policy_status(doc, as_of):
        effective = datetime.fromisoformat(doc["effective_at"]) if doc.get("effective_at") else None
        expires = datetime.fromisoformat(doc["expires_at"]) if doc.get("expires_at") else None
        if expires and expires <= as_of:
            return "repealed"
        if doc["status"] == "draft":
            return "draft"
        if effective:
            return "effective" if effective <= as_of else "announced"
        return "unknown" if doc["status"] in ("effective", "repealed") else doc["status"]

    def search_policy(self, ctx, query="", days=90):
        docs = self.policies(ctx, query, days)[:12]
        ids = [d["snapshot_id"] for d in docs]
        ctx.evidence_ids.update(ids)
        return Observation(status="ok" if docs else "empty", source="local_policy_library", market="CN_A",
                           snapshot_id=ids[0] if ids else None, snapshot_ids=ids,
                           data={"policies": [{**{k: v for k, v in d.items() if k != "blocks"}, "locators": [b["locator"] for b in d["blocks"]]} for d in docs],
                                 "coverage": "仅本地带来源政策；匹配不等于适用性证明", "as_of": ctx.as_of.isoformat()})

    def read_policy(self, ctx, policy_id, locator):
        if not re.fullmatch(r"policy_[a-f0-9]{64}", policy_id):
            raise AgentError("INVALID_POLICY_ID", "政策ID无效")
        path = self.store.root / "policies" / f"{policy_id}.json"
        if not path.is_file():
            raise AgentError("POLICY_NOT_FOUND", "政策未入库")
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc["snapshot_id"] not in ctx.evidence_ids or datetime.fromisoformat(doc["available_at"]) > ctx.as_of:
            raise AgentError("EVIDENCE_DENIED", "政策未授权或晚于截止时间")
        blocks = [b for b in doc["blocks"] if b["locator"] == locator]
        if not blocks:
            raise AgentError("LOCATOR_NOT_FOUND", "原文位置不存在")
        return Observation(status="ok", source="local_policy_library", snapshot_id=doc["snapshot_id"],
                           data={**{k: v for k, v in doc.items() if k != "blocks"}, "blocks": blocks, "as_of_status": self.policy_status(doc, ctx.as_of)})

    def import_signal(self, path, spec: SignalImport):
        content = Path(path).read_bytes()
        if len(content) > 4 * 1024 * 1024:
            raise ValueError("信号超过4MiB")
        data = json.loads(content)
        validate_signal(spec.kind, data)
        if spec.kind == "fund_flow" and spec.units.get("main_net_cny") != "CNY":
            raise ValueError("资金流规范单位必须为CNY")
        if spec.kind == "fund_flow" and any(datetime.fromisoformat(r["date"]).date() > spec.data_time.date() for r in data["rows"]):
            raise ValueError("资金流行晚于声明数据日期")
        raw = self.store.save_raw(content, {"source": spec.source, "url": spec.source_url, "encoding": "utf-8", "fetched_at": now().isoformat()})
        body = {**spec.model_dump(mode="json"), "data": data, "snapshot_id": raw, "fetched_at": now().isoformat()}
        key = "signal_" + digest(body)
        write_json(self.store.root / "signals" / f"{key}.json", body)
        return {"signal_id": key, **body}

    def signal(self, kind, ctx, start, end):
        matches = []
        for path in (self.store.root / "signals").glob("signal_*.json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc["kind"] != kind or doc.get("symbol") not in (None, ctx.symbol):
                continue
            available = datetime.fromisoformat(doc["available_at"])
            if available > ctx.as_of or not start <= datetime.fromisoformat(doc["data_time"]).date() <= end:
                continue
            if ctx.as_of.date() < now().date() and not doc["historical_version"]:
                continue
            matches.append(doc)
        if not matches:
            return None
        doc = max(matches, key=lambda d: d["available_at"])
        data = dict(doc["data"])
        if kind == "fund_flow":
            data["rows"] = [r for r in data["rows"] if start <= datetime.fromisoformat(r["date"]).date() <= end
                            and datetime.fromisoformat(r.get("available_at", doc["available_at"])) <= ctx.as_of]
        elif kind == "macro_news":
            data["articles"] = [a for a in data["articles"] if datetime.fromisoformat(a["available_at"]) <= ctx.as_of
                                and start <= datetime.fromisoformat(a["available_at"]).date() <= end]
        data.update(mode="import", definition=doc["definition"], available_at=doc["available_at"], historical_version=doc["historical_version"])
        return Observation(status="ok", source=doc["source"], source_url=doc["source_url"], fetched_at=doc["fetched_at"],
                           snapshot_id=doc["snapshot_id"], symbol=doc.get("symbol"), units=doc["units"], data_time=doc["data_time"], data=data)
