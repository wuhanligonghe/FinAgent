"""Identity-bound stores and bounded, application-wide execution resources."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import re
import json
from threading import Event, RLock, Thread
from time import monotonic

from ..errors import AgentError
from ..harness.state import atomic_json
from .auth_store import AuthError
from .reports import ReportAdapter
from .session_manager import SessionManager
from .store import WebStore


def user_settings(settings, user_id):
    if not re.fullmatch(r'usr_[a-f0-9]{32}', user_id):
        raise AuthError('UNAUTHENTICATED', '请先登录')
    base = settings.data_dir.resolve()
    directory = base / 'users' / user_id
    if (base / 'users').is_symlink() or directory.is_symlink() or not directory.resolve().is_relative_to(base):
        raise ValueError('用户目录越界')
    clone = settings.model_copy(deep=True)
    clone.data_dir = directory
    clone.memory.user_id = user_id
    return clone


@dataclass
class UserContext:
    settings: object
    store: WebStore
    manager: SessionManager
    reports: ReportAdapter
    last_used: float
    closing: bool = False


class UserRuntimeRegistry:
    def __init__(self, settings, auth, host_factory=None):
        self.settings, self.auth, self.host_factory = settings, auth, host_factory
        self.contexts, self.lock, self.resources = {}, RLock(), RLock()
        self.executor = ThreadPoolExecutor(max_workers=settings.auth.workers, thread_name_prefix='finsight')
        self.stopped = Event()
        self.maintenance = None

    def start(self):
        # Recover all persisted users, including those who never log in again.
        for user in self.auth.users():
            clone = user_settings(self.settings, user['id'])
            from ..storage.database import get_database
            from hashlib import sha256
            with self.auth.database.transaction() as db:
                registered = db.one("storage_namespaces", root_fingerprint=sha256(str(clone.data_dir.resolve()).encode()).hexdigest())
            if not registered:
                continue
            store = WebStore(clone.data_dir / 'web', database=self.auth.database)
            manager = SessionManager(clone, store, **({'host_factory': self.host_factory} if self.host_factory else {}))
            manager.start()
            manager.close()
        self.maintenance = Thread(target=self.clean, name='finsight-users', daemon=True)
        self.maintenance.start()

    def for_user(self, user_id):
        user = self.auth.user(user_id)
        if not user or user['status'] != 'active':
            raise AuthError('UNAUTHENTICATED', '请重新登录')
        with self.lock:
            if user_id not in self.contexts:
                if len(self.contexts) >= self.settings.auth.runtime_limit:
                    raise AuthError('SESSION_LIMIT', '当前活跃用户较多，请稍后重试', 503, 2)
                clone = user_settings(self.settings, user_id)
                clone.data_dir.mkdir(parents=True, exist_ok=True)
                clone.data_dir.parent.chmod(0o700)
                clone.data_dir.chmod(0o700)
                owner_file = clone.data_dir / '.owner.json'
                if owner_file.is_symlink() or (owner_file.exists() and json.loads(owner_file.read_text()).get('user_id') != user_id):
                    raise AuthError('NOT_FOUND', '工作区不存在', 404)
                atomic_json(owner_file, {'user_id':user_id})
                store = WebStore(clone.data_dir / 'web', database=self.auth.database)
                options = {'host_factory': self.host_factory} if self.host_factory else {}
                manager = SessionManager(clone, store, executor=self.executor, resources=self, **options)
                manager.start()
                self.contexts[user_id] = UserContext(clone, store, manager, ReportAdapter(clone, manager), monotonic())
            context = self.contexts[user_id]
            if context.closing:
                raise AuthError('RUNTIME_CLOSING', '工作区正在释放，请稍后重试', 503, 2)
            context.last_used = monotonic()
            return context

    def enqueue(self, manager, session_id, key, command):
        with self.resources:
            prior = manager.store.has_run(session_id, key)
            if not prior and sum(c.store.pending_count() for c in list(self.contexts.values())) >= self.settings.auth.queue_global:
                raise AgentError('QUEUE_FULL', '执行队列已满，请稍后重试')
            return manager.store.create_run(session_id, key, command, self.settings.auth.queue_per_user)

    def launch_research(self, manager, launch, *args, **kwargs):
        with self.resources:
            jobs = [job for c in list(self.contexts.values()) for job in c.manager.research_jobs() if job['status'] == 'running']
            own = [j for j in manager.research_jobs() if j['status'] == 'running']
            if len(jobs) >= self.settings.auth.research_global or len(own) >= self.settings.auth.research_per_user:
                raise AgentError('RESEARCH_LIMIT', '研究并发额度已满，请等待完成后重试')
            return launch(*args, **kwargs)

    def clean(self):
        while not self.stopped.wait(2):
            closing = []
            with self.lock:
                for key, context in list(self.contexts.items()):
                    user = self.auth.user(key)
                    inactive = not user or user['status'] != 'active'
                    idle = monotonic() - context.last_used >= self.settings.auth.runtime_idle_seconds
                    if inactive or (idle and not context.manager.busy()):
                        context.closing = True
                        closing.append((key, context))
            for key, context in closing:
                context.manager.close()
                with self.lock:
                    self.contexts.pop(key, None)

    def close(self):
        self.stopped.set()
        if self.maintenance:
            self.maintenance.join(timeout=3)
        for context in list(self.contexts.values()):
            context.manager.close()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.contexts.clear()
