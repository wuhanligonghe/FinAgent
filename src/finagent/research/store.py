"""MySQL owns research admission, budgets, events and accepted references."""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from ..data.evidence import write_json
from ..errors import AgentError
from ..runtime import now


def process_identity(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None if ctypes.get_last_error() == 87 else "unknown"
        try:
            status = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value != 259:
                return None
            stamps = [wintypes.FILETIME() for _ in range(4)]
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(s) for s in stamps)):
                return "unknown"
            return str((stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime)
        finally:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return stat.read_text().rsplit(")", 1)[1].split()[19] if stat.exists() else "unknown"
    except ProcessLookupError:
        return None
    except PermissionError:
        return "unknown"


class ResearchStore:
    def __init__(self, root, *, database=None, scope=None):
        from ..storage.database import get_database
        from ..storage.scope import resolve_scope
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = database or get_database()
        self.scope = scope or resolve_scope(self.database, self.root)

    @classmethod
    def from_settings(cls, settings):
        from ..storage.database import get_database
        return cls(settings.data_dir, database=get_database(settings.database))

    def connect(self):
        return self.database.transaction(self.scope)

    def transaction(self):
        # The namespace row replaces the old per-file writer serialization.
        # Each workspace progresses independently; parent rows are also locked.
        return self.database.transaction(self.scope, lock=True)

    def directory(self, run):
        if not re.fullmatch(r"[a-f0-9]{32}", run):
            raise AgentError("INVALID_RESEARCH_ID", "研究 ID 无效")
        return self.root / "research" / run

    def get(self, run):
        self.directory(run)
        with self.connect() as db:
            row = db.one("research_runs", id=run)
        if row is None:
            raise AgentError("RESEARCH_NOT_FOUND", "研究不存在")
        return row

    def create(self, scope, config, fingerprint):
        run = uuid4().hex
        with self.transaction() as db:
            db.add("research_runs", dict(id=run, scope=scope.model_dump_json(), config=config.model_dump_json(),
                fingerprint=fingerprint, status="queued", content="unavailable", review="pending", created=now().isoformat()))
        directory = self.directory(run)
        directory.mkdir(parents=True)
        write_json(directory / "scope.json", scope.model_dump(mode="json"))
        return run

    @staticmethod
    def owned(db, run, owner):
        row = db.one("research_runs", id=run, lock=True)
        if row is None or row["owner"] != owner or row["status"] != "running":
            raise AgentError("STALE_RESEARCH_OWNER", "研究已停止或执行代次失效")
        return row

    def acquire(self, run, fingerprint, *, resume=False):
        owner = uuid4().hex
        def operation(db):
            row = db.one("research_runs", id=run, lock=True)
            if row is None:
                raise AgentError("RESEARCH_NOT_FOUND", "研究不存在")
            if row["owner"]:
                identity = process_identity(row["pid"])
                if identity is not None and (identity == "unknown" or identity == row["birth"]):
                    raise AgentError("RESEARCH_BUSY", "原执行进程仍存活，不能重复恢复")
            if row["fingerprint"] != fingerprint:
                raise AgentError("RESEARCH_VERSION_MISMATCH", "实现、模型或方法版本不兼容，请新建研究")
            if resume and (row["status"] == "cancelled" or row["status"] == "completed" and row["content"] == "complete"):
                raise AgentError("RESEARCH_TERMINAL", "已完成或取消的研究请只读查看；更新需新建研究")
            used = row["active_used"]
            if row["segment_started"]:
                config = json.loads(row["config"])
                used += min(max(0, time.time() - row["segment_started"]), config["task_seconds"])
                db.change("research_tasks", dict(status="interrupted", reason="进程中断"), run_id=run, status="running")
            db.change("research_runs", dict(owner=owner, pid=os.getpid(), birth=process_identity(os.getpid()),
                generation=row["generation"] + 1, status="running", active_used=used, segment_started=time.time()), id=run)
        self.database.atomic(operation, self.scope, lock=True)
        self.event(run, "research_resumed" if resume else "research_started")
        return owner

    def event(self, run, kind, task=None, **body):
        def operation(db):
            db.add("research_events", dict(seq=db.next_seq("research_events"), run_id=run, created=now().isoformat(),
                task_id=task, kind=kind, body=json.dumps(body, ensure_ascii=False, allow_nan=False)))
        self.database.atomic(operation, self.scope, lock=True)

    def events(self, run, after=0):
        from ..storage.schema import research_events as t
        self.get(run)
        with self.connect() as db:
            rows = db.rows(t.name, run_id=run, where=[t.c.seq > after], order=[t.c.seq])
        return [{**{k:v for k,v in r.items() if k not in ("run_id", "task_id")}, "run":r["run_id"], "task":r["task_id"]} for r in rows]

    @staticmethod
    def _task(row):
        value = dict(row)
        value["run"], value["id"] = value.pop("run_id"), value.pop("task_id")
        value.pop("seq", None)
        return value

    def tasks(self, run):
        from ..storage.schema import research_tasks as t
        with self.connect() as db:
            return [self._task(r) for r in db.rows(t.name, run_id=run, order=[t.c.seq])]

    def begin_task(self, run, owner, task, role, kind, max_attempts=10, max_retries=2):
        def operation(db):
            self.owned(db, run, owner)
            rows = db.rows("research_tasks", run_id=run)
            own = [r for r in rows if r["task_id"] == task]
            attempts = len(own)
            pairs = [(r, next((p for p in rows if p["task_id"] == r["task_id"] and p["attempt"] == r["attempt"] - 1), None)) for r in rows]
            retries = sum(p is not None and (r["kind"] in ("analyst", "followup") or p["status"] != "completed") for r,p in pairs)
            previous = max(own, key=lambda r:r["attempt"]) if own else None
            retry = bool(attempts and (kind in ("analyst", "followup") or previous["status"] != "completed"))
            denied = attempts >= 2 or retry and retries >= max_retries
            if max_retries == 4:
                retries = sum(p is not None and (p["status"] in ("failed", "interrupted", "cancelled") or p["reason"] == "partial_retry") for r,p in pairs)
                failed = sum(r["status"] in ("failed", "interrupted", "cancelled") for r in own)
                is_retry = bool(previous and (previous["status"] in ("failed", "interrupted", "cancelled") or previous["reason"] == "partial_retry"))
                denied = failed >= 2 or is_retry and retries >= max_retries
            if len(rows) >= max_attempts or denied:
                raise AgentError("RESEARCH_ATTEMPT_BUDGET", "研究任务尝试额度耗尽")
            attempt = attempts + 1
            db.add("research_tasks", dict(run_id=run, task_id=task, role=role, kind=kind, attempt=attempt,
                status="running", seq=db.next_seq("research_tasks")))
            return attempt
        attempt = self.database.atomic(operation, self.scope, lock=True)
        self.event(run, "task_started", task, attempt=attempt, role=role)
        return attempt

    def stage(self, run, owner, task, role, status, reason=""):
        with self.transaction() as db:
            self.owned(db, run, owner)
            old = db.one("research_stage_states", run_id=run, task_id=task)
            db.upsert("research_stage_states", dict(run_id=run, task_id=task, role=role, status=status, reason=reason,
                seq=old["seq"] if old else db.next_seq("research_stage_states")), ["role", "status", "reason"])

    def stages(self, run):
        from ..storage.schema import research_stage_states as t
        with self.connect() as db:
            return [self._task(r) for r in db.rows(t.name, run_id=run, order=[t.c.seq])]

    def invalidate(self, run, owner, task, reason="dependency_changed"):
        with self.transaction() as db:
            self.owned(db, run, owner)
            db.change("research_tasks", dict(status="invalidated", reason=reason), run_id=run, task_id=task, status="completed")
        self.event(run, "task_invalidated", task, reason=reason)

    def end_task(self, run, owner, task, attempt, status, artifact=None, reason=""):
        relative = checksum = None
        if artifact is not None:
            directory = self.directory(run) / "tasks" / task / str(attempt) / "artifacts" / uuid4().hex
            path = directory / "analysis.json"
            write_json(path, artifact)
            (directory / "report.md").write_text(artifact.get("report_markdown", artifact.get("summary", "")), encoding="utf-8")
            relative, checksum = str(path.relative_to(self.root)), sha256(path.read_bytes()).hexdigest()
        with self.transaction() as db:
            self.owned(db, run, owner)
            changed = db.change("research_tasks", dict(status=status, artifact=relative, hash=checksum, reason=reason),
                run_id=run, task_id=task, attempt=attempt, status="running")
            if not changed:
                raise AgentError("STALE_TASK_ATTEMPT", "任务尝试已终止，不接纳晚到结果")
        self.event(run, "task_finished", task, attempt=attempt, status=status, reason=reason)

    def read_artifact(self, row):
        if not row.get("artifact"):
            return None
        path = (self.root / row["artifact"]).resolve()
        if not path.is_relative_to(self.directory(row["run"]).resolve()) or not path.is_file():
            raise AgentError("ARTIFACT_MISSING", "研究产物不存在或路径无效")
        if sha256(path.read_bytes()).hexdigest() != row["hash"]:
            raise AgentError("ARTIFACT_CHANGED", "研究产物哈希不匹配")
        return json.loads(path.read_text(encoding="utf-8"))

    def grant(self, run, owner, task, result_id, tool):
        with self.transaction() as db:
            self.owned(db, run, owner)
            db.upsert("research_grants", dict(run_id=run, task_id=task, result_id=result_id, tool=tool), [])

    def grants(self, run, task=None):
        with self.connect() as db:
            rows = db.rows("research_grants", run_id=run, **({"task_id": task} if task else {}))
        return [{"run":r["run_id"], "task":r["task_id"], "result_id":r["result_id"], "tool":r["tool"]} for r in rows]

    def finish(self, run, owner, status, content, review, report=None, reason=""):
        with self.transaction() as db:
            row = self.owned(db, run, owner)
            used = row["active_used"] + max(0, time.time() - row["segment_started"])
            db.change("research_runs", dict(status=status, content=content, review=review, report=report, reason=reason,
                active_used=used, segment_started=None, owner=None), id=run)
            if status in ("cancelled", "interrupted", "failed"):
                db.change("research_tasks", dict(status=status, reason=reason), run_id=run, status="running")
        self.event(run, "research_finished", status=status, content_status=content, reason=reason)

    def snapshot(self, run):
        row = self.get(run)
        row.pop("owner", None)
        row["scope"], row["config"] = json.loads(row["scope"]), json.loads(row["config"])
        row["tasks"], row["budget"], row["stages"] = self.tasks(run), self.usage(run), self.stages(run)
        return row

    def usage(self, run):
        from collections import Counter
        with self.connect() as db:
            rows = db.rows("research_reservations", run_id=run)
        counts = Counter(r["kind"] for r in rows)
        usage = [json.loads(r["usage"]) for r in rows if r["kind"] == "model" and r["usage"]]
        known = counts.get("model", 0) == len(usage) and all(isinstance(u.get("prompt_tokens"), int) and isinstance(u.get("completion_tokens"), int) for u in usage)
        return {"calls": dict(counts), "token_status": "reported" if known else "unknown", "prompt_tokens": sum(u["prompt_tokens"] for u in usage) if known else None,
                "completion_tokens": sum(u["completion_tokens"] for u in usage) if known else None, "cost": None}

    def reserve_budget(self, run, owner, kind, task, total, held):
        operation = uuid4().hex
        def reserve(db):
            row = self.owned(db, run, owner)
            stamp = time.time()
            if db.count("research_reservations", run_id=run, kind=kind) >= total - held:
                raise AgentError("RESEARCH_BUDGET", f"研究{kind}调用额度耗尽")
            db.change("research_runs", dict(active_used=row["active_used"] + max(0, stamp - row["segment_started"]), segment_started=stamp), id=run)
            db.add("research_reservations", dict(id=operation, run_id=run, task_id=task, kind=kind, state="pending", created=now().isoformat()))
        self.database.atomic(reserve, self.scope, lock=True)
        return operation

    def settle_reservation(self, run, owner, operation, usage, state):
        with self.transaction() as db:
            row = db.one("research_runs", id=run, lock=True)
            if row and row["owner"] == owner and row["status"] == "running":
                stamp = time.time()
                db.change("research_runs", dict(active_used=row["active_used"] + max(0, stamp - row["segment_started"]), segment_started=stamp), id=run)
            db.change("research_reservations", dict(state=state, usage=json.dumps(usage, allow_nan=False) if usage is not None else None),
                id=operation, run_id=run, state="pending")

    def create_selection(self, identifier, scope_hash, source):
        with self.transaction() as db:
            db.add("research_execution_selections", dict(id=identifier, scope_hash=scope_hash, source=source, created=now().isoformat()))

    def consume_selection(self, scope):
        from datetime import datetime
        with self.transaction() as db:
            row = db.one("research_execution_selections", id=scope.selection_id, lock=True)
            if (not row or row["scope_hash"] != scope.scope_hash or row["source"] != scope.selection_source or row["consumed"] or
                    (now() - datetime.fromisoformat(row["created"])).total_seconds() > 900):
                raise AgentError("RESEARCH_DENIED", "研究选择无效、过期、范围改变或已使用，请重新选择")
            db.change("research_execution_selections", {"consumed": 1}, id=scope.selection_id, consumed=0)

    def bind_selection(self, scope, run):
        with self.transaction() as db:
            row = db.one("research_execution_selections", id=scope.selection_id, lock=True)
            if not row or not row["consumed"] or row["run_id"] not in (None, run):
                raise AgentError("RESEARCH_DENIED", "研究选择不能绑定至该任务")
            db.change("research_execution_selections", {"run_id": run}, id=scope.selection_id)
