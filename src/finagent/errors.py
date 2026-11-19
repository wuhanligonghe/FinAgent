from dataclasses import dataclass


@dataclass
class AgentError(Exception):
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
