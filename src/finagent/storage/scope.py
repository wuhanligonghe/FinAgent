"""Host-owned workspace identities; a marker locates data, it never authorizes it."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import fcntl
import json
import re
import time
from uuid import uuid4

from ..errors import AgentError
from ..harness.state import atomic_json
from .database import Session


@dataclass(frozen=True)
class StorageScope:
    id: str
    root: Path
    owner_user_id: str | None


def resolve_scope(database, root, *, owner_user_id=None, importing=False):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.parent.name == "users":
        if not re.fullmatch(r"usr_[a-f0-9]{32}", root.name):
            raise ValueError("Invalid user workspace")
        if owner_user_id is not None and owner_user_id != root.name:
            raise ValueError("Workspace owner mismatch")
        owner_user_id = root.name
        owner = root / ".owner.json"
        if owner.is_symlink() or (owner.exists() and json.loads(owner.read_text()).get("user_id") != owner_user_id):
            raise ValueError("Workspace owner mismatch")
    marker = root / ".storage_namespace.json"
    if marker.is_symlink():
        raise ValueError("Workspace marker must not be a symlink")
    fingerprint = sha256(str(root).encode()).hexdigest()
    with (root / ".storage_namespace.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        with database.transaction(unchecked=importing) as s:
            row = s.one("storage_namespaces", root_fingerprint=fingerprint, lock=True)
            if row:
                if row["owner_user_id"] != owner_user_id or row["root_path"] != str(root):
                    raise ValueError("Workspace identity mismatch")
                if marker.exists() and json.loads(marker.read_text()).get("namespace_id") != row["id"]:
                    raise AgentError("DATABASE_NAMESPACE_CHANGED", "工作区标记与数据库不一致")
                if not importing and row["status"] != "ready":
                    raise AgentError("DATABASE_IMPORT_INCOMPLETE", "工作区尚未完成导入")
                if not marker.exists() and not importing:
                    raise AgentError("DATABASE_NAMESPACE_CHANGED", "工作区标记丢失，请核对并恢复标记")
                value = row["id"]
            else:
                if marker.exists():
                    raise AgentError("DATABASE_NAMESPACE_CHANGED", "MySQL 中缺少原工作区，请恢复数据库")
                if not importing and any(p.exists() for p in (root / "research.sqlite3", root / "web/web.sqlite3")):
                    raise AgentError("DATABASE_MIGRATION_REQUIRED", "发现 SQLite 业务数据，请先离线导入 MySQL")
                if not importing and any((root / "v3/memory").glob("users/*/embeddings.sqlite3")):
                    raise AgentError("DATABASE_MIGRATION_REQUIRED", "发现 SQLite 向量缓存，请先离线导入 MySQL")
                value = uuid4().hex
                s.add("storage_namespaces", dict(id=value, kind="web_user" if owner_user_id else "cli",
                    owner_user_id=owner_user_id, root_fingerprint=fingerprint, root_path=str(root),
                    status="importing" if importing else "ready", created_at=time.time()))
                scoped = Session(s.connection, value)
                for name in ("web_sessions", "web_runs", "web_events", "research_tasks", "research_stage_states", "research_events"):
                    scoped.add("namespace_sequences", {"sequence_name":name, "last_value":0})
        atomic_json(marker, {"schema_version": 1, "namespace_id": value})
    return StorageScope(value, root, owner_user_id)
