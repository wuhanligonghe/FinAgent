"""Server-side authentication. No credentials are passed to the agent runtime."""
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import hmac
import re
import secrets
from threading import BoundedSemaphore
import time
from uuid import uuid4


class AuthError(Exception):
    def __init__(self, code, message, status=401, retry_after=None):
        self.code, self.message, self.status, self.retry_after = code, message, status, retry_after
        super().__init__(code)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@lru_cache(maxsize=1)
def password_tools():
    from argon2 import PasswordHasher
    hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
    return hasher, hasher.hash(secrets.token_urlsafe(32)), BoundedSemaphore(2)


def password_hash(password):
    if not isinstance(password, str) or not 15 <= len(password) <= 128:
        raise AuthError("PASSWORD_POLICY", "密码须为15至128个字符", 422)
    hasher, _, gate = password_tools()
    if not gate.acquire(blocking=False):
        raise AuthError("RATE_LIMITED", "请求较多，请稍后重试", 429, 1)
    try:
        return hasher.hash(password)
    finally:
        gate.release()


def account_keys(username, email=None):
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{3,32}", username):
        raise AuthError("INVALID_ACCOUNT", "用户名须为3至32位字母、数字、下划线或连字符", 422)
    if email:
        from email_validator import validate_email, EmailNotValidError
        try:
            email = validate_email(email.strip(), check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            raise AuthError("INVALID_ACCOUNT", "邮箱格式无效", 422) from None
    return username, email or None


class AuthStore:
    def __init__(self, root, settings, *, database=None):
        from ..storage.database import get_database
        from ..errors import AgentError
        from ..harness.state import atomic_json
        if root.is_symlink():
            raise ValueError("认证目录不能是符号链接")
        self.root, self.settings = root.resolve(), settings
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.database = database or get_database()
        marker = self.root / ".mysql_auth.json"
        if marker.is_symlink() or (self.root / "auth.sqlite3").is_symlink():
            raise ValueError("认证数据库标记不能是符号链接")
        if (self.root / "auth.sqlite3").exists() and not marker.exists():
            raise AgentError("DATABASE_MIGRATION_REQUIRED", "发现旧认证数据库，请先迁入 MySQL")
        fingerprint = hashlib.sha256(str(self.root).encode()).hexdigest()
        def initialize(db):
            prior = db.one("auth_meta", meta_key="deployment_root")
            csrf = db.one("auth_meta", meta_key="csrf_key")
            if marker.exists() and (not prior or not csrf):
                raise AgentError("DATABASE_DATA_MISSING", "原认证元数据缺失，请恢复 MySQL 备份；不会重建空账号库")
            db.upsert("auth_meta", {"meta_key": "deployment_root", "value": fingerprint}, [])
            if db.one("auth_meta", meta_key="deployment_root")["value"] != fingerprint:
                raise AgentError("DATABASE_NAMESPACE_CHANGED", "认证库已绑定其他目录，请显式迁移")
            db.upsert("auth_meta", {"meta_key": "csrf_key", "value": secrets.token_hex(32)}, [])
            return bytes.fromhex(db.one("auth_meta", meta_key="csrf_key")["value"])
        self.csrf_key = self.database.atomic(initialize)
        atomic_json(marker, {"schema_version": 1, "root_fingerprint": fingerprint})

    def db(self):
        return self.database.transaction()

    @staticmethod
    def public(user):
        return {"id": user["id"], "username": user["username_key"], "display_name": user["display_name"]}

    def user(self, user_id):
        with self.db() as db:
            return db.one("users", id=user_id)

    def users(self):
        from ..storage.schema import users
        with self.db() as db:
            return [{k: r[k] for k in ("id", "username_key", "display_name", "status")} for r in db.rows("users", order=[users.c.created_at])]

    def audit(self, action, user_id, result, request_id=None):
        with self.db() as db:
            db.add("auth_audit", dict(action=action, user_id=user_id, result=result, request_id=request_id, occurred_at=time.time()))

    def limit(self, key, maximum, window):
        from ..storage.schema import auth_limits
        now = time.time()
        def operation(db):
            db.upsert("auth_limits", dict(limit_key=key, started=now, count=0), [])
            row = db.one("auth_limits", limit_key=key, lock=True)
            if now - row["started"] < window and row["count"] >= maximum:
                raise AuthError("RATE_LIMITED", "请求较多，请稍后重试", 429, max(1, int(window - now + row["started"])))
            fresh = now - row["started"] >= window
            db.change("auth_limits", dict(started=now if fresh else row["started"], count=1 if fresh else row["count"] + 1), limit_key=key)
            db.remove("auth_limits", where=[auth_limits.c.started < now - 86400])
        self.database.atomic(operation)

    def create_user(self, username, password, email=None, display_name=None, *, invite=None, require_invite=False):
        from sqlalchemy.exc import IntegrityError
        username, email = account_keys(username, email)
        hashed = password_hash(password)
        user_id, now = "usr_" + uuid4().hex, time.time()
        def operation(db):
            if require_invite:
                row = db.one("invites", token_hash=digest(invite or ""), lock=True)
                if not row or row["consumed_by"] or row["expires_at"] <= now or (row["email_key"] and row["email_key"] != email):
                    raise AuthError("REGISTRATION_DENIED", "注册信息或邀请码无效", 422)
            db.add("users", dict(id=user_id, username_key=username, email_key=email, display_name=(display_name or username)[:80],
                password_hash=hashed, status="active", auth_version=1, created_at=now, updated_at=now))
            if require_invite:
                db.change("invites", dict(consumed_by=user_id, consumed_at=now), token_hash=digest(invite))
        try:
            self.database.atomic(operation)
        except IntegrityError:
            raise AuthError("REGISTRATION_DENIED", "注册信息或邀请码无效", 422) from None
        self.audit("create_user", user_id, "ok")
        return self.public(self.user(user_id))

    def create_invite(self, email=None, hours=48):
        if email:
            _, email = account_keys("invite", email)
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.add("invites", dict(token_hash=digest(token), email_key=email, expires_at=time.time() + hours * 3600))
        self.audit("create_invite", None, "ok")
        return token

    def authenticate(self, identifier, password, ip, request_id=None):
        from ..storage.schema import users
        key = digest(identifier.strip().lower())
        self.limit("login-ip:" + digest(ip), 30, 60)
        self.limit("login-account:" + key, 5, 900)
        with self.db() as db:
            identity = identifier.strip().lower()
            row = db.one("users", where=[(users.c.username_key == identity) | (users.c.email_key == identity)])
        hasher, dummy, gate = password_tools()
        if not gate.acquire(blocking=False):
            raise AuthError("RATE_LIMITED", "请求较多，请稍后重试", 429, 1)
        try:
            from argon2.exceptions import VerificationError, InvalidHashError
            try:
                valid = hasher.verify(row["password_hash"] if row else dummy, password)
            except (VerificationError, InvalidHashError):
                valid = False
            if not valid or not row or row["status"] != "active":
                self.audit("login", row["id"] if row else None, "denied", request_id)
                raise AuthError("INVALID_CREDENTIALS", "账号或密码错误")
            rehashed = hasher.hash(password) if hasher.check_needs_rehash(row["password_hash"]) else None
            with self.db() as db:
                db.remove("auth_limits", limit_key="login-account:" + key)
                if rehashed:
                    db.change("users", {"password_hash": rehashed}, id=row["id"], auth_version=row["auth_version"])
            return row
        finally:
            gate.release()

    def csrf(self, token):
        return hmac.new(self.csrf_key, token.encode(), hashlib.sha256).hexdigest()

    def new_session(self, user=None, remember=False, old_token=None):
        from ..storage.schema import auth_sessions
        token, now = secrets.token_urlsafe(32), time.time()
        lifetime = (self.settings.remember_seconds if remember else self.settings.session_seconds) if user else 600
        def operation(db):
            if user:
                current = db.one("users", id=user["id"], lock=True)
                if not current or current["status"] != "active" or current["auth_version"] != user["auth_version"]:
                    raise AuthError("INVALID_CREDENTIALS", "账号或密码错误")
            db.add("auth_sessions", dict(token_hash=digest(token), user_id=user["id"] if user else None, csrf_hash=digest(self.csrf(token)),
                auth_version=user["auth_version"] if user else None, remember=int(remember), created_at=now, last_seen_at=now, expires_at=now + lifetime))
            if old_token:
                db.change("auth_sessions", {"revoked_at": now}, token_hash=digest(old_token))
            db.remove("auth_sessions", where=[(auth_sessions.c.expires_at < now) | (auth_sessions.c.revoked_at < now - 86400)])
        self.database.atomic(operation)
        return token, now + lifetime

    def resolve(self, token, *, touch=False):
        if not token or len(token) > 128:
            return None
        now = time.time()
        with self.db() as db:
            row = db.one("auth_sessions", token_hash=digest(token))
            if not row or row["revoked_at"] or row["expires_at"] <= now:
                return None
            user = db.one("users", id=row["user_id"]) if row["user_id"] else None
            row.update(status=user["status"] if user else None, current_version=user["auth_version"] if user else None,
                username_key=user["username_key"] if user else None, display_name=user["display_name"] if user else None)
            if row["user_id"]:
                idle = self.settings.remember_idle_seconds if row["remember"] else self.settings.idle_seconds
                if row["status"] != "active" or row["auth_version"] != row["current_version"] or now - row["last_seen_at"] >= idle:
                    return None
            if touch and now - row["last_seen_at"] >= 60:
                db.change("auth_sessions", {"last_seen_at": now}, token_hash=row["token_hash"])
        return row

    def check_csrf(self, session, value):
        if not session or not value or not hmac.compare_digest(session["csrf_hash"], digest(value)):
            raise AuthError("CSRF_FAILED", "页面验证已失效，请刷新后重试", 403)

    def revoke(self, token):
        with self.db() as db:
            db.change("auth_sessions", {"revoked_at": time.time()}, token_hash=digest(token or ""))

    def update_user(self, user_id, *, password=None, status=None):
        hashed = password_hash(password) if password is not None else None
        if status is not None and status not in {"active", "disabled"}:
            raise ValueError("无效账号状态")
        def operation(db):
            row = db.one("users", id=user_id, lock=True)
            if not row:
                raise AuthError("USER_NOT_FOUND", "账号不存在", 404)
            values = dict(auth_version=row["auth_version"] + 1, updated_at=time.time())
            if hashed is not None:
                values["password_hash"] = hashed
            if status is not None:
                values["status"] = status
            db.change("users", values, id=user_id)
            db.change("auth_sessions", {"revoked_at": time.time()}, user_id=user_id)
        self.database.atomic(operation)
        self.audit("update_user", user_id, "ok")
