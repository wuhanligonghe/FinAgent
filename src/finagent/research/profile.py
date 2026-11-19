from dataclasses import dataclass


@dataclass(frozen=True)
class AgentProfile:
    name: str
    system_prompt: str
    infer_required_tools: bool = False
