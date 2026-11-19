"""s10/s13 persistent dependency graph, assignment leases and worktree cwd."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
from uuid import uuid4

from ..errors import AgentError


class TaskBoard:
    def __init__(self, store, session, workspace):
        self.store, self.session = store, session
        self.workspace = Path(workspace).resolve()
        self.active_resource = lambda task_id: False

    def _read(self, records, task_id):
        task = records.get(task_id)
        if task is None:
            raise AgentError("TASK_NOT_FOUND", "任务不存在")
        return task

    def _write(self, records, task):
        records[task["id"]] = task

    def create(self, subject, description=""):
        if not subject.strip():
            raise ValueError("任务标题不能为空")
        task = dict(id="task_" + uuid4().hex[:8], subject=subject, description=description,
                    status="pending", owner=None, blockedBy=[], version=0, cwd=str(self.workspace), worktree=None)
        self.store.put("tasks", self.session, task)
        return task

    def get(self, task_id):
        return self._read({task["id"]: task for task in self.all()}, task_id)

    def all(self):
        return self.store.records("tasks", self.session)

    def dependencies(self, task_id, dependencies):
        def update(records):
            task = self._read(records, task_id)
            if task["status"] != "pending" or task["owner"]:
                raise ValueError("只能修改待认领任务的依赖")
            for dep in dependencies:
                pending, seen = [dep], set()
                while pending:
                    item = pending.pop()
                    if item == task_id:
                        raise ValueError("任务依赖成环")
                    if item not in seen:
                        seen.add(item)
                        pending.extend(self._read(records, item)["blockedBy"])
            task["blockedBy"] = sorted(set(task["blockedBy"] + dependencies))
            task["version"] += 1
            self._write(records, task)
            return task
        return self.store.update_records("tasks", self.session, update)

    def claim(self, task_id, owner):
        def update(records):
            task = self._read(records, task_id)
            if any(r["owner"] == owner and r["status"] == "in_progress" for r in records.values()):
                raise ValueError("同一执行者不能同时认领两个任务")
            if task["status"] != "pending" or task["owner"] or any(
                    self._read(records, d)["status"] != "completed" for d in task["blockedBy"]):
                raise ValueError("任务不是可认领状态")
            self.verify_cwd(task)
            task.update(status="in_progress", owner=owner, version=task["version"] + 1)
            self._write(records, task)
            return task
        return self.store.update_records("tasks", self.session, update)

    def finish(self, task_id, owner, release=False):
        def update(records):
            task = self._read(records, task_id)
            if task["owner"] != owner or task["status"] != "in_progress":
                raise ValueError("只有任务owner可完成或释放任务")
            task.update(status="pending" if release else "completed", owner=None if release else owner,
                        version=task["version"] + 1)
            self._write(records, task)
            return task
        return self.store.update_records("tasks", self.session, update)

    def recover(self, owner_prefix):
        # A process restart cannot restore worker threads; release interrupted leases.
        for task in self.all():
            if task["status"] == "in_progress" and str(task["owner"]).startswith(owner_prefix):
                self.finish(task["id"], task["owner"], release=True)

    def git(self, *args):
        r = subprocess.run(["git", *args], cwd=self.workspace, capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise AgentError("GIT_ERROR", r.stderr[:1000])
        return r.stdout

    def verify_cwd(self, task):
        cwd = Path(task["cwd"]).resolve()
        if not cwd.is_relative_to(self.workspace) or not cwd.is_dir():
            raise ValueError("任务工作目录失效")
        if task.get("worktree"):
            entries = self.git("worktree", "list", "--porcelain")
            block = next((b for b in entries.split("\n\n") if "worktree " + str(cwd) in b.splitlines()), "")
            if "branch refs/heads/wt/" + task["worktree"] not in block.splitlines():
                raise ValueError("worktree registry/分支不匹配")
        return cwd

    def create_worktree(self, name, task_id):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name) or ".." in name:
            raise ValueError("无效worktree名称")
        with self.store.lease("worktree_" + task_id):
            task = self.get(task_id)
            if task["status"] != "pending" or task["owner"]:
                raise ValueError("只能绑定未认领任务")
            self.git("rev-parse", "--show-toplevel")
            root = self.workspace / ".worktrees"
            root.mkdir(exist_ok=True)
            target = (root / name).resolve()
            if not target.is_relative_to(root.resolve()) or target.exists():
                raise ValueError("worktree路径已存在或越界")
            try:
                self.git("worktree", "add", "-b", "wt/" + name, str(target))
            except AgentError:
                self.store.emit(self.session, "worktree_partial_failure", {"path": str(target), "name": name})
                raise
            def update(records):
                latest = self._read(records, task_id)
                if latest["version"] != task["version"] or latest["owner"]:
                    raise ValueError("任务已变更，新worktree未绑定并保留待恢复")
                latest.update(cwd=str(target), worktree=name, version=latest["version"] + 1)
                self._write(records, latest)
                return latest
            return self.store.update_records("tasks", self.session, update)

    def remove_worktree(self, task_id, confirmed=False):
        task = self.get(task_id)
        cwd = self.verify_cwd(task)
        if not confirmed or task["status"] == "in_progress" or not task.get("worktree") or self.active_resource(task_id):
            raise ValueError("移除需要显式确认且任务不能正在执行")
        r = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True)
        if r.stdout.strip():
            raise ValueError("worktree有未提交改动，保留目录")
        self.git("worktree", "remove", str(cwd))
