from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator
from ..config import StrictModel

ROLES = ("technical", "sentiment", "news", "fundamentals", "policy")


class Entity(StrictModel):
    type: Literal["COMPANY", "STOCK_CODE", "LOOKBACK_WINDOW", "TIME_CUTOFF", "REPORT_PERIOD", "TRADE_DATE", "DECISION_HORIZON", "FINANCIAL_METRIC", "COMPARISON_MODE", "DISPLAY_UNIT"]
    role: Literal["target", "excluded", "background"] | None = None
    text: str = Field(min_length=1, max_length=1000)
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    score: float | None = Field(None, ge=0, le=1)

    @model_validator(mode="after")
    def valid(self):
        if self.end <= self.start or (self.type in ("COMPANY", "STOCK_CODE")) != (self.role is not None):
            raise ValueError("实体边界或证券角色无效")
        return self


class NLUResult(StrictModel):
    schema_version: Literal["nlu_sft_v2.1"] = "nlu_sft_v2.1"
    task_type: Literal["stock_analysis", "data_query", "concept_qa", "research_status", "research_report", "research_resume", "other"] = "other"
    dialogue_act: Literal["new_request", "follow_up", "switch_entity", "correct", "clarification_answer", "cancel", "other"] = "other"
    entity_binding: Literal["explicit_single", "explicit_multiple", "reference", "absent"] = "absent"
    dimension_state: dict[str, Literal["include", "exclude", "unspecified"]] = Field(default_factory=lambda: dict.fromkeys(ROLES, "unspecified"))
    entities: list[Entity] = Field(default_factory=list, max_length=64)
    scores: dict = Field(default_factory=dict)
    model_id: str = Field(min_length=1, max_length=200)
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    backend: Literal["qwen3_ollama"] = "qwen3_ollama"
    model_sha256: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    prompt_sha256: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    diagnostic_only: bool = False
    quality_status: Literal["diagnostic", "qualified"] | None = None
    unqualified_authorized: bool = False
    usage: dict = Field(default_factory=dict)
    input_status: Literal["ok", "too_long", "invalid"] = "ok"
    admission: Literal["accept", "clarify", "reject"] = "clarify"
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def all_dimensions(self):
        if set(self.dimension_state) != set(ROLES):
            raise ValueError("必须提供五个分析维度的状态")
        return self

    def check_text(self, text):
        end = 0
        for e in self.entities:
            if e.start < end or e.end > len(text) or text[e.start:e.end] != e.text:
                raise ValueError("实体并非当前原文中的有效片段")
            end = e.end
        return self
