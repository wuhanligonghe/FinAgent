from __future__ import annotations

import os
from pathlib import Path
import tomllib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, protected_namespaces=())


class Limits(StrictModel):
    model_calls: int = Field(20, ge=1, le=100)
    tool_calls: int = Field(20, ge=1, le=200)
    tools_per_turn: int = Field(5, ge=1, le=20)
    source_requests: int = Field(60, ge=1, le=500)
    wall_seconds: float = Field(180, gt=0, le=3600)
    request_seconds: float = Field(20, gt=0, le=60)
    # Accepted for old config files; natural-language completion does not retry
    # on missing JSON fields or financial final-answer schemas.
    finish_corrections: int = Field(2, ge=0, le=5)
    repeated_observations: int = Field(3, ge=2, le=10)
    context_chars: int = Field(60000, ge=12000, le=500000)
    context_summary_turns: int = Field(8, ge=0, le=100)
    context_keep_turns: int = Field(3, ge=1, le=20)


class ModelSettings(StrictModel):
    base_url: str = ""
    name: str = ""
    api_key_env: str = "FINAGENT_API_KEY"
    api_key: SecretStr = Field(default=SecretStr(""), exclude=True)
    max_output_tokens: int = Field(3000, ge=128, le=32000)
    protocol: Literal["auto", "chat_completions", "anthropic_messages"] = "auto"
    # Native tool_reference/defer_loading requires an Anthropic-compatible API.
    tool_loading: Literal["eager", "deferred"] = "eager"
    prompt_caching: bool = False
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    fallback_model: str = ""
    proxy_url: str | None = None
    thinking: Literal["enabled", "disabled"] | None = None

    @field_validator("proxy_url")
    @classmethod
    def model_proxy(cls, value):
        from urllib.parse import urlsplit
        if not value:
            return None
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("模型代理必须是无凭证、路径和查询参数的 HTTP(S) 地址")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("模型代理端口无效")
        return value.rstrip("/")


class ContextSummarySettings(StrictModel):
    max_calls: int = Field(2, ge=0, le=5)
    cooldown_turns: int = Field(4, ge=1, le=20)
    timeout_seconds: float = Field(6, gt=0, le=20)
    total_seconds: float = Field(12, gt=0, le=40)
    max_output_tokens: int = Field(1200, ge=256, le=3000)
    max_chars: int = Field(1600, ge=512, le=8000)
    target_ratio: float = Field(0.6, ge=0.3, le=0.8)
    disable_thinking: bool = True


class ResearchSettings(StrictModel):
    max_concurrent: int = Field(3, ge=1, le=3)
    model_calls: int = Field(48, ge=1, le=200)
    tool_calls: int = Field(120, ge=1, le=1000)
    source_requests: int = Field(180, ge=1, le=1000)
    wall_seconds: float = Field(900, gt=0, le=7200)
    task_seconds: float = Field(180, gt=0, le=900)
    request_seconds: float = Field(20, gt=0, le=60)
    expert_model_calls: int = Field(6, ge=1, le=30)
    expert_tool_calls: int = Field(18, ge=1, le=100)
    followup_model_calls: int = Field(4, ge=1, le=10)
    max_attempts: int = Field(10, ge=1, le=10)
    reserve_model: int = Field(8, ge=0)
    reserve_tool: int = Field(12, ge=0)
    reserve_source: int = Field(12, ge=0)
    reserve_seconds: float = Field(120, ge=0)
    model_profiles: dict[str, ModelSettings] = Field(default_factory=dict)
    role_models: dict[str, str] = Field(default_factory=dict)

    @field_validator("role_models")
    @classmethod
    def role_names(cls, value):
        if set(value) - {"technical", "sentiment", "news", "fundamentals", "policy", "review", "synthesis", "quality_gate", "bull", "bear", "research_manager", "trader", "risk_aggressive", "risk_conservative", "risk_neutral", "portfolio_manager"}:
            raise ValueError("未知研究角色")
        return value

    @model_validator(mode="after")
    def valid_research(self):
        if any(total < reserve for total, reserve in ((self.model_calls, self.reserve_model), (self.tool_calls, self.reserve_tool),
                (self.source_requests, self.reserve_source), (self.wall_seconds, self.reserve_seconds))):
            raise ValueError("研究总预算不能低于保留池")
        if set(self.role_models.values()) - set(self.model_profiles):
            raise ValueError("研究角色引用未定义的模型配置")
        return self


class DecisionSettings(ResearchSettings):
    model_calls: int = Field(128, ge=1, le=200)
    tool_calls: int = Field(180, ge=1, le=1000)
    wall_seconds: float = Field(1800, gt=0, le=7200)
    max_attempts: int = Field(26, ge=1, le=100)
    reserve_model: int = 64
    reserve_tool: int = 60
    reserve_source: int = 0
    reserve_seconds: float = 720
    final_model: int = Field(4, ge=2)
    final_tool: int = Field(8, ge=2)
    final_seconds: float = Field(180, gt=0)
    debate_rounds: int = Field(2, ge=1, le=3)
    risk_rounds: int = Field(2, ge=1, le=3)
    manager_output_tokens: int = Field(12000, ge=128, le=32000)

    @model_validator(mode="after")
    def decision_valid(self):
        if any(a < b for a, b in ((self.reserve_model, self.final_model), (self.reserve_tool, self.final_tool), (self.reserve_seconds, self.final_seconds))):
            raise ValueError("下游保留池不能小于最终裁决保留")
        return self


class NLUSettings(StrictModel):
    enabled: bool = False
    backend: Literal["qwen3_ollama", "legacy_regex"] = "qwen3_ollama"
    endpoint: str = "http://127.0.0.1:11434"
    model: str = "finagent-intent:qwen3.5-9b-v2-f16"
    model_dir: Path = Path("models/finagent-qwen3-intent")
    timeout_seconds: float = Field(30, gt=0, le=120)
    context_tokens: int = Field(4096, ge=1024, le=32768)
    output_tokens: int = Field(1024, ge=128, le=4096)
    max_input_chars: int = Field(12000, ge=100, le=100000)
    expected_schema: Literal["nlu_sft_v2.1"] = "nlu_sft_v2.1"
    expected_digest: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")
    allow_legacy_regex: bool = False
    store_raw_text: bool = False
    # Explicit operator opt-in for candidate use; never fabricates qualification.
    allow_unqualified: bool = False

    @model_validator(mode="after")
    def explicit_legacy(self):
        if self.backend == "legacy_regex" and not self.allow_legacy_regex:
            raise ValueError("旧规则仅能通过 allow_legacy_regex 显式启用")
        from urllib.parse import urlsplit
        url = urlsplit(self.endpoint)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("NLU endpoint 必须是无凭据、查询和片段的 HTTP(S) Ollama 地址")
        if not self.model.strip() or self.output_tokens >= self.context_tokens:
            raise ValueError("NLU 模型名为空或生成预算不小于上下文窗口")
        return self


class IntentSettings(StrictModel):
    """Prompt-based tool routing, independent of the legacy fine-tuned NLU."""
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:11434"
    model: str = "qwen3.5:9b"
    timeout_seconds: float = Field(8, gt=0, le=120)
    context_tokens: int = Field(8192, ge=2048, le=32768)
    output_tokens: int = Field(256, ge=128, le=1024)
    max_input_chars: int = Field(4000, ge=100, le=16000)
    confidence_threshold: float = Field(0.65, ge=0, le=1)

    @model_validator(mode="after")
    def valid_service(self):
        from urllib.parse import urlsplit
        url = urlsplit(self.endpoint)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("意图服务必须是无凭据、查询和片段的 HTTP(S) Ollama 地址")
        if not self.model.strip() or self.output_tokens >= self.context_tokens:
            raise ValueError("意图模型名或生成预算无效")
        return self


class SecuritySettings(StrictModel):
    enabled: bool = True
    filter_inputs: bool = True
    tag_messages: bool = True
    detect_prompt_leaks: bool = True
    redact_pii: bool = True
    min_prompt_fragment_chars: int = Field(48, ge=24, le=256)
    sensitive_env_names: list[str] = Field(default_factory=lambda: [
        "FINAGENT_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
        "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "FINAGENT_MYSQL_URL", "FINAGENT_MYSQL_ADMIN_URL"])


class ClaimExtractionSettings(StrictModel):
    enabled: bool = False
    endpoint: str = "http://127.0.0.1:11435"
    model: str = "qwen3.5:9b"
    timeout_seconds: float = Field(20, gt=0, le=60)
    context_tokens: int = Field(32768, ge=4096, le=65536)
    output_tokens: int = Field(4096, ge=512, le=16384)
    max_input_chars: int = Field(12000, ge=100, le=50000)

    @model_validator(mode="after")
    def valid_service(self):
        from urllib.parse import urlsplit
        parsed = urlsplit(self.endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
            raise ValueError("主张提取地址须为无凭据、路径和查询参数的 HTTP(S) Ollama 地址")
        if not self.model.strip() or self.output_tokens >= self.context_tokens:
            raise ValueError("主张提取模型名为空或输出预算不小于上下文窗口")
        return self


class AnswerReviewSettings(ClaimExtractionSettings):
    """Whole-answer evidence review; independent of the legacy extractor."""
    backend: Literal["ollama", "main_model"] = "ollama"
    agentic: bool = False
    context_tokens: int = Field(65536, ge=4096, le=131072)
    timeout_seconds: float = Field(45, gt=0, le=120)
    max_evidence_chars: int = Field(40000, ge=1000, le=200000)
    max_evidence_results: int = Field(32, ge=1, le=128)
    protocol_retries: int = Field(1, ge=0, le=2)
    max_model_calls: int = Field(6, ge=1, le=20)
    max_tool_calls: int = Field(14, ge=1, le=60)
    initial_evidence_chars: int = Field(16000, ge=2000, le=60000)
    max_read_chars: int = Field(24000, ge=1000, le=100000)
    max_read_per_call: int = Field(6000, ge=200, le=16000)
    max_retrieval_calls: int = Field(4, ge=1, le=30)
    max_no_progress_calls: int = Field(2, ge=1, le=10)


class FactCheckSettings(StrictModel):
    enabled: bool = True
    max_corrections: int = Field(2, ge=0, le=5)
    extraction: ClaimExtractionSettings = Field(default_factory=ClaimExtractionSettings)
    review: AnswerReviewSettings = Field(default_factory=AnswerReviewSettings)


class HarnessSettings(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def discard_retired_task_setting(cls, value):
        if isinstance(value, dict) and "task_frames_required" in value:
            value = {k: v for k, v in value.items() if k != "task_frames_required"}
        return value

    workspace: Path = Path(".")
    memory_enabled: bool = True
    auto_memory: bool = True
    timezone: str = "Asia/Hong_Kong"
    stop_limit: int = Field(5, ge=1, le=20)
    model_retries: int = Field(2, ge=0, le=3)
    hint_timeout_seconds: float = Field(3, gt=0, le=30)
    mcp_servers: dict[str, dict] = Field(default_factory=dict)


class MemorySettings(StrictModel):
    user_id: str = Field("local", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    # Empty means a stable hash of the configured workspace, not the session ID.
    project_id: str = Field("", pattern=r"^[A-Za-z0-9_-]{0,64}$")
    core_limit: int = Field(4, ge=0, le=10)
    core_chars: int = Field(1600, ge=0, le=8000)
    recall_limit: int = Field(5, ge=1, le=20)
    recall_chars: int = Field(8000, ge=1000, le=30000)
    candidate_limit: int = Field(20, ge=1, le=100)
    semantic_min_score: float = Field(0.35, ge=-1, le=1)
    rerank: Literal["auto", "off", "always"] = "auto"
    rerank_backend: Literal["http", "chat", "disabled"] = "http"
    rerank_endpoint: str = "http://127.0.0.1:11436/v1"
    rerank_model: str = "Qwen/Qwen3-Reranker-8B"
    rerank_api_key_env: str = "FINAGENT_RERANK_API_KEY"
    rerank_api_key: SecretStr = Field(default=SecretStr(""), exclude=True)
    rerank_timeout_seconds: float = Field(30, gt=0, le=120)
    rerank_min_score: float = Field(0.5, ge=0, le=1)
    # HTTP adapters share the same interface; no vector database is required.
    embedding_backend: Literal["ollama", "openai", "disabled"] = "ollama"
    embedding_endpoint: str = "http://127.0.0.1:11434"
    embedding_model: str = "qwen3-embedding:8b"
    embedding_format: Literal["auto", "plain", "embeddinggemma", "qwen3"] = "auto"
    embedding_api_key_env: str = "FINAGENT_EMBEDDING_API_KEY"
    embedding_api_key: SecretStr = Field(default=SecretStr(""), exclude=True)
    embedding_timeout_seconds: float = Field(30, gt=0, le=60)

    @model_validator(mode="after")
    def valid_memory(self):
        from urllib.parse import urlsplit
        url = urlsplit(self.embedding_endpoint)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or not (url.scheme == "https" or (local and url.scheme == "http"))):
            raise ValueError("Embedding 地址须为无凭据/查询参数的 HTTPS API 前缀；本机允许 HTTP")
        if self.embedding_backend != "disabled" and not self.embedding_model.strip():
            raise ValueError("Embedding 模型名不能为空")
        url = urlsplit(self.rerank_endpoint)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or not (url.scheme == "https" or (local and url.scheme == "http"))):
            raise ValueError("Reranker 地址须为无凭据/查询参数的 HTTPS API 前缀；本机允许 HTTP")
        if self.rerank_backend == "http" and not self.rerank_model.strip():
            raise ValueError("Reranker 模型名不能为空")
        if self.candidate_limit < self.recall_limit:
            raise ValueError("记忆候选数不能小于召回数")
        return self


class DatabaseSettings(StrictModel):
    backend: Literal["mysql"] = "mysql"
    url: SecretStr = Field(default_factory=lambda: SecretStr(os.environ.get("FINAGENT_MYSQL_URL", "")), exclude=True)
    pool_size: int = Field(5, ge=1, le=64)
    max_overflow: int = Field(5, ge=0, le=64)
    pool_timeout: int = Field(5, ge=1, le=60)
    pool_recycle: int = Field(1800, ge=30)
    connect_timeout: int = Field(3, ge=1, le=30)
    read_timeout: int = Field(10, ge=1, le=120)
    write_timeout: int = Field(10, ge=1, le=120)
    ssl_ca: Path | None = None


class RuntimeSettings(StrictModel):
    # Redis is the primary runtime store, not a cache over SQLite.
    redis_url: SecretStr = Field(default=SecretStr("redis://127.0.0.1:6379/0"), exclude=True)
    namespace: str = Field("finagent:runtime:v3", pattern=r"^[A-Za-z0-9:_-]{1,64}$")
    connect_timeout: float = Field(3, gt=0, le=30)
    socket_timeout: float = Field(10, gt=0, le=60)

    @model_validator(mode="after")
    def valid_redis(self):
        from urllib.parse import urlsplit
        value = urlsplit(self.redis_url.get_secret_value())
        if value.scheme not in {"redis", "rediss", "unix"} or value.fragment:
            raise ValueError("Redis 地址须使用 redis://、rediss:// 或 unix://")
        if value.scheme != "unix" and not value.hostname:
            raise ValueError("Redis 地址缺少服务主机")
        if value.scheme == "unix" and not value.path.startswith("/"):
            raise ValueError("Redis Unix socket 须使用绝对路径")
        return self


class AuthSettings(StrictModel):
    mode: Literal["local", "server"] = "local"
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost", "::1"])
    allowed_origins: list[str] = Field(default_factory=list)
    registration: Literal["invite", "closed"] = "invite"
    session_seconds: int = Field(43200, ge=60, le=86400)
    idle_seconds: int = Field(7200, ge=60, le=86400)
    remember_seconds: int = Field(2592000, ge=60, le=7776000)
    remember_idle_seconds: int = Field(604800, ge=60, le=2592000)
    workers: int = Field(4, ge=1, le=32)
    research_global: int = Field(3, ge=1, le=32)
    research_per_user: int = Field(1, ge=1, le=8)
    queue_global: int = Field(32, ge=1, le=256)
    queue_per_user: int = Field(8, ge=1, le=64)
    runtime_limit: int = Field(64, ge=1, le=512)
    runtime_idle_seconds: int = Field(1800, ge=60, le=86400)

    @model_validator(mode="after")
    def deployment(self):
        from urllib.parse import urlsplit
        if not self.allowed_hosts or any(not h or any(c in h for c in "/:@*") for h in self.allowed_hosts if h != "::1"):
            raise ValueError("认证 Host 必须是明确的主机名，不接受通配符或端口")
        local_hosts = {'localhost', '127.0.0.1', '::1'}
        if self.mode == 'local' and set(self.allowed_hosts) - local_hosts:
            raise ValueError('本机模式只能配置回环 Host；外部域名必须使用服务器模式')
        for origin in self.allowed_origins:
            p = urlsplit(origin)
            if not p.hostname or p.username or p.password or p.query or p.fragment or p.path:
                raise ValueError("Origin 必须是完整源，不带路径、凭据和查询参数")
            if self.mode == 'local' and p.hostname not in local_hosts:
                raise ValueError('本机模式不接受外部 Origin')
            if p.scheme != "https" and not (self.mode == "local" and p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}):
                raise ValueError("服务器模式要求 HTTPS Origin")
        if self.mode == "server" and not self.allowed_origins:
            raise ValueError("服务器模式必须显式配置 HTTPS allowed_origins")
        return self


class Settings(StrictModel):
    data_dir: Path = Path(".finagent")
    data_proxy_url: str | None = None
    securities_file: Path | None = None
    request_timeout: float = Field(12, gt=0, le=20)
    source_retries: int = Field(1, ge=0, le=2)
    quote_sources: list[str] = Field(default_factory=lambda: ["tencent", "sina"])
    news_sources: list[str] = Field(default_factory=lambda: ["eastmoney", "sina"])
    model: ModelSettings = Field(default_factory=ModelSettings)
    limits: Limits = Field(default_factory=Limits)
    context_summary: ContextSummarySettings = Field(default_factory=ContextSummarySettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    decision: DecisionSettings = Field(default_factory=DecisionSettings)
    nlu: NLUSettings = Field(default_factory=NLUSettings)
    intent: IntentSettings = Field(default_factory=IntentSettings)
    fact_check: FactCheckSettings = Field(default_factory=FactCheckSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    harness: HarnessSettings = Field(default_factory=HarnessSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)

    @model_validator(mode="after")
    def review_model_identity(self):
        if self.fact_check.review.backend == "main_model":
            self.fact_check.review.model = self.model.name
        return self

    @field_validator("data_proxy_url")
    @classmethod
    def data_proxy(cls, value):
        return ModelSettings.model_proxy(value)

    @field_validator("quote_sources", "news_sources")
    @classmethod
    def unique_sources(cls, value, info):
        allowed = {"tencent", "sina"} if info.field_name == "quote_sources" else {"eastmoney", "sina"}
        if not value or len(value) != len(set(value)) or set(value) - allowed:
            raise ValueError(f"来源必须唯一且属于 {sorted(allowed)}")
        return value


def read_local_env(path: Path) -> dict[str, str]:
    """Read literal dotenv values without executing or expanding their contents."""
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key, value = key.strip(), value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or value[end + 1:].strip() and not value[end + 1:].strip().startswith("#"):
                raise ValueError("Invalid dotenv quoting")
            value = value[1:end]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def find_config(path: str | Path | None = None) -> Path:
    if path is not None:
        selected = Path(path).resolve()
        if not selected.is_file():
            raise ValueError("指定配置文件不存在")
        return selected
    directory = Path.cwd().resolve()
    for parent in (directory, *directory.parents):
        candidate = parent / "finagent.toml"
        if candidate.is_file():
            return candidate
    return directory / "finagent.toml"


def load_settings(path: str | Path | None = None) -> Settings:
    path = find_config(path)
    body = tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    model = body.setdefault("model", {})
    local_env = read_local_env(path.parent / ".env")

    def env_value(name):
        return os.environ.get(name, local_env.get(name, ""))
    if env_value("FINAGENT_DATA_PROXY"):
        body["data_proxy_url"] = env_value("FINAGENT_DATA_PROXY")
    for env, key in (("FINAGENT_BASE_URL", "base_url"), ("FINAGENT_MODEL", "name"),
                     ("FINAGENT_MODEL_PROXY", "proxy_url"), ("FINAGENT_TOOL_LOADING", "tool_loading")):
        if env_value(env):
            model[key] = env_value(env)
    if env_value("FINAGENT_PROMPT_CACHING"):
        value = env_value("FINAGENT_PROMPT_CACHING").lower()
        if value not in {"true", "false", "1", "0"}:
            raise ValueError("FINAGENT_PROMPT_CACHING 必须是 true/false/1/0")
        model["prompt_caching"] = value in {"true", "1"}
    nlu = body.setdefault("nlu", {})
    for env, key in (("FINAGENT_NLU_ENDPOINT", "endpoint"), ("FINAGENT_NLU_MODEL", "model"),
                     ("FINAGENT_NLU_MODEL_DIR", "model_dir"), ("FINAGENT_NLU_EXPECTED_DIGEST", "expected_digest")):
        if env_value(env):
            nlu[key] = env_value(env)
    intent = body.setdefault("intent", {})
    for env, key in (("FINAGENT_INTENT_ENDPOINT", "endpoint"), ("FINAGENT_INTENT_MODEL", "model")):
        if env_value(env):
            intent[key] = env_value(env)
    if env_value("FINAGENT_INTENT_ENABLED"):
        value = env_value("FINAGENT_INTENT_ENABLED").lower()
        if value not in {"true", "false", "1", "0"}:
            raise ValueError("FINAGENT_INTENT_ENABLED 必须是 true/false/1/0")
        intent["enabled"] = value in {"true", "1"}
    review = body.setdefault("fact_check", {}).setdefault("review", {})
    for env, key in (("FINAGENT_ANSWER_REVIEW_BACKEND", "backend"),
                     ("FINAGENT_ANSWER_REVIEW_ENDPOINT", "endpoint"),
                     ("FINAGENT_ANSWER_REVIEW_MODEL", "model")):
        if env_value(env):
            review[key] = env_value(env)
    if env_value("FINAGENT_ANSWER_REVIEW_ENABLED"):
        value = env_value("FINAGENT_ANSWER_REVIEW_ENABLED").lower()
        if value not in {"true", "false", "1", "0"}:
            raise ValueError("FINAGENT_ANSWER_REVIEW_ENABLED 必须是 true/false/1/0")
        review["enabled"] = value in {"true", "1"}
    extraction = body.setdefault("fact_check", {}).setdefault("extraction", {})
    for env, key in (("FINAGENT_CLAIM_EXTRACTION_ENDPOINT", "endpoint"),
                     ("FINAGENT_CLAIM_EXTRACTION_MODEL", "model")):
        if env_value(env):
            extraction[key] = env_value(env)
    if env_value("FINAGENT_CLAIM_EXTRACTION_ENABLED"):
        value = env_value("FINAGENT_CLAIM_EXTRACTION_ENABLED").lower()
        if value not in {"true", "false", "1", "0"}:
            raise ValueError("FINAGENT_CLAIM_EXTRACTION_ENABLED 必须是 true/false/1/0")
        extraction["enabled"] = value in {"true", "1"}
    model["api_key"] = SecretStr(env_value(model.get("api_key_env", "FINAGENT_API_KEY")))
    runtime = body.setdefault("runtime", {})
    database = body.setdefault("database", {})
    if env_value("FINAGENT_MYSQL_URL"):
        database["url"] = SecretStr(env_value("FINAGENT_MYSQL_URL"))
    if env_value("FINAGENT_REDIS_URL"):
        runtime["redis_url"] = SecretStr(env_value("FINAGENT_REDIS_URL"))
    memory = body.setdefault("memory", {})
    for env, key in (("FINAGENT_MEMORY_USER_ID", "user_id"), ("FINAGENT_MEMORY_PROJECT_ID", "project_id"),
                     ("FINAGENT_EMBEDDING_BACKEND", "embedding_backend"),
                     ("FINAGENT_EMBEDDING_ENDPOINT", "embedding_endpoint"),
                     ("FINAGENT_EMBEDDING_MODEL", "embedding_model"),
                     ("FINAGENT_MEMORY_RERANK", "rerank"),
                     ("FINAGENT_RERANK_BACKEND", "rerank_backend"),
                     ("FINAGENT_RERANK_ENDPOINT", "rerank_endpoint"),
                     ("FINAGENT_RERANK_MODEL", "rerank_model"),
                     ("FINAGENT_RERANK_TIMEOUT_SECONDS", "rerank_timeout_seconds"),
                     ("FINAGENT_RERANK_MIN_SCORE", "rerank_min_score")):
        if env_value(env):
            memory[key] = env_value(env)
    memory["embedding_api_key"] = SecretStr(env_value(memory.get("embedding_api_key_env", "FINAGENT_EMBEDDING_API_KEY")))
    memory["rerank_api_key"] = SecretStr(env_value(memory.get("rerank_api_key_env", "FINAGENT_RERANK_API_KEY")))
    for section in ("research", "decision"):
        for profile in body.get(section, {}).get("model_profiles", {}).values():
            profile["api_key"] = SecretStr(env_value(profile.get("api_key_env", "FINAGENT_API_KEY")))
    settings = Settings.model_validate(body)
    settings.data_dir = (path.parent / settings.data_dir).resolve()
    if settings.database.ssl_ca:
        settings.database.ssl_ca = (path.parent / settings.database.ssl_ca).resolve()
    if settings.securities_file:
        settings.securities_file = (path.parent / settings.securities_file).resolve()
    settings.nlu.model_dir = (path.parent / settings.nlu.model_dir).resolve()
    settings.harness.workspace = (path.parent / settings.harness.workspace).resolve()
    return settings
