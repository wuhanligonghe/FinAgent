from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from ..errors import AgentError
from .contracts import Observation


def write_json(path: Path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not append a UUID to already long content-hash filenames. Deep
    # research/evaluation directories otherwise exceed Windows path limits.
    tmp = path.with_name(f".write-{uuid4().hex}.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False, default=str), encoding="utf-8")
    tmp.replace(path)


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def save_raw(self, content: bytes, metadata: dict) -> str:
        # Metadata is part of identity: identical bytes fetched at different times
        # must not overwrite their acquisition history.
        digest = hashlib.sha256(content + json.dumps(metadata, sort_keys=True).encode()).hexdigest()
        key = f"raw_{digest}"
        directory = self.root / "evidence"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{key}.bin").write_bytes(content)
        write_json(directory / f"{key}.json", metadata)
        return key

    def save_result(self, result: Observation) -> Observation:
        result = result.model_copy(deep=True)
        result.result_id = "result_" + uuid4().hex
        write_json(self.root / "results" / f"{result.result_id}.json", result.model_dump(mode="json"))
        return result

    def load_result(self, key: str) -> Observation:
        if not re.fullmatch(r"result_[a-f0-9]{32}", key):
            raise AgentError("INVALID_EVIDENCE_ID", "规范结果 ID 无效")
        path = self.root / "results" / f"{key}.json"
        if not path.is_file():
            raise AgentError("EVIDENCE_NOT_FOUND", "规范结果不存在")
        return Observation.model_validate_json(path.read_text(encoding="utf-8"))

    def has_raw(self, key: str) -> bool:
        return bool(re.fullmatch(r"raw_[a-f0-9]{64}", key)) and all(
            (self.root / "evidence" / f"{key}.{suffix}").is_file() for suffix in ("bin", "json"))

    def read(self, key: str, offset: int = 0, limit: int = 6000) -> dict:
        if not re.fullmatch(r"(?:raw_[a-f0-9]{64}|result_[a-f0-9]{32})", key):
            raise AgentError("INVALID_EVIDENCE_ID", "只允许使用已登记证据 ID")
        if key.startswith("raw_"):
            path = self.root / "evidence" / f"{key}.bin"
            meta_path = path.with_suffix(".json")
            if not path.exists():
                raise AgentError("EVIDENCE_NOT_FOUND", "原始证据不存在")
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            content = path.read_bytes().decode(metadata.get("encoding", "utf-8"), errors="replace")
        else:
            path = self.root / "results" / f"{key}.json"
            if not path.exists():
                raise AgentError("EVIDENCE_NOT_FOUND", "规范结果不存在")
            metadata = {"kind": "normalized_result"}
            content = path.read_text(encoding="utf-8")
        return {"evidence_id": key, "metadata": metadata, "offset": offset,
                "total_chars": len(content), "text": content[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(content) else None}
