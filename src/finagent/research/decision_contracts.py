"""Versioned v2.1 contracts. Legacy ResearchScope serialization stays unchanged."""
from __future__ import annotations

from typing import Literal
from pydantic import Field, model_validator
from ..config import StrictModel
from .contracts import ResearchRequest, ROLES, digest


class DecisionRequest(ResearchRequest):
    pipeline: Literal["trading"] = "trading"
    workflow_version: Literal["2.1"] = "2.1"
    debate_rounds: int = Field(2, ge=1, le=3)
    risk_rounds: int = Field(2, ge=1, le=3)
    decision_horizon: str = Field("未来 1—3 个月", min_length=1, max_length=160)
    risk_profile: Literal["unspecified", "aggressive", "balanced", "conservative"] = "unspecified"
    position_context: Literal["unknown", "not_held", "held"] = "unknown"

    @model_validator(mode="after")
    def deep_only(self):
        if self.depth != "deep":
            raise ValueError("完整决策流程须 depth=deep；专项使用 research")
        return self


class DecisionScope(DecisionRequest):
    model_config = {**StrictModel.model_config, "frozen": True}
    analysts: tuple = ROLES
    market: Literal["CN_A"] = "CN_A"
    benchmarks: tuple[str, ...] = ("000300.SH",)
    scope_hash: str = ""

    @model_validator(mode="after")
    def fingerprint(self):
        # Validate tuple members through the inherited request contract.
        if not self.analysts or len(set(self.analysts)) != len(self.analysts) or set(self.analysts) - set(ROLES):
            raise ValueError("无效分析师范围")
        value = digest(self.model_dump(mode="json", exclude={"scope_hash"}))
        if self.scope_hash and self.scope_hash != value:
            raise ValueError("研究范围哈希不匹配")
        object.__setattr__(self, "scope_hash", value)
        return self


class ItemRef(StrictModel):
    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    item_id: str = Field(min_length=1, max_length=100)


class Argument(StrictModel):
    argument_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    text: str = Field(min_length=1, max_length=1200)
    claim_refs: list[ItemRef] = Field(min_length=1, max_length=12)
    horizon: str = Field(min_length=1, max_length=160)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=8)


class Response(StrictModel):
    target: ItemRef
    stance: Literal["rebut", "accept", "qualify", "unresolved"]
    reason: str = Field(min_length=1, max_length=1200)
    claim_refs: list[ItemRef] = Field(default_factory=list, max_length=12)


class DebateTurn(StrictModel):
    side: Literal["bull", "bear"]
    round: int = Field(ge=1, le=3)
    turn_index: int = Field(ge=1, le=6)
    horizon: str | None = Field(None, max_length=160)
    arguments: list[Argument] = Field(default_factory=list, max_length=3)
    responses: list[Response] = Field(default_factory=list, max_length=3)
    concessions: list[str] = Field(default_factory=list, max_length=6)
    open_questions: list[str] = Field(default_factory=list, max_length=6)
    abstention_reason: str = ""
    no_response_reason: str = ""
    no_response_refs: list[ItemRef] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def valid_turn(self):
        if not self.arguments and not self.abstention_reason:
            raise ValueError("无论点须说明弃权原因")
        if len({a.argument_id for a in self.arguments}) != len(self.arguments):
            raise ValueError("论点ID重复")
        return self


class Ruling(StrictModel):
    target: ItemRef
    verdict: Literal["supported", "refuted", "conditional", "unresolved"]
    reason: str = Field(min_length=1, max_length=500)
    # The target is itself an evidence-bearing argument. Extra references are
    # needed only when the ruling relies on a different supporting claim.
    claim_refs: list[ItemRef] = Field(default_factory=list, max_length=12)


class Direction(StrictModel):
    decision_status: Literal["decided", "abstained"]
    reason: str = Field(min_length=1, max_length=2000)
    horizon: str = Field(min_length=1, max_length=160)
    conditions: list[str] = Field(min_length=1, max_length=12)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=12)
    review_triggers: list[str] = Field(min_length=1, max_length=12)
    claim_refs: list[ItemRef] = Field(default_factory=list, max_length=30)
    constraints: list[str] = Field(default_factory=list, max_length=12)
    unresolved: list[str] = Field(default_factory=list, max_length=12)


Rating = Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]
Action = Literal["Buy", "Hold", "Sell", "Wait"]


class ResearchPlan(Direction):
    rating: Rating | None = None
    issue_rulings: list[Ruling] = Field(default_factory=list, max_length=30)
    support_refs: list[ItemRef] = Field(default_factory=list, max_length=12)
    opposition_refs: list[ItemRef] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def direction(self):
        if (self.rating is not None) != (self.decision_status == "decided"):
            raise ValueError("评级和决策状态不一致")
        return self


class TraderProposal(Direction):
    action: Action | None = None
    plan_ref: ItemRef
    preconditions: list[str] = Field(min_length=1, max_length=12)
    action_triggers: list[str] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def direction(self):
        if (self.action is not None) != (self.decision_status == "decided"):
            raise ValueError("方向和决策状态不一致")
        return self


class RiskTurn(StrictModel):
    perspective: Literal["risk_aggressive", "risk_conservative", "risk_neutral"]
    round: int = Field(ge=1, le=3)
    turn_index: int = Field(ge=1, le=9)
    horizon: str | None = Field(None, max_length=160)
    proposal_ref: ItemRef
    assessment: Literal["accept", "modify", "reject", "abstain"]
    reason: str = Field(min_length=1, max_length=1500)
    risk_arguments: list[Argument] = Field(default_factory=list, max_length=3)
    responses: list[Response] = Field(default_factory=list, max_length=3)
    suggested_constraints: list[str] = Field(default_factory=list, max_length=12)
    unresolved: list[str] = Field(default_factory=list, max_length=12)
    abstention_reason: str = ""
    no_response_reason: str = ""
    no_response_refs: list[ItemRef] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def valid_turn(self):
        if not self.risk_arguments and (self.assessment != "abstain" or not self.abstention_reason):
            raise ValueError("无风险论点时须有理由弃权")
        if len({a.argument_id for a in self.risk_arguments}) != len(self.risk_arguments):
            raise ValueError("风险论点ID重复")
        return self


class PortfolioDecision(Direction):
    rating: Rating | None = None
    action: Action | None = None
    plan_ref: ItemRef
    proposal_ref: ItemRef
    risk_refs: list[ItemRef] = Field(min_length=1, max_length=12)
    accepted_rejected_views: list[Ruling] = Field(min_length=1, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def direction(self):
        if self.decision_status == "decided" and (self.rating is None or self.action is None):
            raise ValueError("最终决定需要评级和方向")
        if self.decision_status == "abstained" and (self.rating is not None or self.action is not None):
            raise ValueError("弃权不得带评级或方向")
        return self


class IssueResolution(StrictModel):
    issue_id: str
    state: Literal["open", "resolved", "withdrawn"]
    reason: str = Field(min_length=1, max_length=1500)
    claim_refs: list[ItemRef] = Field(default_factory=list, max_length=12)


class QualitySubmission(StrictModel):
    summary: str = Field(min_length=1, max_length=3000)
    resolutions: list[IssueResolution] = Field(default_factory=list, max_length=40)


SCHEMAS = {"bull": DebateTurn, "bear": DebateTurn, "research_manager": ResearchPlan,
           "trader": TraderProposal, "portfolio_manager": PortfolioDecision, "quality_gate": QualitySubmission,
           **{r: RiskTurn for r in ("risk_aggressive", "risk_conservative", "risk_neutral")}}
