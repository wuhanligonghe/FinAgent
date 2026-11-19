"""Bounded MySQL pools and typed, namespace-bound Core operations."""
from contextlib import contextmanager
import atexit
import random
import time

from sqlalchemy import create_engine, select, func, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError, IntegrityError
from sqlalchemy.dialects.mysql import insert

from ..errors import AgentError
from .schema import TABLES

REVISION = "20261115_mysql"


class Session:
    def __init__(self, connection, namespace_id=None):
        self.connection, self.namespace_id = connection, namespace_id

    def conditions(self, table, where=(), **eq):
        result = list(where)
        if "namespace_id" in table.c:
            if not self.namespace_id:
                raise ValueError("A trusted storage namespace is required")
            result.append(table.c.namespace_id == self.namespace_id)
        result.extend(table.c[k] == v for k, v in eq.items())
        return result

    def rows(self, name, *, where=(), order=(), limit=None, lock=False, **eq):
        t = TABLES[name]
        q = select(t).where(*self.conditions(t, where, **eq)).order_by(*order)
        if limit is not None:
            q = q.limit(limit)
        if lock:
            q = q.with_for_update()
        values = [dict(r) for r in self.connection.execute(q).mappings()]
        for value in values:
            value.pop("namespace_id", None)
        return values

    def one(self, name, **options):
        values = self.rows(name, limit=1, **options)
        return values[0] if values else None

    def count(self, name, *, where=(), **eq):
        t = TABLES[name]
        return self.connection.scalar(select(func.count()).select_from(t).where(*self.conditions(t, where, **eq)))

    def values(self, name, values):
        result = dict(values)
        if "namespace_id" in TABLES[name].c:
            if not self.namespace_id or ("namespace_id" in result and result["namespace_id"] != self.namespace_id):
                raise ValueError("Storage namespace mismatch")
            result["namespace_id"] = self.namespace_id
        return result

    def add(self, name, values):
        return self.connection.execute(TABLES[name].insert().values(**self.values(name, values)))

    def change(self, name, values, *, where=(), **eq):
        if "namespace_id" in values:
            raise ValueError("Cannot change storage namespace")
        t = TABLES[name]
        return self.connection.execute(t.update().where(*self.conditions(t, where, **eq)).values(**values)).rowcount

    def remove(self, name, *, where=(), **eq):
        t = TABLES[name]
        return self.connection.execute(t.delete().where(*self.conditions(t, where, **eq))).rowcount

    def upsert(self, name, values, columns):
        q = insert(TABLES[name]).values(**self.values(name, values))
        changes = {k: q.inserted[k] for k in columns}
        if not changes:
            pk = next(c.name for c in TABLES[name].primary_key if c.name != "namespace_id")
            changes = {pk: TABLES[name].c[pk]}
        return self.connection.execute(q.on_duplicate_key_update(**changes))

    def next_seq(self, name):
        row = self.one("namespace_sequences", sequence_name=name, lock=True)
        if row is None:
            self.add("namespace_sequences", {"sequence_name": name, "last_value": 0})
            row = {"last_value": 0}
        value = row["last_value"] + 1
        self.change("namespace_sequences", {"last_value": value}, sequence_name=name)
        return value


class Database:
    def __init__(self, settings):
        self.settings = settings
        raw = settings.url.get_secret_value()
        try:
            url = make_url(raw).update_query_dict({"charset": "utf8mb4"})
            if url.drivername != "mysql+pymysql" or not url.database:
                raise ValueError()
        except (ValueError, SQLAlchemyError):
            raise AgentError("DATABASE_CONFIG", "请配置有效的 FINAGENT_MYSQL_URL") from None
        args = dict(connect_timeout=settings.connect_timeout, read_timeout=settings.read_timeout, write_timeout=settings.write_timeout)
        if settings.ssl_ca:
            args["ssl"] = {"ca": str(settings.ssl_ca), "check_hostname": True}
        self.engine = create_engine(url, pool_size=settings.pool_size, max_overflow=settings.max_overflow,
            pool_timeout=settings.pool_timeout, pool_recycle=settings.pool_recycle, pool_pre_ping=True,
            isolation_level="READ COMMITTED", hide_parameters=True, connect_args=args)
        self.checked = False

    def check(self):
        if self.checked:
            return
        try:
            with self.engine.connect() as c:
                revision = c.scalar(text("SELECT version_num FROM alembic_version"))
                incomplete = c.scalar(select(func.count()).select_from(TABLES["storage_import_batches"]).where(
                    TABLES["storage_import_batches"].c.status != "verified"))
        except SQLAlchemyError as exc:
            code = exc.orig.args[0] if isinstance(exc, DBAPIError) and getattr(exc.orig, "args", ()) else None
            if code in (1146, 1054):
                raise AgentError("DATABASE_SCHEMA_MISMATCH", "MySQL 结构尚未升级，请运行数据库升级") from None
            raise AgentError("DATABASE_UNAVAILABLE", "MySQL 无法连接或检查失败，请检查数据库服务", True) from None
        if revision != REVISION:
            raise AgentError("DATABASE_SCHEMA_MISMATCH", "MySQL 结构版本不匹配，请先升级")
        if incomplete:
            raise AgentError("DATABASE_IMPORT_INCOMPLETE", "MySQL 导入尚未完成，请继续迁移并核对")
        self.checked = True

    @contextmanager
    def transaction(self, scope=None, *, lock=False, unchecked=False):
        if not unchecked:
            self.check()
        try:
            with self.engine.begin() as c:
                s = Session(c, getattr(scope, "id", scope))
                if scope:
                    row = c.execute(select(TABLES["storage_namespaces"]).where(TABLES["storage_namespaces"].c.id == s.namespace_id)
                        .with_for_update() if lock else select(TABLES["storage_namespaces"]).where(TABLES["storage_namespaces"].c.id == s.namespace_id)).mappings().first()
                    if not row or (not unchecked and row["status"] != "ready"):
                        raise AgentError("DATABASE_IMPORT_INCOMPLETE", "工作区尚未就绪")
                yield s
        except IntegrityError:
            raise
        except SQLAlchemyError:
            raise AgentError("DATABASE_UNAVAILABLE", "MySQL 操作失败；结果可能未知，请检查数据库状态", True) from None

    def atomic(self, operation, scope=None, *, lock=False):
        """Retry only transactions known to be rolled back, never external work."""
        self.check()
        for attempt in range(3):
            try:
                with self.engine.begin() as c:
                    s = Session(c, getattr(scope, "id", scope))
                    if scope:
                        ns = TABLES["storage_namespaces"]
                        row = c.execute(select(ns).where(ns.c.id == s.namespace_id).with_for_update()).mappings().first() if lock else c.execute(select(ns).where(ns.c.id == s.namespace_id)).mappings().first()
                        if not row or row["status"] != "ready":
                            raise AgentError("DATABASE_IMPORT_INCOMPLETE", "工作区尚未就绪")
                    result = operation(s)
                return result
            except IntegrityError:
                raise
            except DBAPIError as exc:
                code = exc.orig.args[0] if getattr(exc.orig, "args", ()) else None
                if code in (1205, 1213) and attempt < 2:
                    time.sleep(random.uniform(.01, .05) * (attempt + 1))
                    continue
                raise AgentError("DATABASE_UNAVAILABLE", "MySQL 事务未完成；请按幂等标识核对结果", True) from None
            except SQLAlchemyError:
                raise AgentError("DATABASE_UNAVAILABLE", "MySQL 连接不可用", True) from None

    def close(self):
        self.engine.dispose()
        self.checked = False


_databases = {}


def get_database(settings=None):
    if settings is None:
        from ..config import load_settings
        settings = load_settings().database
    cache_key = (settings.url.get_secret_value(), settings.model_dump_json())
    if cache_key not in _databases:
        _databases[cache_key] = Database(settings)
    return _databases[cache_key]


def close_databases():
    for db in _databases.values():
        db.close()
    _databases.clear()


atexit.register(close_databases)
