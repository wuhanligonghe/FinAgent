"""Versioned session snapshots and durable, acknowledged event delivery."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache, wraps
from hashlib import sha256
import fcntl
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from ..config import RuntimeSettings
from ..errors import AgentError


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise AgentError("INVALID_ID", "标识只能包含字母、数字、短横线和下划线")
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(encode(value), encoding="utf-8")
    temporary.replace(path)


def empty_state(session_id, workspace):
    return dict(schema_version=3, session_id=identity(session_id), revision=0,
                workspace=str(Path(workspace).resolve()), focus=None, subjects=[],
                temporal_context={}, user_constraints=[], objectives=[], known_facts=[],
                hypotheses=[], open_questions=[], failed_attempts=[], next_actions=[],
                artifact_refs=[], evidence_index_ref={}, research_links=[], todos=[],
                active_goal_ref=None, usage={}, loaded_skills={}, pending_clarification=None,
                transcript_ref=f"sessions/{session_id}/transcript.jsonl")


@lru_cache(maxsize=16)
def redis_client(url, connect_timeout, socket_timeout):
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=connect_timeout,
                                socket_timeout=socket_timeout, health_check_interval=30, max_connections=64,
                                retry=Retry(NoBackoff(), 0))


def redis_operation(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except redis.RedisError:
            # Never expose connection URLs/passwords or fall back to SQLite.
            raise AgentError("RUNTIME_UNAVAILABLE", "Redis 会话存储不可用，请检查服务连接及持久化状态", True) from None
    return guarded


class RuntimeStore:
    """Primary Redis checkpoints, ordered events and transactional task state.

    Files still hold transcripts, offloaded outputs and single-host leases.
    Keys have no TTL; Redis persistence owns runtime durability.
    """

    @redis_operation
    def __init__(self, root, *, settings=None, allow_legacy=False):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        settings = settings or RuntimeSettings(redis_url=os.environ.get("FINAGENT_REDIS_URL", "redis://127.0.0.1:6379/0"))
        self.client = redis_client(settings.redis_url.get_secret_value(), settings.connect_timeout, settings.socket_timeout)
        self.prefix = settings.namespace + ":{" + sha256(str(self.root).encode()).hexdigest() + "}:"
        self.client.ping()
        meta = self.prefix + "meta"
        self.marker = self.root / ".runtime_redis.json"
        if not allow_legacy:
            if (self.root / "runtime.sqlite3").exists() and not self.client.hget(meta, "legacy_import"):
                raise AgentError("RUNTIME_MIGRATION_REQUIRED", "发现旧会话数据库，请停写后执行 Redis 迁移；不会自动打开空会话")
            if self.marker.exists() and not self.client.hexists(meta, "schema_version"):
                raise AgentError("RUNTIME_DATA_MISSING", "Redis 会话命名空间缺失，请恢复持久化数据；不会静默重建")
            if self.marker.exists() and json.loads(self.marker.read_text())["namespace"] != self.prefix:
                raise AgentError("RUNTIME_NAMESPACE_CHANGED", "Redis 命名空间已变化，请显式迁移原会话")
        self.client.hsetnx(meta, "schema_version", "1")
        if not allow_legacy and not self.marker.exists():
            atomic_json(self.marker, {"backend": "redis", "namespace": self.prefix})

    @contextmanager
    def lease(self, name):
        path = self.root / "locks" / (identity(name) + ".lock")
        path.parent.mkdir(exist_ok=True)
        with path.open("a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AgentError("SESSION_BUSY", "该会话或工作流正在由另一个执行者处理") from exc
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @redis_operation
    def load(self, session_id):
        raw = self.client.hget(self.prefix + "sessions", identity(session_id))
        return json.loads(raw) if raw else None

    @redis_operation
    def save(self, session_id, body):
        self.client.hset(self.prefix + "sessions", identity(session_id), encode(body))

    @redis_operation
    def sessions(self):
        return sorted(self.client.hkeys(self.prefix + "sessions"))

    @redis_operation
    def emit(self, session, kind, payload=None, event_id=None, notify=False):
        identity(session)
        event_id = event_id or uuid4().hex
        body = dict(event_id=event_id, session_id=session, kind=kind,
                    time=datetime.now().astimezone().isoformat(), payload=payload or {})
        stored = self.client.eval("""
            local old = redis.call('HGET', KEYS[2], ARGV[1])
            if old then return old end
            local seq = redis.call('INCR', KEYS[1])
            redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
            redis.call('ZADD', KEYS[3], seq, ARGV[1])
            redis.call('ZADD', KEYS[4], seq, ARGV[1])
            if ARGV[3] == '1' then redis.call('ZADD', KEYS[5], seq, ARGV[1]) end
            return ARGV[2]
        """, 5, self.prefix + "sequence", self.prefix + "event_bodies", self.prefix + "events",
            self.prefix + "events:" + session, self.prefix + "pending:" + session,
            event_id, encode(body), "1" if notify else "0")
        return json.loads(stored)

    @redis_operation
    def pending(self, session):
        ids = self.client.zrange(self.prefix + "pending:" + identity(session), 0, -1)
        return [json.loads(raw) for raw in self.client.hmget(self.prefix + "event_bodies", ids) if raw] if ids else []

    @redis_operation
    def ack(self, ids):
        if ids:
            self.client.eval("""
                for i=2,#ARGV do
                    local body = redis.call('HGET', KEYS[1], ARGV[i])
                    if body then
                        local session = cjson.decode(body)['session_id']
                        redis.call('ZREM', ARGV[1] .. 'pending:' .. session, ARGV[i])
                    end
                end
                return 1
            """, 1, self.prefix + "event_bodies", self.prefix, *ids)

    @redis_operation
    def events(self, session=None, *, after=0, limit=10000, kind=None):
        """Return explicit seq/body records for projection or diagnostics."""
        index = self.prefix + "events" + (":" + identity(session) if session is not None else "")
        rows, cursor = [], int(after)
        while len(rows) < limit:
            page = self.client.zrangebyscore(index, "(" + str(cursor), "+inf", start=0,
                                            num=min(100, limit - len(rows)), withscores=True)
            if not page:
                break
            raw = self.client.hmget(self.prefix + "event_bodies", [key for key, _ in page])
            for (_, sequence), body in zip(page, raw):
                cursor = int(sequence)
                if body and (kind is None or json.loads(body)["kind"] == kind):
                    rows.append({"seq": cursor, "body": body})
        return rows

    def record_key(self, table, session):
        if table not in {"tasks", "jobs", "work"}:
            raise ValueError("未知运行记录类型")
        return self.prefix + table + ":" + identity(session)

    @redis_operation
    def records(self, table, session):
        key = self.record_key(table, session)
        ids = self.client.lrange(key + ":order", 0, -1)
        return [json.loads(raw) for raw in self.client.hmget(key, ids) if raw] if ids else []

    @redis_operation
    def put(self, table, session, body):
        key = self.record_key(table, session)
        self.client.eval("""
            if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 0 then
                redis.call('RPUSH', KEYS[2], ARGV[1])
            end
            redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
            redis.call('SADD', KEYS[3], ARGV[3])
            return 1
        """, 3, key, key + ":order", self.prefix + "record_sessions", body["id"], encode(body), session)

    @redis_operation
    def update_records(self, table, session, update):
        """Optimistic transaction over one session's records; callback has no side effects."""
        key = self.record_key(table, session)
        for _ in range(32):
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    before = pipe.hgetall(key)
                    records = {name: json.loads(raw) for name, raw in before.items()}
                    result = update(records)
                    after = {name: encode(body) for name, body in records.items()}
                    pipe.multi()
                    for name, raw in after.items():
                        if before.get(name) != raw:
                            pipe.hset(key, name, raw)
                        if name not in before:
                            pipe.rpush(key + ":order", name)
                    for name in before.keys() - after.keys():
                        pipe.hdel(key, name)
                        pipe.lrem(key + ":order", 0, name)
                    if after:
                        pipe.sadd(self.prefix + "record_sessions", session)
                    pipe.execute()
                    return result
                except redis.WatchError:
                    continue
        raise AgentError("RUNTIME_BUSY", "运行记录并发修改过多，请重试", True)

    @redis_operation
    def snapshot(self):
        """Atomic offline export, including event sequence and notification acknowledgments."""
        raw = self.client.eval("""
            local p = ARGV[1]
            local data = {sessions=redis.call('HGETALL',p..'sessions'),events={},records={}}
            local events = redis.call('ZRANGE',p..'events',0,-1,'WITHSCORES')
            for i=1,#events,2 do
                local body=redis.call('HGET',p..'event_bodies',events[i])
                local session=cjson.decode(body)['session_id']
                table.insert(data.events,{seq=tonumber(events[i+1]),body=body,
                    pending=redis.call('ZSCORE',p..'pending:'..session,events[i]) ~= false})
            end
            for _,session in ipairs(redis.call('SMEMBERS',p..'record_sessions')) do
                for _,kind in ipairs({'tasks','jobs','work'}) do
                    local key=p..kind..':'..session
                    for _,id in ipairs(redis.call('LRANGE',key..':order',0,-1)) do
                        table.insert(data.records,{kind=kind,session=session,id=id,body=redis.call('HGET',key,id)})
                    end
                end
            end
            return cjson.encode(data)
        """, 0, self.prefix)
        data = json.loads(raw)
        pairs = data["sessions"] or []
        return {"sessions": {pairs[i]: json.loads(pairs[i + 1]) for i in range(0, len(pairs), 2)},
                "events": [{**entry, "body": json.loads(entry["body"])} for entry in data["events"] or []],
                "records": [{**entry, "body": json.loads(entry["body"])} for entry in data["records"] or []]}

    @redis_operation
    def restore(self, snapshot, *, source):
        """Import into an empty namespace atomically; never replace live runtime data."""
        keys = [self.prefix + name for name in ("sessions", "events", "record_sessions", "meta")]
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(*keys)
                if pipe.hlen(keys[0]) or pipe.zcard(keys[1]) or pipe.scard(keys[2]):
                    raise AgentError("RUNTIME_NOT_EMPTY", "目标 Redis 会话命名空间非空，禁止覆盖")
                # Validate and encode before MULTI so invalid input cannot partly execute.
                sessions = {identity(name): encode(body) for name, body in snapshot["sessions"].items()}
                events = [(int(entry["seq"]), identity(entry["body"]["session_id"]),
                           entry["body"]["event_id"], encode(entry["body"]), bool(entry["pending"]))
                          for entry in snapshot["events"]]
                records = [(self.record_key(entry["kind"], entry["session"]), entry["session"],
                            entry["body"]["id"], encode(entry["body"])) for entry in snapshot["records"]]
                if (any(sequence <= 0 for sequence, *_ in events)
                        or len({entry[2] for entry in events}) != len(events)
                        or len({entry[0] for entry in events}) != len(events)):
                    raise ValueError("迁移事件序号或ID重复")
                pipe.multi()
                if sessions:
                    pipe.hset(keys[0], mapping=sessions)
                for sequence, session, event_id, body, pending in events:
                    pipe.hset(self.prefix + "event_bodies", event_id, body)
                    pipe.zadd(keys[1], {event_id: sequence})
                    pipe.zadd(self.prefix + "events:" + session, {event_id: sequence})
                    if pending:
                        pipe.zadd(self.prefix + "pending:" + session, {event_id: sequence})
                pipe.set(self.prefix + "sequence", max((entry[0] for entry in events), default=0))
                for key, session, record_id, body in records:
                    pipe.hset(key, record_id, body)
                    pipe.rpush(key + ":order", record_id)
                    pipe.sadd(keys[2], session)
                pipe.hset(keys[3], mapping={"schema_version": "1", "legacy_import": source})
                pipe.execute()
            except redis.WatchError:
                raise AgentError("RUNTIME_BUSY", "目标 Redis 存储已变化，迁移未覆盖数据", True) from None
        atomic_json(self.marker, {"backend": "redis", "namespace": self.prefix})

    def append_transcript(self, session, message):
        path = self.root / "sessions" / identity(session) / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(encode(message) + "\n")
