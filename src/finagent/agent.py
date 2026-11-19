from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from importlib.resources import files
import json
import re
from pathlib import Path
from time import monotonic
from typing import Callable
from uuid import uuid4

from . import __version__
from .data.contracts import Observation, error_observation
from .data.evidence import EvidenceStore, write_json
from .errors import AgentError
from .intent import requires_data
from .models import ChatModel
from .runtime import ExecutionContext, now
from .provenance import package_digest
from .presentation import ProgressReporter, failure_answer, format_answer, format_sources
from .tools.registry import ToolRegistry
from .terminal import read_terminal_response, response_records


@dataclass
class RunRequest:
    goal: str
    required_tools: tuple[str, ...] = ()
    require_data: bool | None = None


@dataclass
class RunResult:
    run_id: str
    status: str
    answer: str
    reason: str
    model_calls: int
    tool_calls: int
    source_requests: int
    elapsed_seconds: float
    evidence_ids: list[str]
    run_dir: Path
    calculation_ids: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str = ""
    error_retryable: bool = False

    def raise_for_error(self):
        if self.error_code:
            raise AgentError(self.error_code, self.error_message, self.error_retryable)


# Compatibility contract only. Execution is owned by the shared v3 Kernel.
from .harness.compat import ResearchLoop as AgentLoop
