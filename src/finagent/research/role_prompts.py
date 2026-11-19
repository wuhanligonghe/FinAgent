"""Internal research methods, loaded only for the executing workflow role."""
from importlib.resources import files

from ..errors import AgentError


ROLE_PROMPTS = {
    "bull": "bull.md",
    "bear": "bear.md",
    "research_manager": "research_manager.md",
    "trader": "trader.md",
    "risk_aggressive": "risk_aggressive.md",
    "risk_conservative": "risk_conservative.md",
    "risk_neutral": "risk_neutral.md",
    "portfolio_manager": "portfolio_manager.md",
    "quality_gate": "quality_gate.md",
    "review": "review.md",
    # The legacy coordinator still supports synthesis; retain its method here.
    "synthesis": "synthesis.md",
}


def role_method(role):
    """Use an explicit role map, never a model-supplied path or skill name."""
    if role not in ROLE_PROMPTS:
        raise AgentError("UNKNOWN_RESEARCH_ROLE", "该角色没有登记内部研究方法")
    try:
        body = files("finagent").joinpath("research", "prompts", ROLE_PROMPTS[role]).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise AgentError("ROLE_PROMPT_UNAVAILABLE", "独立研究角色方法缺失或无法读取") from exc
    if not body:
        raise AgentError("ROLE_PROMPT_UNAVAILABLE", "独立研究角色方法为空")
    return body
