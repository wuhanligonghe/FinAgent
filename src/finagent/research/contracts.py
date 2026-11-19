from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..config import StrictModel
from ..data.symbols import normalize_symbol

Role = Literal["technical", "sentiment", "news", "fundamentals", "policy"]
ROLES = ("technical", "sentiment", "news", "fundamentals", "policy")
ContentStatus = Literal["complete", "partial", "unavailable"]


def digest(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


class ResearchRequest(StrictModel):
    goal: str = Field(min_length=1, max_length=12000)
    symbol: str
    depth: Literal["deep", "specialist"] = "deep"
    analysts: list[Role] = Field(default_factory=lambda: list(ROLES), min_length=1, max_length=5)
    as_of: datetime
    session_id: str | None = None
    market_window: int = Field(120, ge=60, le=250)
    news_days: int = Field(7, ge=1, le=30)
    policy_days: int = Field(90, ge=1, le=365)
    resolution_source_requests: int = Field(0, ge=0, le=20)

    @field_validator("symbol")
    @classmethod
    def symbol_valid(cls, value):
        return normalize_symbol(value)

    @field_validator("analysts")
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("分析师不可重复")
        return value

    @field_validator("as_of")
    @classmethod
    def aware(cls, value):
        from ..runtime import now
        if value.tzinfo is None or value > now():
            raise ValueError("截止时间必须带时区且不晚于现在")
        return value


class ResearchScope(ResearchRequest):
    model_config = {**StrictModel.model_config, "frozen": True}
    analysts: tuple[Role, ...] = Field(default=ROLES, min_length=1, max_length=5)
    market: Literal["CN_A"] = "CN_A"
    benchmarks: tuple[str, ...] = ("000300.SH",)
    scope_hash: str = ""

    @model_validator(mode="after")
    def fingerprint(self):
        expected = digest(self.model_dump(mode="json", exclude={"scope_hash"}))
        if self.scope_hash and self.scope_hash != expected:
            raise ValueError("研究范围哈希不匹配")
        object.__setattr__(self, "scope_hash", expected)
        return self


class EvidenceRef(StrictModel):
    result_id: str = Field(pattern=r"^result_[a-f0-9]{32}$")
    snapshot_id: str = Field(pattern=r"^raw_[a-f0-9]{64}$")
    locator: str = Field(min_length=1, max_length=500)


class Claim(StrictModel):
    claim_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    kind: Literal["fact", "calculation", "interpretation", "scenario"]
    text: str = Field(min_length=1, max_length=2500)
    horizon: str = Field("未指定", max_length=100)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    calculation_refs: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def supported(self):
        if not self.evidence_refs and not self.calculation_refs:
            raise ValueError("每个主张须附依据；情景另列假设")
        if self.kind == "calculation" and not self.calculation_refs:
            raise ValueError("计算主张须引用计算记录")
        if self.kind == "scenario" and not self.assumptions:
            raise ValueError("情景须列明假设")
        return self


class AnalysisSubmission(StrictModel):
    content_status: ContentStatus
    summary: str = Field(min_length=1, max_length=4000)
    report_markdown: str = Field(min_length=1, max_length=24000)
    claims: list[Claim] = Field(default_factory=list, max_length=60)
    missing: list[str] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    follow_up_requests: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def consistent(self):
        if len({c.claim_id for c in self.claims}) != len(self.claims):
            raise ValueError("主张 ID 不可重复")
        if self.content_status == "complete" and (not self.claims or self.missing):
            raise ValueError("完整产物需要有支持的主张且无关键缺口")
        return self


class ReviewIssue(StrictModel):
    task_id: str
    claim_id: str
    severity: Literal["critical", "major", "minor"]
    description: str = Field(min_length=1, max_length=1500)
    question: str = Field("", max_length=1500)
    success_condition: str = Field("", max_length=1000)


class ReviewSubmission(StrictModel):
    summary: str = Field(min_length=1, max_length=4000)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=40)


class SynthesisSubmission(StrictModel):
    report_markdown: str = Field(min_length=1, max_length=40000)
    claim_refs: list[str] = Field(default_factory=list, max_length=200)


class PolicyImport(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    source_url: str
    publisher: str = Field(min_length=1, max_length=200)
    published_at: datetime
    time_precision: Literal["date", "minute", "second"] = "date"
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    status: Literal["draft", "announced", "effective", "repealed", "unknown"] = "unknown"
    regions: list[str] = Field(default_factory=lambda: ["CN"])
    industries: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    version: str = Field("1", min_length=1, max_length=100)
    supersedes: str | None = None

    @model_validator(mode="after")
    def dates(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("政策原文必须记录 HTTPS 来源")
        for stamp in (self.published_at, self.effective_at, self.expires_at):
            if stamp is not None and stamp.tzinfo is None:
                raise ValueError("政策时间必须带时区")
        if self.effective_at and self.expires_at and self.expires_at < self.effective_at:
            raise ValueError("政策失效不能早于生效")
        self.symbols = [normalize_symbol(s) for s in self.symbols]
        return self
