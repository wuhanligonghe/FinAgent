"""s11-s13 background lifecycle, durable cron delivery and persistent teammates."""
from __future__ import annotations

from datetime import datetime
import json
import queue
from threading import Event, RLock, Thread
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..errors import AgentError
from .inherited import cron_matches, validate_cron


class Background:
    def __init__(self, store, session):
        self.store, self.session = store, session
        self.handles, self.lock = {}, RLock()
        self.closed = Event()

    def launch(self, label, fn, cancel=None, kind="background", resources=None):
        if self.closed.is_set():
            raise ValueError("宿主已关闭")
        work_id = "work_" + uuid4().hex
        cancel = cancel or Event()
        record = dict(id=work_id, kind=kind, label=label, status="running", resources=resources or {})
        if getattr(self, 'owner_user_id', None):
            record['resources'] = {**record['resources'], 'owner_user_id': self.owner_user_id}
        self.store.put("work", self.session, record)
        def run():
            try:
                value = fn(cancel, work_id)
                status = "cancelled" if cancel.is_set() else "completed"
                if isinstance(value, dict) and value.get("status") in {"failed", "cancelled"}:
                    status = value["status"]
                record.update(status=status, result=value)
            except BaseException as exc:
                record.update(status="cancelled" if cancel.is_set() else "failed", error=type(exc).__name__)
            finally:
                self.store.put("work", self.session, record)
                self.store.emit(self.session, "task_notification", record, event_id=work_id, notify=True)
        thread = Thread(target=run, name=work_id, daemon=True)
        with self.lock:
            self.handles[work_id] = (thread, cancel)
        thread.start()
        return {"work_id": work_id, "status": "running"}

    def active(self):
        return any(thread.is_alive() for thread, _ in self.handles.values())

    def cancel(self, work_id=None):
        for key, (_, cancel) in list(self.handles.items()):
            if work_id is None or key == work_id:
                cancel.set()

    def recover(self):
        for record in self.store.records("work", self.session):
            if record["status"] == "running":
                record.update(status="interrupted", reason="原宿主线程无法跨进程恢复")
                self.store.put("work", self.session, record)
            if record["status"] in {"interrupted", "completed", "failed", "cancelled"}:
                self.store.emit(self.session, "task_notification", record, event_id=record["id"], notify=True)

    def close(self):
        self.closed.set()
        self.cancel()
        for thread, _ in list(self.handles.values()):
            thread.join(timeout=2)


class Scheduler:
    def __init__(self, store, session, timezone="Asia/Hong_Kong"):
        self.store, self.session, self.timezone = store, session, timezone
        ZoneInfo(timezone)
        self.lock = RLock()
        self.stop_event = Event()
        self.thread = None

    def schedule(self, cron, prompt, recurring=True, durable=True):
        error = validate_cron(cron)
        if error:
            raise ValueError(error)
        job = dict(id="cron_" + uuid4().hex, cron=cron, prompt=prompt, recurring=recurring,
                   durable=durable, timezone=self.timezone, status="active", last_fired=None)
        self.store.put("jobs", self.session, job)
        return job

    def jobs(self):
        return self.store.records("jobs", self.session)

    def cancel(self, job_id):
        with self.lock:
            job = next((j for j in self.jobs() if j["id"] == job_id), None)
            if job is None:
                raise ValueError("定时任务不存在")
            job["status"] = "cancelled"
            self.store.put("jobs", self.session, job)
            for event in self.store.pending(self.session):
                if event["payload"].get("job_id") == job_id:
                    self.store.ack([event["event_id"]])
            return job

    def tick(self, stamp=None):
        stamp = stamp or datetime.now(ZoneInfo(self.timezone))
        with self.lock:
            for job in self.jobs():
                local = stamp.astimezone(ZoneInfo(job["timezone"]))
                minute = local.isoformat(timespec="minutes")
                if job["status"] != "active" or job["last_fired"] == minute or not cron_matches(job["cron"], local):
                    continue
                # Persist before publishing, then replay pending_delivery on restart.
                job.update(last_fired=minute, pending_event=job["id"] + "_" + minute,
                           status="active" if job["recurring"] else "pending_delivery")
                self.store.put("jobs", self.session, job)
                self._deliver(job)

    def _deliver(self, job):
        self.store.emit(self.session, "cron_prompt", {"job_id": job["id"], "prompt": job["prompt"]},
                        event_id=job["pending_event"], notify=True)

    def ack(self, events):
        with self.lock:
            ids = {e["event_id"] for e in events}
            for job in self.jobs():
                if job.get("pending_event") in ids:
                    if not job["recurring"]:
                        job["status"] = "completed"
                    self.store.put("jobs", self.session, job)

    def start(self):
        if self.thread:
            return
        for job in self.jobs():
            if not job["durable"]:
                self.cancel(job["id"])
            elif job["status"] in {"active", "pending_delivery"} and job.get("pending_event"):
                self._deliver(job)
        def run():
            while not self.stop_event.wait(.5):
                self.tick()
        self.thread = Thread(target=run, name="cron_" + self.session, daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)


class Teams:
    def __init__(self, board, background, runner):
        self.board, self.background, self.runner = board, background, runner
        self.members, self.plans, self.lock = {}, {}, RLock()
        self.funding = {}

    def spawn(self, name, role, prompt, task_id=None, require_plan=False, ledger=None):
        from .state import identity
        identity(name)
        with self.lock:
            if name in self.members:
                raise ValueError("队友名称已存在")
            owner = "team_" + name
            assignment = self.board.claim(task_id, owner) if task_id else None
            if task_id and ledger is not None:
                self.funding[task_id]=ledger
            member = dict(name=name, role=role, owner=owner, status="IDLE", inbox=queue.Queue(),
                          assignment=assignment, require_plan=require_plan, approved=None, cancel=Event())
            self.members[name] = member
        def work(cancel, work_id):
            member["cancel"] = cancel
            try:
                while not cancel.is_set():
                    if member["assignment"] is None:
                        with self.lock:
                            for task in self.board.all():
                                try:
                                    member["assignment"] = self.board.claim(task["id"], owner)
                                    break
                                except ValueError:
                                    continue
                        if member["assignment"] is None:
                            try:
                                message = member["inbox"].get(timeout=.25)
                                if message.get("type") == "shutdown":
                                    break
                            except queue.Empty:
                                pass
                            continue
                    assignment = member["assignment"]
                    member["status"] = "WORK"
                    task_ledger=self.funding.get(assignment["id"],ledger)
                    value = self.runner(member, prompt, task_ledger)
                    current = self.board.get(assignment["id"])
                    self.background.store.emit(self.board.session, "teammate_result", {
                        "name": name, "task_id": current["id"], "result": value}, notify=True)
                    pending_plan = any(p["teammate"] == name and p["task_id"] == current["id"]
                                       and p["version"] == current["version"] and p["status"] == "pending"
                                       for p in list(self.plans.values()))
                    if member["require_plan"] and pending_plan and not member["approved"]:
                        member["status"] = "WAITING_PLAN"
                        while not cancel.is_set() and not member["approved"]:
                            try:
                                message = member["inbox"].get(timeout=.25)
                                if message.get("type") == "shutdown":
                                    cancel.set()
                            except queue.Empty:
                                pass
                        continue
                    if current["status"] == "in_progress":
                        self.board.finish(current["id"], owner, release=True)
                        member["assignment"] = None
                        break  # Avoid immediately reclaiming the same failed/incomplete task.
                    member.update(assignment=None, approved=None, status="IDLE")
                return {"name": name, "status": "cancelled" if cancel.is_set() else "completed"}
            finally:
                task = member.get("assignment")
                if task:
                    current = self.board.get(task["id"])
                    if current["status"] == "in_progress" and current["owner"] == owner:
                        self.board.finish(task["id"], owner, release=True)
                member.update(status="STOPPED", assignment=None, approved=None)
        return self.background.launch("队友 " + name, work, member["cancel"], kind="teammate")

    def send(self, to, content, sender="lead", kind="message"):
        with self.lock:
            if to == "lead":
                return self.background.store.emit(self.board.session, "teammate_message", {
                    "from": sender, "content": content}, notify=True)
            member = self.members.get(to)
            if not member:
                raise ValueError("队友不存在")
            member["inbox"].put({"type": kind, "from": sender, "content": content})
            if kind == "shutdown":
                member["cancel"].set()
            return {"delivered": to}

    def request_plan(self, teammate, task=""):
        member = self.members[teammate]
        member.update(require_plan=True, approved=None)
        return self.send(teammate, "请提交计划：" + task, kind="request_plan")

    def submit_plan(self, name, content):
        member = self.members[name]
        assignment = member["assignment"]
        if not assignment:
            raise ValueError("没有可审批的assignment")
        current = self.board.get(assignment["id"])
        plan = dict(request_id="plan_" + uuid4().hex, teammate=name, task_id=current["id"],
                    version=current["version"], content=content, status="pending")
        self.plans[plan["request_id"]] = plan
        self.background.store.emit(self.board.session, "plan_submission", plan, notify=True)
        return plan

    def review(self, request_id, approve, feedback=""):
        plan = self.plans[request_id]
        member = self.members[plan["teammate"]]
        current = self.board.get(plan["task_id"])
        if current["version"] != plan["version"] or current["owner"] != member["owner"]:
            raise ValueError("旧assignment的审批已失效")
        plan["status"] = "approved" if approve else "rejected"
        member["approved"] = (current["id"], current["version"]) if approve else None
        return self.send(plan["teammate"], {"plan": plan, "feedback": feedback}, kind="plan_review")

    def drain(self, name):
        member = self.members[name]
        values = []
        while True:
            try:
                values.append(member["inbox"].get_nowait())
            except queue.Empty:
                return values

    def can_act(self, name):
        member = self.members[name]
        task = member["assignment"]
        if not task:
            return False
        current = self.board.get(task["id"])
        approved = member["approved"]
        approved_now = approved == (task["id"], current["version"])
        completed_lease = current["status"] == "completed" and approved == (task["id"], current["version"] - 1)
        return (current["owner"] == member["owner"] and current["status"] in {"in_progress", "completed"}
                and (not member["require_plan"] or approved_now or completed_lease))

    def list(self):
        return [{k: m[k] for k in ("name", "role", "status", "assignment", "require_plan", "approved")}
                for m in self.members.values()]
