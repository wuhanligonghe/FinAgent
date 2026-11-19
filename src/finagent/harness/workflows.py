"""s16 trustworthy registered workflows, semantic journals and exclusive resume."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from hashlib import sha256
import inspect
import json
from pathlib import Path
from threading import RLock
from uuid import uuid4

import jsonschema

from ..errors import AgentError
from .state import atomic_json, encode, identity
from .tools import validate, check_schema


class WorkflowContext:
    def __init__(self, runtime, run_id, meta, args, ledger, cache, journal, depth=0, executor=None):
        self.runtime, self.run_id, self.meta, self.args = runtime, run_id, meta, args
        self.ledger, self.cache, self.journal, self.depth = ledger, cache, journal, depth
        self.lock, self.calls = RLock(), 0
        self.executor = executor
        self.semaphore = asyncio.Semaphore(runtime.concurrency)

    def event(self, kind, **data):
        self.runtime.event(kind, work_id=self.run_id, **data)

    def phase(self, title):
        self.event("task_progress", phase=title)

    def log(self, message):
        self.event("task_progress", message=message)

    async def agent(self, prompt, schema=None, label="agent", depends_on=None):
        key = sha256(encode([self.meta["version"], self.args, prompt, schema, label, depends_on]).encode()).hexdigest()
        if key in self.cache:
            self.event("task_progress", cached=True, label=label)
            return self.cache[key]
        with self.lock:
            self.calls += 1
            if self.calls > self.runtime.agent_cap:
                raise AgentError("WORKFLOW_BUDGET", "工作流子调用数耗尽")
        async with self.semaphore:
            self.ledger.remaining()
            self.event("task_progress", label=label)
            loop = asyncio.get_running_loop()
            value = await loop.run_in_executor(self.executor, partial(self.runtime.runner, prompt, schema, self.ledger))
            if schema:
                try:
                    validate(value, schema)
                except jsonschema.ValidationError as exc:
                    corrected = prompt + "\n前次输出：" + encode(value) + "\n校验错误：" + exc.message
                    value = await loop.run_in_executor(self.executor, partial(self.runtime.runner, corrected, schema, self.ledger))
                    validate(value, schema)
            with self.lock:
                with self.journal.open("a", encoding="utf-8") as f:
                    f.write(encode({"key": key, "value": value}) + "\n")
                    f.flush()
                self.cache[key] = value
            return value

    async def parallel(self, thunks):
        return await asyncio.gather(*(fn() for fn in thunks))

    async def pipeline(self, items, *stages):
        async def run(item, index):
            value = item
            for stage in stages:
                value = await stage(value, item, index)
            return value
        return await asyncio.gather(*(run(item, i) for i, item in enumerate(items)))

    async def workflow(self, name, args):
        if self.depth >= 1:
            raise ValueError("工作流仅支持一层嵌套")
        meta, fn = self.runtime.registry[name]
        child = WorkflowContext(self.runtime, self.run_id, meta, args, self.ledger,
                                self.cache, self.journal, depth=1, executor=self.executor)
        return await fn(child, args)


class WorkflowRuntime:
    def __init__(self, store, session, runner, event=None, model_identity="", concurrency=4, agent_cap=100):
        self.store, self.session, self.runner = store, session, runner
        self.event = event or (lambda kind, **data: store.emit(session, kind, data))
        self.model_identity, self.concurrency, self.agent_cap = model_identity, concurrency, agent_cap
        self.registry = {}
        self.root = store.root / "workflows" / session
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, name, description, fn, schema=None, version=None):
        identity(name)
        if not description or name in self.registry:
            raise ValueError("工作流名称重复或描述为空")
        if not inspect.iscoroutinefunction(fn):
            raise ValueError("工作流必须为宿主注册的async函数")
        if schema:
            check_schema(schema)
        source = inspect.getsource(fn) if version is None else version
        version = sha256((source + self.model_identity).encode()).hexdigest()
        self.registry[name] = (dict(name=name, description=description, schema=schema, version=version), fn)

    def run(self, name, args, ledger, resume_from_run_id=None):
        if name not in self.registry:
            raise ValueError("未注册的工作流")
        meta, fn = self.registry[name]
        if meta["schema"]:
            validate(args, meta["schema"])
        run_id = identity(resume_from_run_id or "wf_" + uuid4().hex)
        with self.store.lease(run_id):
            snapshot, journal = self.root / (run_id + ".json"), self.root / (run_id + ".jsonl")
            if resume_from_run_id:
                saved = json.loads(snapshot.read_text())
                if saved["name"] != name or saved["session"] != self.session:
                    raise ValueError("工作流恢复范围不匹配")
            cache = {}
            if journal.exists():
                for line in journal.read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    cache[row["key"]] = row["value"]
            record = dict(id=run_id, session=self.session, name=name, args=args, version=meta["version"], status="running")
            atomic_json(snapshot, record)
            self.event("task_started", work_id=run_id, name=name)
            executor = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="workflow")
            ctx = WorkflowContext(self, run_id, meta, args, ledger, cache, journal, executor=executor)
            try:
                async def execute():
                    task = asyncio.create_task(fn(ctx, args))
                    try:
                        while not task.done():
                            # Deadline watchdog also makes completion delivery
                            # independent of platform self-pipe wakeup behavior.
                            ledger.remaining()
                            await asyncio.sleep(.05)
                        return await task
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                value = asyncio.run(execute())
                record.update(status="completed", result=value)
                atomic_json(self.root / (run_id + ".output.json"), value)
            except BaseException as exc:
                record.update(status="failed", error=type(exc).__name__)
                raise
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
                atomic_json(snapshot, record)
                self.event("task_notification", **record)
            return record
