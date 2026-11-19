"""v3 behavioral acceptance. Synthetic models/data are not live quality evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from finagent.config import HarnessSettings, Settings, Limits
from finagent.data.contracts import Observation
from finagent.data.evidence import EvidenceStore
from finagent.errors import AgentError
from finagent.harness.host import SessionHost
from finagent.harness.inherited import cron_matches, validate_cron
from finagent.harness.kernel import Ledger
from finagent.harness.state import RuntimeStore
from finagent.harness.tasks import TaskBoard
from finagent.harness.tools import RuntimeTool
from finagent.models import ModelReply, ToolCall
from finagent.runtime import now
from tests.support import ScriptedModel, ServiceCase, call_reply, tencent_body


def text(value):
    return ModelReply({"role": "assistant", "content": value})


class HarnessCase(ServiceCase):
    def setUp(self):
        super().setUp()
        self.work = self.root / "workspace"
        self.work.mkdir()
        self.settings.data_dir = self.root / "data"
        self.store = self.service.store = EvidenceStore(self.settings.data_dir)
        self.http.store = self.store
        self.settings.harness = HarnessSettings(workspace=self.work, auto_memory=False)
        self.hosts = []

    def host(self, model=None, name="default", **kw):
        host = SessionHost(self.settings, name, model=model or ScriptedModel(text("完成")), service=self.service, **kw)
        self.hosts.append(host)
        return host

    def tearDown(self):
        for host in reversed(self.hosts):
            host.close()
        super().tearDown()

    def test_A01_A04_A05_A10_continuous_main_without_nlu_or_role_routing(self):
        model = ScriptedModel(text("第一点：现金流。第二点：回款。第三点：利润。"), text("第二点是回款速度。"))
        host = self.host(model)
        with patch("finagent.nlu.parser.prepare", side_effect=AssertionError("NLU must not run")), \
             patch("finagent.research.coordinator.ResearchCoordinator.run", side_effect=AssertionError("no forced roles")):
            self.assertEqual(host.submit("分析一下茅台基本面").status, "completed")
            self.assertEqual(host.submit("第二点再解释一下").answer, "第二点是回款速度。")
        self.assertIn("第二点：回款", json.dumps(model.messages_seen[1], ensure_ascii=False))
        self.assertEqual(len(host.state["objectives"]), 1)
        self.assertIsNone(host.ctx.nlu_admission)

    def test_A02_A03_hint_is_advisory_and_failure_is_nonfatal(self):
        self.settings.nlu.enabled = True
        host = self.host(ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519.SH"})), text("已取得资料")),
                         hint_parser=lambda *_: {"task_type": "concept_qa", "dimension_state": {}})
        self.assertEqual(host.submit("市盈率是什么意思？顺便看茅台").status, "completed")
        self.assertTrue(host.ctx.result_tools)
        host.hint_parser = lambda *_: (_ for _ in ()).throw(ValueError("bad frame"))
        host.model = ScriptedModel(text("继续回答"))
        self.assertEqual(host.submit("继续解释").status, "completed")

    def test_A06_A07_A08_multisymbol_constraints_survive_compaction_and_restart(self):
        host = self.host(ScriptedModel(text("已记录")))
        host.context.model = None
        host.submit("只比较现金流，不做估值")
        for index in range(7):
            host.submit("追问 " + str(index))
        host.ctx.bind_symbol("600519.SH")
        host.ctx.bind_symbol("000858.SZ")
        host.context.compact(host.messages, 12000, force=True)
        host.context.compact(host.messages, 12000, force=True)
        host.checkpoint()
        host.close()
        restored = self.host(ScriptedModel(text("现金流比较")))
        restored.submit("继续")
        self.assertEqual(set(restored.state["subjects"]), {"600519.SH", "000858.SZ"})
        self.assertIn("不做估值", json.dumps(restored.state, ensure_ascii=False))
        self.assertTrue(restored.state["archives"])
        self.assertTrue(restored.state["user_updates"])

    def test_periodic_summary_survives_restart_and_does_not_reinject_old_sources(self):
        from tests.test_context import summary_reply

        self.settings.harness.memory_enabled = False
        self.settings.limits.context_summary_turns = 2
        self.settings.limits.context_keep_turns = 1

        def respond(messages):
            body = json.loads(messages[-1]["content"])
            if body.get("_finagent_security", {}).get("kind") == "history_summary":
                return summary_reply(body["data"])
            return text("完成当前步骤")

        model = ScriptedModel(respond)
        host = self.host(model)
        requests = [f"原文标记{index}：" + "合成用户要求" * 200 for index in range(3)]
        for request in requests:
            self.assertEqual(host.submit(request).status, "completed")
        summary_message = next(m for m in host.messages if m.get("_finagent_origin") == "context_summary")
        note = json.loads(summary_message["content"])
        self.assertEqual(note["summary_status"], "structured")
        self.assertEqual(host.state["usage"]["summary"]["calls"], 1)
        self.assertEqual(host.state["last_ledger"]["operations"]["model"], 2)
        wire = json.dumps(model.messages_seen[-1], ensure_ascii=False)
        self.assertNotIn("原文标记0", wire)
        self.assertIn("原文标记2", wire)
        self.assertEqual(host.state["user_updates"][0]["text"], requests[0])
        self.assertEqual(host.state["objectives"][0]["goal"], requests[0])
        self.assertIn(requests[0], host.context.read_output(note["archived_history"]["full_output"], limit=30000)["text"])
        host.close()
        restored = self.host(ScriptedModel(text("恢复后继续")))
        self.assertIn(summary_message, restored.messages)
        self.assertEqual(restored.state["_summarized_user_sources"], host.state["_summarized_user_sources"])
        self.assertEqual(restored.submit("继续处理待办").status, "completed")

    def test_A09_year_report_reuse_and_dynamic_quote_invalidation(self):
        host = self.host()
        raw = self.store.save_raw(b"synthetic", {"synthetic": True})
        old = self.store.save_result(Observation(status="ok", source="synthetic", snapshot_id=raw,
              fetched_at=now()-__import__('datetime').timedelta(minutes=10), data={"price": 10}))
        host.ctx.result_tools[old.result_id] = "get_quote"
        host.ctx.evidence_ids.add(old.result_id)
        host.prepare(host.messages, host.ctx)
        self.assertIn(old.result_id, host.ctx.invalidated_result_ids)
        host.ctx.result_tools[old.result_id] = "search_evidence"
        host.ctx.invalidated_result_ids.clear()
        host.prepare(host.messages, host.ctx)
        self.assertNotIn(old.result_id, host.ctx.invalidated_result_ids)

    def test_A11_child_context_isolated_shared_budget_and_results_adopted(self):
        child_model = ScriptedModel(text("隔离子任务结论"))
        host = self.host(ScriptedModel(call_reply(("child", "task", {"description": "阅读报告"})), text("汇总完成")),
                         model_factory=lambda role: child_model)
        result = host.submit("分析长报告")
        self.assertEqual(result.status, "completed")
        self.assertEqual(host.state["last_ledger"]["operations"]["model"], 3)
        self.assertNotIn("汇总完成", json.dumps(child_model.messages_seen[0], ensure_ascii=False))
        self.assertIn("隔离子任务结论", json.dumps(host.messages, ensure_ascii=False))

    def test_A12_skill_then_bad_call_can_be_repaired(self):
        model = ScriptedModel(call_reply(("s", "load_skill", {"name": "news-events"})),
                              call_reply(("bad", "read_file", {})),
                              call_reply(("fixed", "write_file", {"path": "report.md", "content": "已修正"})), text("完成"))
        host = self.host(model)
        self.assertEqual(host.submit("加载方法并生成报告").status, "completed")
        self.assertEqual((self.work/"report.md").read_text(), "已修正")

    def test_A13_A27_alternating_failure_stops_and_pairs_remain_complete(self):
        steps = [call_reply((str(i), "missing_a" if i%2 else "missing_b", {})) for i in range(8)]
        host = self.host(ScriptedModel(*steps))
        result = host.submit("测试无进展")
        self.assertIn("NO_PROGRESS", result.reason)
        requested = {c["id"] for m in host.messages for c in m.get("tool_calls", [])}
        paired = {m["tool_call_id"] for m in host.messages if m["role"] == "tool"}
        self.assertEqual(requested, paired)
        self.assertLess(result.model_calls, 8)

    def test_A14_file_tools_shell_and_protected_paths(self):
        model = ScriptedModel(call_reply(("w", "write_file", {"path": "r.md", "content": "财务研究"})),
                              call_reply(("r", "read_file", {"path": "r.md"})),
                              call_reply(("s", "bash", {"command": "pwd"})), text("完成"))
        host = self.host(model)
        self.assertEqual(host.submit("写入并读取文件").status, "completed")
        with self.assertRaises(AgentError):
            host.permissions.path(str(self.root/"outside"))
        with self.assertRaises(AgentError):
            host.permissions.path(str(self.settings.data_dir))
        outputs = [json.loads(m["content"]) for m in host.messages if m["role"] == "tool"]
        self.assertEqual(outputs[2]["exit_code"], 0)

    def test_A15_hooks_order_and_denial(self):
        host = self.host(ScriptedModel(call_reply(("w", "write_file", {"path": "blocked", "content": "x"})), text("结束")))
        events = []
        host.hooks.register("UserPromptSubmit", lambda **kw: events.append("user"))
        host.hooks.register("PreToolUse", lambda **kw: "deny")
        host.hooks.register("PostToolUse", lambda **kw: events.append("post"))
        host.hooks.register("Stop", lambda **kw: events.append("stop"))
        host.submit("执行")
        self.assertEqual(events, ["user", "post", "stop"])
        self.assertFalse((self.work/"blocked").exists())

    def test_A16_A26_background_notification_is_durable_and_session_scoped(self):
        host, other = self.host(name="a"), self.host(name="b")
        done = threading.Event()
        work = host.background.launch("慢操作", lambda cancel, wid: (done.wait(2), {"value": 4})[1])
        self.assertTrue(host.background.active())
        self.assertEqual(other.submit("继续另一个会话").status, "completed")
        done.set()
        host.background.handles[work["work_id"]][0].join(3)
        self.assertTrue(host.store.pending("a"))
        self.assertFalse(host.store.pending("b"))
        self.assertEqual(host.submit().status, "completed")
        self.assertFalse(host.store.pending("a"))

    def test_A17_cron_at_least_once_and_cancel(self):
        host = self.host()
        job = host.scheduler.schedule("* * * * *", "提醒", recurring=False)
        host.scheduler.tick(datetime.now().astimezone())
        pending = host.store.pending(host.session_id)
        self.assertEqual(len(pending), 1)
        host.model = ScriptedModel(ModelReply({"role": "assistant", "content": ""}))
        self.assertEqual(host.submit().status, "failed")
        self.assertEqual(len(host.store.pending(host.session_id)), 1)
        host.scheduler.cancel(job["id"])
        self.assertFalse(host.store.pending(host.session_id))
        self.assertIsNone(validate_cron("*/5 9-15 * * 1,2,3,4,5"))

    def test_A18_task_claim_atomic_dependencies_and_recovery(self):
        host = self.host()
        first, second = host.board.create("原文"), host.board.create("复核")
        host.board.dependencies(second["id"], [first["id"]])
        with self.assertRaises(ValueError):
            host.board.claim(second["id"], "b")
        def claim(owner):
            try:
                return host.board.claim(first["id"], owner)["owner"]
            except ValueError:
                return None
        with ThreadPoolExecutor(2) as pool:
            values = list(pool.map(claim, ["a", "b"]))
        self.assertEqual(sum(v is not None for v in values), 1)
        owner = next(v for v in values if v)
        host.board.finish(first["id"], owner)
        self.assertEqual(host.board.claim(second["id"], "team_worker")["status"], "in_progress")
        host.board.recover("team_")
        self.assertEqual(host.board.get(second["id"])["status"], "pending")

    def test_A19_plan_approval_bound_to_assignment_version(self):
        host = self.host()
        task = host.board.create("检查")
        assigned = host.board.claim(task["id"], "team_worker")
        host.teams.members["worker"] = dict(name="worker", owner="team_worker", assignment=assigned,
                  require_plan=True, approved=None, inbox=__import__('queue').Queue(), cancel=threading.Event())
        plan = host.teams.submit_plan("worker", "只读核对")
        host.teams.review(plan["request_id"], True)
        self.assertTrue(host.teams.can_act("worker"))
        host.board.finish(task["id"], "team_worker", release=True)
        with self.assertRaises(ValueError):
            host.teams.review(plan["request_id"], True)
        self.assertFalse(host.teams.can_act("worker"))

    def test_A21_real_stdio_mcp_and_normalization_collision(self):
        server = self.work/"server.py"
        server.write_text('''import sys,json
for line in sys.stdin:
 r=json.loads(line)
 if "id" not in r:continue
 method=r["method"]
 if method=="initialize":value={"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"test","version":"1"}}
 elif method=="tools/list":value={"tools":[{"name":"search","description":"test","inputSchema":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}]}
 else:value={"content":[{"type":"text","text":r["params"]["arguments"]["query"]}]}
 print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":value}),flush=True)
''')
        self.settings.harness.mcp_servers = {"docs": {"command": [sys.executable, str(server)], "read_only": ["search"]}}
        host = self.host(ScriptedModel(call_reply(("c", "connect_mcp", {"name": "docs"})),
               call_reply(("m", "mcp__docs__search", {"query": "原文"})), text("完成")))
        self.assertEqual(host.submit("查外部文档").status, "completed")
        self.assertIn("mcp__docs__search", host.pool.tools)
        host.mcp.configurations["do-cs"] = {"transport": "mock"}
        host.mcp.configurations["do_cs"] = {"transport": "mock"}
        host.mcp.connect("do-cs")
        with self.assertRaises(ValueError):
            host.mcp.connect("do_cs")

    def test_A20_worktree_tools_use_assignment_cwd_and_preserve_changes(self):
        import subprocess
        host = self.host()
        def git(*args):
            return subprocess.run(["git", "-c", "user.name=Harness Test", "-c", "user.email=test@example.invalid",
                                   "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                                  cwd=self.work, capture_output=True, text=True, check=True)
        git("init")
        (self.work/"README.md").write_text("synthetic")
        git("add", "README.md")
        git("commit", "-m", "fixture")
        task = host.board.create("独立文件")
        bound = host.board.create_worktree("worker", task["id"])
        claimed = host.board.claim(task["id"], "team_worker")
        ctx = __import__('copy').copy(host.ctx)
        ctx.cwd = host.board.verify_cwd(claimed)
        host.workspace.write({"path": "result.md", "content": "isolated"}, ctx)
        self.assertFalse((self.work/"result.md").exists())
        self.assertEqual((Path(bound["cwd"])/"result.md").read_text(), "isolated")
        host.board.finish(task["id"], "team_worker")
        with self.assertRaises(ValueError):
            host.board.remove_worktree(task["id"], confirmed=True)
        lead_task=host.board.create("主Agent独立目录")
        lead_bound=host.board.create_worktree("lead",lead_task["id"])
        host.model=ScriptedModel(call_reply(("claim","claim_task",{"task_id":lead_task["id"]})),
               call_reply(("complete","complete_task",{"task_id":lead_task["id"]}),
                          ("after_complete","write_file",{"path":"lease.md","content":"same turn"})),text("主任务完成"))
        self.assertEqual(host.submit("在已绑定的任务目录处理文件").status,"completed")
        self.assertEqual((Path(lead_bound["cwd"])/"lease.md").read_text(),"same turn")
        self.assertFalse((self.work/"lease.md").exists())
        self.assertEqual(host.ctx.cwd,self.work)

    def test_A19_A26_real_teammate_loop_completes_and_notifies_lead(self):
        host = self.host()
        task = host.board.create("写入产物")
        worker = ScriptedModel(call_reply(("write", "write_file", {"path": "worker.md", "content": "child"})),
                               call_reply(("done", "complete_task", {"task_id": task["id"]})), text("子任务完成"))
        host.model_factory = lambda role: worker
        work = host.teams.spawn("worker", "reader", "完成已分配任务", task["id"], ledger=Ledger(self.settings.limits))
        deadline = time.monotonic()+4
        while time.monotonic() < deadline and (host.board.get(task["id"])["status"] != "completed"
                                               or not host.store.pending(host.session_id)):
            time.sleep(.02)
        self.assertEqual(host.board.get(task["id"])["status"], "completed")
        self.assertEqual((self.work/"worker.md").read_text(), "child")
        self.assertTrue(host.store.pending(host.session_id))
        host.teams.send("worker", "停止", kind="shutdown")
        host.background.handles[work["work_id"]][0].join(3)
        self.assertEqual(host.teams.members["worker"]["status"], "STOPPED")

    def test_A19_idle_teammate_uses_new_assignment_budget_not_expired_spawn_budget(self):
        host=self.host()
        def complete(messages):
            context=next(json.loads(m["content"]) for m in messages if isinstance(m.get("content"),str)
                         and m["content"].startswith('{') and 'assignment' in json.loads(m["content"]))
            return call_reply(("done","complete_task",{"task_id":context["assignment"]["id"]}))
        host.model_factory=lambda role:ScriptedModel(complete,text("完成新任务"))
        expired=Ledger(self.settings.limits);expired.started-=1000
        work=host.teams.spawn("idle","reader","等待并完成任务",ledger=expired)
        fresh=Ledger(self.settings.limits)
        from finagent.harness.extensions import create_task
        task=create_task(host,{"subject":"新用户要求的任务"},fresh)
        deadline=time.monotonic()+4
        while time.monotonic()<deadline and host.board.get(task["id"])["status"]!='completed':time.sleep(.02)
        self.assertEqual(host.board.get(task["id"])["status"],'completed')
        self.assertEqual(expired.counts['model'],0)
        self.assertGreater(fresh.counts['model'],0)
        host.teams.send('idle','停止',kind='shutdown')
        host.background.handles[work['work_id']][0].join(3)

    def test_A27_truncated_tool_is_not_executed_and_can_continue(self):
        host = self.host(ScriptedModel(call_reply(("cut", "write_file", {"path": "bad", "content": "x"}), finish_reason="length"),
                                      text("恢复后的回答")))
        result = host.submit("生成结果")
        self.assertEqual(result.status, "completed")
        self.assertFalse((self.work/"bad").exists())
        self.assertIn("MODEL_TRUNCATED", json.dumps(host.messages))

    def test_A28_A29_deep_research_is_explicit_background_and_scope_is_separate(self):
        from finagent.harness.terminal import command
        host = self.host(ScriptedModel(text("深入研究可以使用独立入口"), text("主会话继续")))
        with patch("finagent.harness.research.ResearchService.start", return_value={
                "id": "b"*32, "status": "completed", "content": "complete"}) as start:
            host.submit("你说的深度研究是什么")
            start.assert_not_called()
            command(host, "/research --mode deep_research --symbol 600519.SH 检查现金流", lambda _: None)
            for thread, _ in list(host.background.handles.values()):
                thread.join(3)
            start.assert_called_once()
            host.ctx.bind_symbol("000858.SZ")
            host.submit()
            self.assertEqual(host.ctx.symbol, "000858.SZ")
            self.assertIn("b"*32, host.state["research_links"])

    def test_A30_new_cli_and_session_lease(self):
        from finagent.cli import main
        from contextlib import redirect_stdout
        import io
        host = self.host()
        with self.assertRaises(AgentError):
            SessionHost(self.settings, host.session_id, model=ScriptedModel(text("never")))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["run", "old goal"]), 2)
            self.assertEqual(main(["chat"]), 2)

    def test_A29_report_adoption_requires_grant_keeps_focus_and_marks_old_quote(self):
        from finagent.harness.research import ResearchService
        from finagent.research.store import ResearchStore
        from finagent.research.contracts import ResearchScope
        from datetime import timedelta
        host=self.host()
        cutoff=now()-timedelta(days=1)
        scope=ResearchScope(goal="SYNTHETIC",symbol="600519.SH",as_of=cutoff)
        database=ResearchStore(self.settings.data_dir)
        run=database.create(scope,self.settings.research,"test")
        owner=database.acquire(run,"test")
        raw=self.store.save_raw(b"synthetic",{"synthetic":True})
        quote=self.store.save_result(Observation(status="ok",source="SYNTHETIC",symbol="600519.SH",snapshot_id=raw,
                 fetched_at=cutoff,data={"price":10,"available_at":cutoff.isoformat()}))
        database.grant(run,owner,"technical",quote.result_id,"get_quote")
        path=database.directory(run)/"report.md";path.write_text("历史合成报告")
        database.finish(run,owner,"completed","complete","pass",str(path))
        host.ctx.bind_symbol("000858.SZ")
        service=ResearchService(self.settings)
        with self.assertRaises(AgentError):service.read(run,host.ctx)
        result=service.read(run,host.ctx,authorized=[run])
        self.assertEqual(host.ctx.symbol,"000858.SZ")
        self.assertEqual(result["original_scope"]["symbol"],"600519.SH")
        self.assertIn(quote.result_id,host.ctx.invalidated_result_ids)

    def test_A08_state_changes_are_atomic_and_user_can_return_to_current_time(self):
        host=self.host()
        host.submit("截至2024-12-31只比较现金流，不做估值")
        first=host.state["user_updates"][-1]["id"]
        original=host.ctx.as_of
        args={"source_id":first,"updates":{"temporal_context":{"cutoff":"2024-12-31T23:59:59+08:00"},"objectives":"invalid"}}
        result=host.pool.execute(ToolCall("bad","update_state",json.dumps(args)),host.ctx,Ledger(self.settings.limits))
        self.assertEqual(result["error"],"INVALID_ARGUMENTS")
        self.assertEqual(host.ctx.as_of,original)
        args["updates"]["objectives"]=[]
        result=host.pool.execute(ToolCall("ok","update_state",json.dumps(args)),host.ctx,Ledger(self.settings.limits))
        self.assertNotIn("error",result)
        self.assertTrue(host.ctx.cutoff_explicit)
        host.submit("现在可以看估值，也看最新行情")
        latest=host.state["user_updates"][-1]["id"]
        args={"source_id":latest,"updates":{"temporal_context":{"mode":"current"},
              "constraint_changes":[{"source":first,"reason":"现在可以看估值"}]}}
        result=host.pool.execute(ToolCall("current","update_state",json.dumps(args)),host.ctx,Ledger(self.settings.limits))
        self.assertNotIn("error",result)
        self.assertFalse(host.ctx.cutoff_explicit)
        self.assertTrue(any(c["status"]=="superseded" for c in host.state["user_constraints"]))

    def test_A15_approval_shutdown_does_not_leave_model_worker_blocked(self):
        from finagent.harness.terminal import ApprovalBroker
        broker=ApprovalBroker();answers=[]
        thread=threading.Thread(target=lambda:answers.append(broker.ask("external_write",{})))
        thread.start();broker.requests.get(timeout=2)
        broker.close();thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(answers,[False])

    def test_A25_memory_consolidation_retains_sources_and_deleted_source_stays_forgotten(self):
        host=self.host()
        first=host.memory.write("输出偏好","回答简短",source="user_a")
        second=host.memory.write("输出偏好","用户后来要求详细",source="user_b")
        proposal=[{"keep_id":second["id"],"remove_ids":[first["id"]],"body":"用户最新要求详细回答","description":"输出偏好"}]
        host.memory.consolidate(ScriptedModel(text(json.dumps(proposal,ensure_ascii=False))))
        self.assertEqual(len(host.memory.records()),1)
        self.assertEqual(set(host.memory.records()[0]["sources"]),{"user_a","user_b"})
        host.memory.forget(second["id"])
        self.assertEqual(host.memory.write("输出偏好","换一种说法恢复旧偏好",source="user_a")["status"],"forgotten")

    def test_A27_call_ids_survive_lossy_history_compaction(self):
        host=self.host(ScriptedModel(call_reply(("same","write_file",{"path":"once","content":"first"})),text("完成")))
        host.context.model=None
        host.submit("写一次文件")
        host.model=ScriptedModel(text("继续"))
        for i in range(6):host.submit("追问"+str(i))
        host.context.compact(host.messages,12000,force=True)
        host.model=ScriptedModel(call_reply(("same","write_file",{"path":"once","content":"second"})))
        result=host.submit("继续处理")
        self.assertIn("MODEL_PROTOCOL_ERROR",result.reason)
        self.assertEqual((self.work/"once").read_text(),"first")

    def test_A31_tool_schema_cannot_fetch_external_files(self):
        host=self.host()
        with self.assertRaises(ValueError):
            host.pool.register(RuntimeTool("remote_schema","invalid",{"$ref":"file:///etc/passwd"},lambda *_:None))
        self.assertNotIn("remote_schema",host.pool.tools)

    def test_H01_H17_all_reference_tools_are_integrated(self):
        host=self.host()
        definitions=json.loads(Path('src/finagent/harness/builtin_schemas.json').read_text())
        self.assertEqual(len(definitions),26)
        self.assertTrue({t['name'] for t in definitions}<=host.pool.tools.keys())
        self.assertIn("Workflow",host.pool.tools)

    def test_A22_workflow_parallel_pipeline_schema_repair_and_resume(self):
        host = self.host()
        calls = []
        def runner(prompt, schema, ledger):
            calls.append(prompt)
            return {"value": 3} if "校验错误" in prompt else {"wrong": True}
        host.workflows.runner = runner
        schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
        async def flow(ctx, args):
            ctx.phase("检查")
            async def stage(value, item, index):
                return await ctx.agent("输入" + str(item), schema, label=str(index))
            return await ctx.pipeline(args["items"], stage)
        host.workflows.register("check", "有界检查", flow, version="1")
        result = host.workflows.run("check", {"items": [1, 2]}, Ledger(self.settings.limits))
        self.assertEqual(result["result"], [{"value": 3}, {"value": 3}])
        count = len(calls)
        host.workflows.run("check", {"items": [1, 2]}, Ledger(self.settings.limits), result["id"])
        self.assertEqual(len(calls), count)
        with host.store.lease(result["id"]):
            with self.assertRaises(AgentError):
                host.workflows.run("check", {"items": [1]}, Ledger(self.settings.limits), result["id"])

    def test_A23_A24_optional_goal_independent_check_and_suspension(self):
        model = ScriptedModel(text("尝试完成"), text('{"ok":false,"reason":"缺少工具证据","impossible":false}'),
                              text("已有验证"), text('{"ok":true,"reason":"条件满足","impossible":false}'))
        host = self.host(model)
        host.goals.set("完成验证")
        self.assertEqual(host.submit("开始").status, "completed")
        self.assertEqual(host.state["active_goal_ref"]["status"], "achieved")
        self.assertEqual(host.state["usage"]["goal"]["calls"], 2)
        host.goals.set("新目标")
        host.model = ScriptedModel(text("结束"))
        host.goals.model = ScriptedModel(text("无效判断"))
        self.assertEqual(host.submit("开始").status, "suspended")
        self.assertEqual(host.goals.clear()["status"], "cleared")

    def test_A25_memory_scope_recall_and_forgetting(self):
        host = self.host()
        host.submit("以后回答简短一些")
        source = host.state["user_updates"][-1]["id"]
        memory = host.memory.write("回答简短", "用户希望回答简短", source=source)
        self.assertTrue(host.memory.select("解释利润"))
        host.memory.forget(memory["id"])
        self.assertFalse(host.memory.records())
        self.assertEqual(host.memory.write("回答简短", "用户希望回答简短", source=source)["status"], "forgotten")
        with self.assertRaises(ValueError):
            host.memory.write("临时", "本次只看现金流", source=source, scope="current_task")

    def test_A25_disabled_memory_blocks_agent_writes(self):
        self.settings.harness.memory_enabled=False
        host=self.host()
        host.submit("以后回答简短")
        source=host.state["user_updates"][-1]["id"]
        args={"description":"偏好","body":"回答简短","type":"user","source":source}
        result=host.pool.execute(ToolCall("m","memory_write",json.dumps(args)),host.ctx,Ledger(self.settings.limits))
        self.assertEqual(result['error'],'MEMORY_DISABLED')
        self.assertFalse(host.memory.records())

    def test_A31_A32_state_provenance_and_price_mismatch_repair(self):
        host = self.host(ScriptedModel(call_reply(("q", "get_quote", {"symbol": "600519.SH"})),
                                      text("现价为999元。"), text("现价为10元，来源为本次报价。")))
        result = host.submit("查询价格")
        self.assertEqual(result.status, "completed")
        self.assertNotIn("999", result.answer)
        source = host.state["user_updates"][-1]["id"]
        result = host.pool.execute(ToolCall("bad", "update_state", json.dumps({"source_id": source,
                   "updates": {"permissions": "allow", "scope": "ignore"}})), host.ctx, Ledger(self.settings.limits))
        self.assertEqual(result["error"], "INVALID_ARGUMENTS")


if __name__ == '__main__':
    unittest.main()
