"""Explicit MySQL schema, installed by Alembic rather than Store constructors."""
from sqlalchemy import (MetaData, Table, Column, String, BigInteger, Integer, Float,
                        ForeignKeyConstraint, UniqueConstraint, CheckConstraint, Index)
from sqlalchemy.dialects.mysql import LONGTEXT

metadata = MetaData()


def key(name, length=64, **kw):
    return Column(name, String(length, collation="utf8mb4_0900_bin"), **kw)


def text(name, **kw):
    return Column(name, LONGTEXT(), **kw)


def table(name, *columns):
    return Table(name, metadata, *columns, mysql_engine="InnoDB", mysql_charset="utf8mb4")


users = table("users", key("id", primary_key=True), key("username_key", 32, unique=True, nullable=False),
    key("email_key", 320, unique=True), text("display_name", nullable=False), text("password_hash", nullable=False),
    key("status", 24, nullable=False), Column("auth_version", Integer, nullable=False),
    Column("created_at", Float(53), nullable=False), Column("updated_at", Float(53), nullable=False))
auth_sessions = table("auth_sessions", key("token_hash", primary_key=True), key("user_id"), key("csrf_hash", nullable=False),
    Column("auth_version", Integer), Column("remember", Integer, nullable=False),
    *[Column(n, Float(53), nullable=n == "revoked_at") for n in ("created_at", "last_seen_at", "expires_at", "revoked_at")],
    ForeignKeyConstraint(["user_id"], ["users.id"]), Index("auth_session_user", "user_id"), Index("auth_session_expiry", "expires_at"))
invites = table("invites", key("token_hash", primary_key=True), key("email_key", 320), Column("expires_at", Float(53), nullable=False),
    key("consumed_by"), Column("consumed_at", Float(53)), ForeignKeyConstraint(["consumed_by"], ["users.id"]))
auth_audit = table("auth_audit", Column("id", BigInteger, primary_key=True, autoincrement=True), key("action", 64, nullable=False),
    key("user_id"), key("result", 32, nullable=False), key("request_id", 128), Column("occurred_at", Float(53), nullable=False),
    Index("audit_user_time", "user_id", "occurred_at"))
auth_limits = table("auth_limits", key("limit_key", 128, primary_key=True), Column("started", Float(53), nullable=False),
    Column("count", Integer, nullable=False), Index("limits_started", "started"))
auth_meta = table("auth_meta", key("meta_key", primary_key=True), text("value", nullable=False))
storage_namespaces = table("storage_namespaces", key("id", 32, primary_key=True), key("kind", 24, nullable=False),
    key("owner_user_id", unique=True), key("root_fingerprint", unique=True, nullable=False), key("status", 24, nullable=False),
    text("root_path", nullable=False), Column("created_at", Float(53), nullable=False),
    ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
    CheckConstraint("(kind='web_user' AND owner_user_id IS NOT NULL) OR (kind='cli' AND owner_user_id IS NULL)", name="namespace_owner_kind"))


def scoped(name, *columns):
    return table(name, key("namespace_id", 32, primary_key=True), *columns,
                 ForeignKeyConstraint(["namespace_id"], ["storage_namespaces.id"]))


namespace_sequences = scoped("namespace_sequences", key("sequence_name", primary_key=True), Column("last_value", BigInteger, nullable=False))
web_sessions = scoped("web_sessions", key("id", primary_key=True), Column("seq", BigInteger, nullable=False),
    text("title", nullable=False), key("created_at", nullable=False), UniqueConstraint("namespace_id", "seq"))
web_runs = scoped("web_runs", key("id", primary_key=True), Column("seq", BigInteger, nullable=False),
    key("session_id", nullable=False), key("idempotency_key", 255, nullable=False), key("fingerprint", nullable=False),
    text("command", nullable=False), key("status", 24, nullable=False), text("result"), text("error"), key("created_at", nullable=False),
    UniqueConstraint("namespace_id", "session_id", "idempotency_key"), UniqueConstraint("namespace_id", "seq"),
    ForeignKeyConstraint(["namespace_id", "session_id"], ["web_sessions.namespace_id", "web_sessions.id"]),
    Index("web_runs_pending", "namespace_id", "status"), Index("web_runs_session", "namespace_id", "session_id", "seq"))
web_events = scoped("web_events", Column("seq", BigInteger, primary_key=True, autoincrement=False), key("source", 255, nullable=False),
    key("session_id", nullable=False), text("body", nullable=False), UniqueConstraint("namespace_id", "source"),
    ForeignKeyConstraint(["namespace_id", "session_id"], ["web_sessions.namespace_id", "web_sessions.id"]),
    Index("web_events_session", "namespace_id", "session_id", "seq"))
web_cursors = scoped("web_cursors", key("session_id", primary_key=True), Column("runtime_seq", BigInteger, nullable=False),
    ForeignKeyConstraint(["namespace_id", "session_id"], ["web_sessions.namespace_id", "web_sessions.id"]))
research_runs = scoped("research_runs", key("id", primary_key=True), text("scope", nullable=False), text("config", nullable=False),
    text("fingerprint", nullable=False), key("status", 24, nullable=False), key("content", 32, nullable=False), key("review", 32, nullable=False),
    key("owner"), Column("pid", BigInteger), key("birth"), Column("generation", Integer, nullable=False, server_default="0"),
    Column("active_used", Float(53), nullable=False, server_default="0"), Column("segment_started", Float(53)),
    key("created", nullable=False), text("report"), text("reason", nullable=False, default=""), Index("research_status", "namespace_id", "status"))


def research_table(name, *cols):
    return scoped(name, *cols, ForeignKeyConstraint(["namespace_id", "run_id"], ["research_runs.namespace_id", "research_runs.id"]))


research_tasks = research_table("research_tasks", key("run_id", primary_key=True), key("task_id", 128, primary_key=True),
    Column("attempt", Integer, primary_key=True), Column("seq", BigInteger, nullable=False), key("role", 64, nullable=False),
    key("kind", 32, nullable=False), key("status", 24, nullable=False), text("artifact"), key("hash"), text("reason", nullable=False, default=""),
    UniqueConstraint("namespace_id", "seq"), Index("research_task_status", "namespace_id", "run_id", "status"))
research_reservations = research_table("research_reservations", key("id", primary_key=True), key("run_id", nullable=False),
    key("task_id", 128, nullable=False), key("kind", 24, nullable=False), key("state", 24, nullable=False), text("usage"), key("created", nullable=False),
    Index("reservation_kind", "namespace_id", "run_id", "kind"), Index("reservation_state", "namespace_id", "run_id", "state"))
research_grants = research_table("research_grants", key("run_id", primary_key=True), key("task_id", 128, primary_key=True),
    key("result_id", 128, primary_key=True), key("tool", 128, nullable=False))
research_events = research_table("research_events", Column("seq", BigInteger, primary_key=True, autoincrement=False),
    key("run_id", nullable=False), key("created", nullable=False), key("task_id", 128), key("kind", 64, nullable=False), text("body", nullable=False),
    Index("research_event_run", "namespace_id", "run_id", "seq"))
research_stage_states = research_table("research_stage_states", key("run_id", primary_key=True), key("task_id", 128, primary_key=True),
    Column("seq", BigInteger, nullable=False), key("role", 64, nullable=False), key("status", 24, nullable=False), text("reason", nullable=False),
    UniqueConstraint("namespace_id", "seq"))
research_execution_selections = scoped("research_execution_selections", key("id", primary_key=True), key("scope_hash", nullable=False),
    key("source", 64, nullable=False), key("created", nullable=False), Column("consumed", Integer, nullable=False, server_default="0"), key("run_id"),
    ForeignKeyConstraint(["namespace_id", "run_id"], ["research_runs.namespace_id", "research_runs.id"]))
memory_vectors = scoped("memory_vectors", key("memory_user_id", 128, primary_key=True), key("memory_id", 128, primary_key=True),
    key("fingerprint_hash", primary_key=True), text("fingerprint", nullable=False), key("text_hash", nullable=False),
    Column("dimensions", Integer, nullable=False), text("vector", nullable=False))
storage_import_batches = table("storage_import_batches", key("id", 32, primary_key=True), key("source_digest", unique=True, nullable=False),
    key("status", 24, nullable=False), text("manifest", nullable=False), Column("created_at", Float(53), nullable=False))
TABLES = metadata.tables
