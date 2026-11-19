"""MySQL projection of Web sessions and execution requests."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from ..errors import AgentError
from ..storage.database import get_database
from ..storage.scope import resolve_scope
from ..storage import schema as tables


def stamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


class WebStore:
    def __init__(self, root, *, database=None, scope=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = database or get_database()
        self.scope = scope or resolve_scope(self.database, self.root.parent)

    def db(self):
        return self.database.transaction(self.scope, lock=True)

    def create_session(self, title):
        value = dict(id="web_" + uuid4().hex, title=title, created_at=stamp())
        def operation(db):
            db.add("web_sessions", {**value, "seq": db.next_seq("web_sessions")})
        self.database.atomic(operation, self.scope, lock=True)
        return value

    @staticmethod
    def _session(db, session_id):
        row = db.one("web_sessions", id=session_id)
        if row is None:
            raise AgentError("NOT_FOUND", "会话不存在或不属于 Web 工作区")
        return row

    def session(self, session_id):
        with self.database.transaction(self.scope) as db:
            return self._session(db, session_id)

    def sessions(self, before=0, limit=50):
        with self.database.transaction(self.scope) as db:
            return db.rows("web_sessions", where=[tables.web_sessions.c.seq < before] if before else [],
                order=[tables.web_sessions.c.seq.desc()], limit=limit)

    def all_sessions(self):
        with self.database.transaction(self.scope) as db:
            return [r["id"] for r in db.rows("web_sessions")]

    def pending_count(self):
        with self.database.transaction(self.scope) as db:
            return db.count("web_runs", where=[tables.web_runs.c.status.in_(["queued", "running", "cancelling"])])

    def has_run(self, session_id, key):
        with self.database.transaction(self.scope) as db:
            return db.one("web_runs", session_id=session_id, idempotency_key=key) is not None

    def _event(self, db, session_id, kind, payload, source):
        if db.one("web_events", source=source):
            return
        self._session(db, session_id)
        body = dict(type=kind, session_id=session_id, occurred_at=stamp(), payload=payload)
        db.add("web_events", dict(seq=db.next_seq("web_events"), source=source, session_id=session_id, body=encode(body)))

    def create_run(self, session_id, key, command, max_pending=8):
        fingerprint = hashlib.sha256(encode(command).encode()).hexdigest()
        run_id = "run_" + uuid4().hex
        def operation(db):
            self._session(db, session_id)
            row = db.one("web_runs", session_id=session_id, idempotency_key=key)
            if row:
                if row["fingerprint"] != fingerprint:
                    raise AgentError("IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同请求")
                return self.decode_run(row), False
            if db.count("web_runs", where=[tables.web_runs.c.status.in_(["queued", "running", "cancelling"])]) >= max_pending:
                raise AgentError("QUEUE_FULL", "执行队列已满，请稍后重试")
            db.add("web_runs", dict(id=run_id, seq=db.next_seq("web_runs"), session_id=session_id, idempotency_key=key,
                fingerprint=fingerprint, command=encode(command), status="queued", created_at=stamp()))
            if command.get("text"):
                db.change("web_sessions", {"title": command["text"][:40]}, id=session_id, title="新对话")
            self._event(db, session_id, "run.queued", {"run_id": run_id}, "queued:" + run_id)
            return self.decode_run(db.one("web_runs", id=run_id)), True
        return self.database.atomic(operation, self.scope, lock=True)

    @staticmethod
    def decode_run(row):
        value = dict(row)
        value["key"] = value.pop("idempotency_key")
        value["command"] = json.loads(value["command"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        return value

    def run(self, run_id):
        with self.database.transaction(self.scope) as db:
            row = db.one("web_runs", id=run_id)
        if row is None:
            raise AgentError("NOT_FOUND", "执行记录不存在")
        return self.decode_run(row)

    def runs(self, session_id, before=0, limit=50):
        with self.database.transaction(self.scope) as db:
            self._session(db, session_id)
            rows = db.rows("web_runs", session_id=session_id, where=[tables.web_runs.c.seq < before] if before else [],
                order=[tables.web_runs.c.seq.desc()], limit=limit)
        return [self.decode_run(r) for r in reversed(rows)]

    def update_run(self, run_id, status, result=None, error=None):
        with self.db() as db:
            db.change("web_runs", dict(status=status, result=encode(result) if result is not None else None, error=error), id=run_id)

    def recover(self):
        with self.db() as db:
            db.change("web_runs", dict(status="interrupted", error="服务已停止；执行结果可能未知，未自动重试"),
                where=[tables.web_runs.c.status.in_(["queued", "running", "cancelling"])])

    def event(self, session_id, kind, payload, source=None):
        source = source or uuid4().hex
        self.database.atomic(lambda db: self._event(db, session_id, kind, payload, source), self.scope, lock=True)

    def project(self, session_id, rows, transform):
        # Transform outside the transaction; only deterministic DB writes retry.
        values = [(row, transform(json.loads(row["body"]))) for row in rows]
        def operation(db):
            self._session(db, session_id)
            for row, value in values:
                if value is not None:
                    kind, payload = value
                    self._event(db, session_id, kind, payload, f"runtime:{session_id}:{row['seq']}")
            if rows:
                prior = db.one("web_cursors", session_id=session_id)
                current = max([r["seq"] for r in rows] + [prior["runtime_seq"] if prior else 0])
                db.upsert("web_cursors", dict(session_id=session_id, runtime_seq=current), ["runtime_seq"])
        self.database.atomic(operation, self.scope, lock=True)

    def cursor(self, session_id):
        with self.database.transaction(self.scope) as db:
            row = db.one("web_cursors", session_id=session_id)
        return row["runtime_seq"] if row else 0

    def events(self, after=0, session_id=None, limit=100):
        conditions = [tables.web_events.c.seq > after]
        if session_id is not None:
            conditions.append(tables.web_events.c.session_id == session_id)
        with self.database.transaction(self.scope) as db:
            rows = db.rows("web_events", where=conditions, order=[tables.web_events.c.seq], limit=limit)
        return [{**json.loads(r["body"]), "sequence": r["seq"], "event_id": str(r["seq"])} for r in rows]
