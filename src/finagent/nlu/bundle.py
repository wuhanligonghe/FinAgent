"""Content-addressed deployment metadata; a model tag alone never grants admission."""
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import Field
from ..config import StrictModel
from ..errors import AgentError
from .semantic import PROMPT_HASH, SCHEMA_HASH, strict_json


class Bundle(StrictModel):
    schema_version: Literal["nlu_sft2.bundle.1"]
    model_id: str = Field(min_length=1)
    ollama_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    gguf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    tokenizer_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    schema_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_model: Literal["Qwen/Qwen3.5-9B"]
    base_revision: str = Field(min_length=1)
    context_tokens: int = Field(ge=1024, le=32768)
    output_tokens: int = Field(ge=128, le=4096)
    template_verified: Literal[True]
    quality_status: Literal["diagnostic", "qualified"] = "diagnostic"
    qualification_sha256: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")

    def fingerprint(self):
        body = self.model_dump(exclude={"quality_status", "qualification_sha256"})
        return sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_bundle(config, *, diagnostic=False):
    directory = config.model_dir.resolve()
    if not (directory / "manifest.json").is_file():
        raise AgentError("NLU_MODEL_MISSING", "缺少已微调的 Qwen3 部署清单；请导出部署包并配置 nlu.model_dir，使用 nlu-check 检查")
    try:
        bundle = Bundle.model_validate(strict_json((directory / "manifest.json").read_text(encoding="utf-8")), strict=True)
        if bundle.model_id != config.model or bundle.prompt_sha256 != PROMPT_HASH or bundle.schema_sha256 != SCHEMA_HASH:
            raise ValueError("模型tag、prompt或schema与部署包不一致")
        if config.expected_digest and bundle.ollama_digest != config.expected_digest:
            raise ValueError("模型摘要不符合配置的固定摘要")
        if (config.context_tokens, config.output_tokens) != (bundle.context_tokens, bundle.output_tokens):
            raise ValueError("推理token预算与已验证部署包不一致")
        if file_hash(directory / "tokenizer.json") != bundle.tokenizer_sha256:
            raise ValueError("tokenizer文件缺损或内容变化")
    except (ValueError, OSError, TypeError) as exc:
        raise AgentError("NLU_MODEL_INCOMPATIBLE", "Qwen3部署包或版本校验失败：" + str(exc)) from exc
    if not diagnostic:
        # An explicit local setting permits candidate operation only. Invalid
        # qualified reports still fail the normal verification below.
        if bundle.quality_status == "diagnostic" and config.allow_unqualified and bundle.qualification_sha256 is None:
            return bundle
        if bundle.quality_status != "qualified" or not bundle.qualification_sha256:
            raise AgentError("NLU_MODEL_UNQUALIFIED", "模型尚未通过独立人工质量与宿主验收；仅可使用 nlu-parse --diagnostic 诊断")
        try:
            report_path = directory / "qualification.json"
            if file_hash(report_path) != bundle.qualification_sha256:
                raise ValueError("质量报告哈希变化")
            report = strict_json(report_path.read_text(encoding="utf-8"))
            from ..provenance import package_digest
            if (report.get("schema_version") != "nlu_sft2.host_acceptance.1" or
                    report.get("deployment_fingerprint") != bundle.fingerprint() or
                    report.get("implementation") != package_digest() or
                    report.get("host_gates_passed") is not True or report.get("semantic_gates_passed") is not True or
                    report.get("independent_human_samples", 0) < 300):
                raise ValueError("质量报告未通过或与当前模型／应用版本不符")
        except (ValueError, OSError, TypeError, KeyError) as exc:
            raise AgentError("NLU_MODEL_UNQUALIFIED", "生产验收报告无效：" + str(exc)) from exc
    return bundle
