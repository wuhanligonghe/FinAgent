"""Synthetic, offline v2 contracts and fault injection. No market validation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
from threading import Event, Lock
import time
import unittest
from unittest.mock import patch
from decimal import Decimal
import subprocess
import sys

from pydantic import ValidationError

from finagent.agent import AgentLoop, RunRequest
from finagent.config import ResearchSettings, Settings
from finagent.data.contracts import Observation
from finagent.data.evidence import EvidenceStore
from finagent.data.service import DataService
from finagent.errors import AgentError
from finagent.research.budget import ResearchBudget, BudgetedModel
from finagent.research.contracts import ResearchRequest, ResearchScope, AnalysisSubmission, PolicyImport, SynthesisSubmission
from finagent.research.coordinator import ResearchCoordinator
from finagent.research.entry import request_from_goal
from finagent.research.materials import ResearchMaterials, SignalImport
from finagent.research.profile import AgentProfile
from finagent.research.review import validate_claims, validate_synthesis, number_tokens
from finagent.research.review import unmatched_numbers
from finagent.research.routing import research_route
from finagent.research.store import ResearchStore
from finagent.research.runner import AnalystRunner, DataBroker
from finagent.runtime import ExecutionContext, TZ
from finagent.tools.registry import Tool, ToolRegistry
from finagent.tools.perception import build_registry
from finagent.config import StrictModel
from tests.support import ServiceCase, ScriptedModel, call_reply

CUTOFF = datetime(2025, 5, 8, 23, 59, 59, tzinfo=TZ)


class ResearchV2Tests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.scope = ResearchScope(goal="合成五维研究", symbol="600519.SH", as_of=CUTOFF)
        self.database = ResearchStore(self.root)
        self.run = self.database.create(self.scope, self.settings.research, "test-version")
        self.owner = self.database.acquire(self.run, "test-version")
        self.cancelled = Event()
        self.budget = ResearchBudget(self.database, self.run, self.owner, self.settings.research, self.cancelled)

    def context(self, tool="get_stock_news"):
        ctx = ExecutionContext(as_of=CUTOFF, symbol="600519.SH", frozen_symbols=frozenset(["600519.SH"]))
        raw = self.store.save_raw(b'{"value":12}', {"source": "synthetic", "fetched_at": CUTOFF.isoformat()})
        result = self.store.save_result(Observation(status="ok", source="synthetic", snapshot_id=raw, symbol="600519.SH",
            data={"value": "12", "available_at": CUTOFF.isoformat()}))
        ctx.result_tools[result.result_id] = tool
        ctx.evidence_ids.update([result.result_id, raw])
        return ctx, result

    def submission(self, result, text="合成数值为12"):
        return AnalysisSubmission(content_status="partial", summary="合成", report_markdown=text,
            claims=[{"claim_id": "c1", "kind": "fact", "text": text, "evidence_refs": [
                {"result_id": result.result_id, "snapshot_id": result.snapshot_id, "locator": "/data/value"}]}])

    def test_scope_frozen_hash_and_duplicate_roles(self):
        with self.assertRaises(ValidationError):
            self.scope.symbol = "000001.SZ"
        with self.assertRaises(ValidationError):
            ResearchScope(**{**self.scope.model_dump(), "scope_hash": "forged"})
        with self.assertRaises(ValidationError):
            ResearchRequest(goal="x", symbol="600519.SH", as_of=CUTOFF, analysts=["technical", "technical"])

    def test_config_reserve_and_unknown_model_rejected(self):
        with self.assertRaises(ValidationError):
            ResearchSettings(model_calls=1)
        with self.assertRaises(ValidationError):
            ResearchSettings(role_models={"technical": "missing"})
        self.assertEqual(Settings().research.max_concurrent, 3)

    def test_routing_concepts_negation_quotes_and_subset(self):
        for goal in ("什么是情绪分析", "不要深入分析，查询价格", "解释“深入研究”这个词", "分析一下这只股票"):
            self.assertEqual(research_route(goal)[0], "direct")
        self.assertEqual(research_route("深入分析这家公司")[1], [])
        self.assertEqual(research_route("只看基本面和政策")[1], ["fundamentals", "policy"])

    def test_request_historical_cutoff_and_name(self):
        req = request_from_goal("截至2025-05-08分析贵州茅台的新闻", self.settings, depth="specialist", analysts=["news"])
        self.assertEqual(req.symbol, "600519.SH")
        self.assertEqual(req.as_of, CUTOFF)

    def test_research_entry_reuses_online_name_resolution(self):
        class Resolver:
            def __init__(inner, settings):
                inner.store = self.store
            def resolve_entity(inner, query, ctx):
                self.assertEqual(query, "测试新公司")
                ctx.bind_symbol("600900.SH")
                ctx.source_requests += 1
                return Observation(status="ok", source="SYNTHETIC", data={"selected": {"symbol": ctx.symbol}})
            def close(inner):
                pass
        with patch("finagent.data.service.DataService", Resolver):
            request = request_from_goal("分析测试新公司", self.settings, depth="specialist", analysts=["news"])
        self.assertEqual(request.symbol, "600900.SH")
        self.assertEqual(request.resolution_source_requests, 1)

    def test_budget_atomic_race_for_one_credit(self):
        self.budget.config = ResearchSettings(model_calls=1, reserve_model=0)
        def take(_):
            try:
                self.budget.reserve("model", "race")
                return 1
            except AgentError:
                return 0
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(sum(pool.map(take, range(24))), 1)
        self.assertEqual(self.database.usage(self.run)["calls"]["model"], 1)

    def test_reserved_pool_is_inside_total(self):
        self.budget.config = ResearchSettings(model_calls=3, reserve_model=2)
        self.budget.reserve("model", "expert")
        with self.assertRaises(AgentError):
            self.budget.reserve("model", "expert")
        self.budget.reserve("model", "review", True)
        self.budget.reserve("model", "synthesis", True)
        with self.assertRaises(AgentError):
            self.budget.reserve("model", "review", True)

    def test_budget_persists_elapsed_work_before_crash(self):
        with self.database.transaction() as db:
            db.change("research_runs", {"segment_started":time.time() - 500}, id=self.run)
        self.budget.reserve("tool", "checkpoint")
        self.assertGreaterEqual(self.database.get(self.run)["active_used"], 499)
        with patch("finagent.research.store.process_identity", return_value=None):
            self.database.acquire(self.run, "test-version", resume=True)
        self.assertGreaterEqual(self.database.get(self.run)["active_used"], 499)

    def test_model_error_and_unknown_usage_remain_charged(self):
        class Broken:
            def complete(self, *args):
                raise RuntimeError("synthetic")
        model = BudgetedModel(Broken(), self.budget, "test")
        with self.assertRaises(RuntimeError):
            model.complete([], [], 1)
        usage = self.database.usage(self.run)
        self.assertEqual(usage["calls"]["model"], 1)
        self.assertEqual(usage["token_status"], "unknown")
        self.assertIsNone(usage["prompt_tokens"])

    def test_frozen_symbols_and_cutoff(self):
        ctx, _ = self.context()
        ctx.frozen_as_of = CUTOFF
        with self.assertRaises(AgentError):
            ctx.bind_symbol("000001.SZ")
        ctx.as_of -= timedelta(days=1)
        with self.assertRaises(AgentError):
            ctx.check()

    def test_valid_claim_and_fabricated_number(self):
        ctx, result = self.context()
        validate_claims(self.submission(result), ctx, self.store)
        with self.assertRaisesRegex(AgentError, "UNSUPPORTED_CLAIM_NUMBER"):
            validate_claims(self.submission(result, "合成数值为999"), ctx, self.store)
        self.assertIn(999, number_tokens("收入999亿元"))

    def test_presentation_rounding_preserves_magnitude(self):
        self.assertFalse(unmatched_numbers("跌幅2.9736%", {Decimal("-2.97356003295")}))
        self.assertTrue(unmatched_numbers("量比1.5122", {Decimal("4.13")}))

    def test_expert_prose_cannot_bypass_claim_register(self):
        ctx, result = self.context()
        payload = self.submission(result)
        payload.report_markdown += "利润999亿元"
        with self.assertRaisesRegex(AgentError, "UNSUPPORTED_REPORT_NUMBER"):
            validate_claims(payload, ctx, self.store)

    def test_rejected_narrative_only_recovers_checked_subset(self):
        from finagent.research.review import checked_subset
        ctx, result = self.context()
        payload = self.submission(result).model_dump(mode="json")
        bad = dict(payload["claims"][0], claim_id="bad", text="利润999亿元")
        payload["claims"].append(bad)
        payload["report_markdown"] = "污染正文包含999"
        recovered = checked_subset([payload], ctx, self.store)
        self.assertEqual(len(recovered["claims"]), 1)
        self.assertNotIn("999", recovered["report_markdown"])
        self.assertFalse(recovered["recovery"]["original_narrative_accepted"])
        self.assertEqual(recovered["content_status"], "partial")

    def test_frozen_pack_hash_and_network_fail_closed(self):
        from finagent.research.evaluation import freeze_packs, FrozenPack
        ctx, result = self.context()
        target = self.root / "frozen"
        manifest = freeze_packs(self.settings, self.run, target)
        self.assertEqual(len(manifest["packs"]), 6)
        self.assertFalse(manifest["packs"][0]["recorded_live_run"])
        pack = FrozenPack(target)
        registry = build_registry(self.service)
        pack.registry(registry, manifest["packs"][0])
        value = registry.execute("get_stock_news", {"symbol": "600519.SH", "start": "2025-05-01", "end": "2025-05-08"}, ctx)
        self.assertEqual(value.error.code, "FROZEN_COVERAGE_MISSING")
        self.assertFalse(self.requests)
        selected = next((target / "input" / "results").glob("*.json"))
        selected.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(AgentError, "EVALUATION_INPUT_CHANGED"):
            FrozenPack(target)

    def test_cross_task_evidence_denied(self):
        _, result = self.context()
        ctx = ExecutionContext(as_of=CUTOFF, symbol="600519.SH")
        with self.assertRaisesRegex(AgentError, "EVIDENCE_DENIED"):
            validate_claims(self.submission(result), ctx, self.store)

    def test_wrong_snapshot_and_locator_denied(self):
        ctx, result = self.context()
        payload = self.submission(result)
        payload.claims[0].evidence_refs[0].locator = "/data/absent"
        with self.assertRaisesRegex(AgentError, "LOCATOR_NOT_FOUND"):
            validate_claims(payload, ctx, self.store)
        payload.claims[0].evidence_refs[0].locator = "/data/value"
        other = self.store.save_raw(b"other", {})
        ctx.evidence_ids.add(other)
        payload.claims[0].evidence_refs[0].snapshot_id = other
        with self.assertRaisesRegex(AgentError, "SNAPSHOT_MISMATCH"):
            validate_claims(payload, ctx, self.store)

    def test_future_and_stale_evidence_denied(self):
        ctx, result = self.context()
        payload = self.submission(result)
        result.data["available_at"] = (CUTOFF + timedelta(seconds=1)).isoformat()
        path = self.store.root / "results" / (result.result_id + ".json")
        path.write_text(result.model_dump_json(), encoding="utf-8")
        with self.assertRaisesRegex(AgentError, "AFTER_CUTOFF"):
            validate_claims(payload, ctx, self.store)
        result.data["available_at"] = CUTOFF.isoformat()
        result.stale, result.status = True, "partial"
        path.write_text(result.model_dump_json(), encoding="utf-8")
        with self.assertRaisesRegex(AgentError, "UNUSABLE_EVIDENCE"):
            validate_claims(payload, ctx, self.store)

    def test_synthesis_unknown_claim_and_new_number_denied(self):
        _, result = self.context()
        artifact = {"task_id": "news", **self.submission(result).model_dump(mode="json")}
        with self.assertRaises(AgentError):
            validate_synthesis(SynthesisSubmission(report_markdown="合成12", claim_refs=["wrong:c1"]), [artifact])
        with self.assertRaises(AgentError):
            validate_synthesis(SynthesisSubmission(report_markdown="收入999亿元", claim_refs=["news:c1"]), [artifact])
        self.assertTrue(validate_synthesis(SynthesisSubmission(report_markdown="合成12", claim_refs=["news:c1"]), [artifact]))

    def test_permission_view_and_invalid_tool_are_budgeted(self):
        ctx, _ = self.context()
        ctx.research_budget, ctx.research_task = self.budget, "test"
        registry = build_registry(self.service).view(["get_stock_news"])
        self.assertEqual(registry.execute("start_stock_research", {}, ctx).error.code, "UNKNOWN_TOOL")
        self.assertEqual(self.database.usage(self.run)["calls"]["tool"], 1)

    def test_cancel_prevents_tool_and_new_source_requests(self):
        ctx, _ = self.context()
        ctx.cancelled = self.cancelled
        ctx.research_budget = self.budget
        self.cancelled.set()
        with self.assertRaises(AgentError):
            ctx.source_request()
        self.assertEqual(ctx.source_requests, 0)
        self.assertEqual(build_registry(self.service).execute("get_quote", {"symbol": "600519.SH"}, ctx).error.code, "CANCELLED")

    def test_expert_profile_does_not_trigger_default_keywords(self):
        model = ScriptedModel(lambda _: __import__("tests.support", fromlist=["final_reply"]).final_reply(answer="普通文字"))
        ctx = ExecutionContext(as_of=CUTOFF)
        result = AgentLoop(model, self.registry, self.store, profile=AgentProfile("test", "synthetic")).run(RunRequest("综合研究同比", require_data=False), ctx)
        self.assertEqual(result.status, "completed")
        self.assertEqual(ctx.required_tools, set())

    def test_submission_can_only_be_corrected_once(self):
        ctx, _ = self.context()
        ctx.research_budget = self.budget
        registry = ToolRegistry()
        registry.register(Tool("submit_analysis", "synthetic", AnalysisSubmission, lambda a, c: None, False))
        for _ in range(2):
            self.assertEqual(registry.execute("submit_analysis", {}, ctx).error.code, "VALIDATION_ERROR")
        self.assertEqual(registry.execute("submit_analysis", {}, ctx).error.code, "SUBMISSION_LIMIT")

    def test_store_live_owner_and_stale_attempt(self):
        with self.assertRaisesRegex(AgentError, "RESEARCH_BUSY"):
            self.database.acquire(self.run, "test-version", resume=True)
        attempt = self.database.begin_task(self.run, self.owner, "technical", "technical", "analyst")
        self.database.end_task(self.run, self.owner, "technical", attempt, "failed")
        with self.assertRaises(AgentError):
            self.database.end_task(self.run, self.owner, "technical", attempt, "completed", {"summary": "late"})

    def test_late_write_cannot_modify_accepted_artifact(self):
        attempt = self.database.begin_task(self.run, self.owner, "news", "news", "analyst")
        self.database.end_task(self.run, self.owner, "news", attempt, "completed", {"summary": "original"})
        row = self.database.tasks(self.run)[0]
        with self.assertRaises(AgentError):
            self.database.end_task(self.run, self.owner, "news", attempt, "completed", {"summary": "late"})
        self.assertEqual(self.database.read_artifact(row)["summary"], "original")

    def test_artifact_missing_and_hash_change(self):
        attempt = self.database.begin_task(self.run, self.owner, "news", "news", "analyst")
        self.database.end_task(self.run, self.owner, "news", attempt, "completed", {"summary": "synthetic"})
        row = self.database.tasks(self.run)[0]
        self.assertEqual(self.database.read_artifact(row)["summary"], "synthetic")
        (self.root / row["artifact"]).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(AgentError, "ARTIFACT_CHANGED"):
            self.database.read_artifact(row)

    def test_crash_recovery_keeps_budget_and_events(self):
        self.budget.reserve("model", "pending")
        self.database.begin_task(self.run, self.owner, "news", "news", "analyst")
        old_seq = self.database.events(self.run)[-1]["seq"]
        with patch("finagent.research.store.process_identity", return_value=None):
            owner = self.database.acquire(self.run, "test-version", resume=True)
        self.assertNotEqual(owner, self.owner)
        self.assertEqual(self.database.tasks(self.run)[0]["status"], "interrupted")
        self.assertEqual(self.database.usage(self.run)["calls"]["model"], 1)
        self.assertGreater(self.database.events(self.run)[-1]["seq"], old_seq)
        with self.assertRaises(AgentError):
            self.budget.reserve("model", "late")

    def test_real_process_exit_is_recoverable_without_new_credits(self):
        child_root = self.root / "child"
        code = '''
import os
from threading import Event
from finagent.research.store import ResearchStore
from finagent.research.contracts import ResearchScope
from finagent.research.budget import ResearchBudget
from finagent.config import ResearchSettings
from finagent.runtime import TZ
from datetime import datetime
s=ResearchStore(ROOT)
scope=ResearchScope(goal="SYNTHETIC",symbol="600519.SH",as_of=datetime(2025,5,8,tzinfo=TZ))
r=s.create(scope,ResearchSettings(),"child")
o=s.acquire(r,"child")
b=ResearchBudget(s,r,o,ResearchSettings(),Event())
b.reserve("model","news")
s.begin_task(r,o,"news","news","analyst")
print(r,flush=True)
os._exit(0)
'''.replace("ROOT", repr(str(child_root)))
        child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=True)
        run = child.stdout.strip()
        database = ResearchStore(child_root)
        database.acquire(run, "child", resume=True)
        self.assertEqual(database.usage(run)["calls"]["model"], 1)
        self.assertEqual(database.tasks(run)[0]["status"], "interrupted")

    def test_macro_html_new_layout_and_no_silent_parse_success(self):
        from finagent.research.data_tools import macro_html, WindowArgs
        from finagent.data.http import RawResponse
        class HTTP:
            def __init__(inner, broken=False):
                inner.broken = broken
            def get(inner, source, url, ctx, **kwargs):
                if "cywjh" in url:
                    body = b'<a href="https://finance.eastmoney.com/a/202505083900397674.html">test</a>'
                elif inner.broken:
                    body = b'<html>unexpected page</html>'
                else:
                    body = '<html><title>合成新闻</title><div class="infos"><div class="item">2025年05月08日 12:30</div></div><div id="ContentBody">合成正文</div></html>'.encode()
                raw = self.store.save_raw(body, {"source": "SYNTHETIC", "fetched_at": CUTOFF.isoformat()})
                return RawResponse(body, source, url, CUTOFF.isoformat(), raw, "utf8")
            def close(inner):
                pass
        ctx = ExecutionContext(as_of=CUTOFF, symbol="600519.SH")
        self.service.http.close()
        self.service.http = HTTP()
        args = WindowArgs(start="2025-05-01", end="2025-05-08")
        value = macro_html(self.service, args, ctx, ["primary_failed"])
        self.assertEqual(len(value.data["articles"]), 1)
        self.assertEqual(value.data["articles"][0]["time_precision"], "minute")
        self.service.http.broken = True
        with self.assertRaisesRegex(AgentError, "INVALID_RESPONSE"):
            macro_html(self.service, args, ctx, [])

    def test_policy_date_precision_scope_and_revision(self):
        source = self.root / "policy.txt"
        source.write_text("合成政策条款，仅离线测试", encoding="utf-8")
        materials = ResearchMaterials(self.store)
        spec = PolicyImport(title="synthetic", source_url="https://example.test/policy", publisher="SYNTHETIC",
            published_at=datetime(2025, 5, 8, tzinfo=TZ), effective_at=datetime(2025, 6, 1, tzinfo=TZ), status="announced", symbols=["600519.SH"])
        doc = materials.import_policy(source, spec)
        ctx = ExecutionContext(as_of=CUTOFF, symbol="600519.SH")
        self.assertFalse(materials.policies(ctx))
        ctx.as_of += timedelta(days=1)
        self.assertEqual(materials.policies(ctx)[0]["as_of_status"], "announced")
        spec.published_at = datetime(2025, 5, 9, tzinfo=TZ)
        spec.version, spec.supersedes = "2", doc["policy_id"]
        materials.import_policy(source, spec)
        ctx.as_of += timedelta(days=1)
        self.assertEqual(len(materials.policies(ctx)), 1)
        self.assertEqual(materials.policies(ctx)[0]["version"], "2")
        ctx.symbol = "000001.SZ"
        self.assertFalse(materials.policies(ctx))

    def test_policy_read_requires_grant(self):
        path = self.root / "policy.txt"
        path.write_text("测试原文", encoding="utf-8")
        materials = ResearchMaterials(self.store)
        doc = materials.import_policy(path, PolicyImport(title="test", source_url="https://example.test/p", publisher="SYNTHETIC",
            published_at=datetime(2025, 5, 1, tzinfo=TZ), symbols=["600519.SH"]))
        ctx = ExecutionContext(as_of=CUTOFF, symbol="600519.SH")
        with self.assertRaises(AgentError):
            materials.read_policy(ctx, doc["policy_id"], doc["blocks"][0]["locator"])
        materials.search_policy(ctx)
        self.assertEqual(materials.read_policy(ctx, doc["policy_id"], doc["blocks"][0]["locator"]).status, "ok")

    def test_signal_import_replay_and_cutoff(self):
        path = self.root / "flow.json"
        path.write_text(json.dumps({"rows": [{"date": "2025-05-07", "main_net_cny": "123"}]}), encoding="utf-8")
        spec = SignalImport(kind="fund_flow", source="SYNTHETIC", source_url="https://example.test/flow", symbol="600519.SH",
            available_at=CUTOFF - timedelta(days=1), data_time=CUTOFF - timedelta(days=1), definition="合成测试", units={"main_net_cny": "CNY"}, historical_version=True)
        materials = ResearchMaterials(self.store)
        materials.import_signal(path, spec)
        ctx = ExecutionContext(as_of=CUTOFF, symbol="600519.SH")
        value = materials.signal("fund_flow", ctx, (CUTOFF - timedelta(days=5)).date(), CUTOFF.date())
        self.assertEqual(value.data["rows"][0]["main_net_cny"], "123")
        ctx.as_of -= timedelta(days=3)
        self.assertIsNone(materials.signal("fund_flow", ctx, ctx.as_of.date(), ctx.as_of.date()))

    def test_broker_singleflight_and_scope_independence(self):
        broker, calls, lock = DataBroker(), [], Lock()
        class Args(StrictModel):
            value: int = 1
        def load(a, c):
            with lock:
                calls.append(1)
            time.sleep(0.03)
            return Observation(status="ok", data={"value": a.value})
        registry = ToolRegistry()
        registry.register(Tool("get_stock_news", "synthetic", Args, load))
        broker.wrap(registry, self.scope)
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: registry.execute("get_stock_news", {}, ExecutionContext(as_of=CUTOFF)), range(3)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(r.status == "ok" for r in results))


class FakeRunner:
    """Coordinator-only synthetic fault fixture; not a real analyst result."""
    def __init__(self, tracker, settings, store, budget, scope):
        self.tracker, self.budget = tracker, budget
        self.store, self.scope = store, scope

    def run(self, role, task, attempt, **kwargs):
        tracker = self.tracker
        with tracker["lock"]:
            tracker["calls"].append(task)
            tracker["active"] += 1
            tracker["maximum"] = max(tracker["maximum"], tracker["active"])
        try:
            time.sleep(0.03)
            self.budget.remaining(role in ("review", "synthesis"))
            if role in tracker.get("fail", set()):
                raise AgentError("SYNTHETIC_FAILURE", "离线故障")
            if role == "review":
                return {"summary": "synthetic review", "issues": tracker.get("issues", [])}
            if role == "synthesis":
                return {"summary": "synthetic synthesis", "report_markdown": "合成报告", "claim_refs": []}
            evidence = EvidenceStore(self.store.root)
            raw = evidence.save_raw(b'{"value":"SYNTHETIC"}', {"source": "SYNTHETIC", "fetched_at": CUTOFF.isoformat()})
            result = evidence.save_result(Observation(status="ok", source="SYNTHETIC", snapshot_id=raw, data={"value": "SYNTHETIC"}))
            self.store.grant(self.budget.run, self.budget.owner, task, result.result_id, "synthetic")
            return {"analyst": role, "scope_hash": self.scope.scope_hash, "content_status": "complete", "summary": "合成", "report_markdown": "合成",
                    "claims": [{"claim_id": "c1", "kind": "fact", "text": "SYNTHETIC", "evidence_refs": [
                    {"result_id": result.result_id, "snapshot_id": raw, "locator": "/data/value"}], "calculation_refs": []}], "missing": [], "limitations": []}
        finally:
            with tracker["lock"]:
                tracker["active"] -= 1


class CoordinatorV2Tests(ServiceCase):
    def tracker(self, fail=()):
        return {"lock": Lock(), "calls": [], "active": 0, "maximum": 0, "fail": set(fail)}

    def coordinator(self, tracker):
        return ResearchCoordinator(self.settings, runner_factory=lambda *args: FakeRunner(tracker, *args))

    def request(self):
        return ResearchRequest(goal="SYNTHETIC", symbol="600519.SH", as_of=CUTOFF)

    def test_parallel_failure_isolation_and_partial_report(self):
        tracker = self.tracker(["policy"])
        state = self.coordinator(tracker).run(self.request())
        self.assertEqual(state["content"], "partial")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(tracker["maximum"], 3)
        self.assertEqual(len([t for t in state["tasks"] if t["status"] == "completed"]), 6)
        self.assertTrue(Path(state["report"]).is_file())

    def test_resume_reuses_completed_roles_and_keeps_scope(self):
        tracker = self.tracker(["policy"])
        coordinator = self.coordinator(tracker)
        first = coordinator.run(self.request())
        before = {r: tracker["calls"].count(r) for r in ("technical", "news", "fundamentals", "sentiment")}
        tracker["fail"].clear()
        second = coordinator.resume(first["id"])
        self.assertEqual(second["content"], "complete")
        self.assertEqual(second["scope"]["as_of"], first["scope"]["as_of"])
        self.assertEqual(before, {r: tracker["calls"].count(r) for r in before})

    def test_cancelled_run_never_admits_late_results(self):
        tracker = self.tracker()
        cancelled = Event()
        cancelled.set()
        state = self.coordinator(tracker).run(self.request(), cancelled=cancelled)
        self.assertEqual(state["status"], "cancelled")
        self.assertFalse(any(t["status"] == "completed" for t in state["tasks"]))
        with self.assertRaises(AgentError):
            self.coordinator(tracker).resume(state["id"])

    def test_no_sources_preserves_five_missing_roles(self):
        tracker = self.tracker(["technical", "sentiment", "news", "fundamentals", "policy"])
        state = self.coordinator(tracker).run(self.request())
        self.assertEqual(state["content"], "unavailable")
        report = json.loads((Path(state["report"]).parent / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(set(report["coverage"]), set(self.request().analysts))
        self.assertTrue(all(s == "unavailable" for s in report["coverage"].values()))

    def test_report_followup_adopts_evidence_and_switch_drops_link(self):
        from finagent.session import Conversation
        tracker = self.tracker()
        state = self.coordinator(tracker).run(self.request())
        conversation = Conversation(self.store, "research-followup-test")
        conversation.remember_research(state)
        context = conversation.context("那这份报告有什么风险", self.settings.limits, as_of=CUTOFF)
        self.assertEqual(context.scope["research_id"], state["id"])
        self.assertFalse(context.evidence_ids)
        value = build_registry(self.service).execute("read_research_artifact", {"research_id": state["id"]}, context)
        self.assertEqual(value.status, "ok")
        self.assertTrue(any(key.startswith("raw_") for key in context.evidence_ids))
        self.assertFalse(context.perception_results)
        switched = conversation.context("换成宁德时代", self.settings.limits, as_of=CUTOFF)
        self.assertNotIn("research_id", switched.scope)
        context.as_of = CUTOFF - timedelta(days=1)
        self.assertEqual(build_registry(self.service).execute("read_research_artifact", {"research_id": state["id"]}, context).error.code, "AFTER_CUTOFF")

    def test_followup_is_one_bounded_round(self):
        tracker = self.tracker()
        tracker["issues"] = [{"task_id": "news", "claim_id": "c1", "severity": "major", "description": "SYNTHETIC",
                               "question": "核对原始来源" + str(i), "success_condition": "取得原始时间"} for i in range(4)]
        state = self.coordinator(tracker).run(self.request())
        self.assertEqual(tracker["calls"].count("review"), 1)
        self.assertEqual(sum(c.startswith("followup_") for c in tracker["calls"]), 2)
        self.assertEqual(state["content"], "partial")

    def test_real_agent_loop_tool_observation_submission_and_synthesis(self):
        """Real executor with synthetic model/data; never a market demo."""
        settings = self.settings

        class NewsService(DataService):
            def news(self, symbol, start, end, limit, ctx, source=None):
                raw = self.store.save_raw(b'{"headline":"SYNTHETIC"}', {"source": "synthetic_news", "fetched_at": CUTOFF.isoformat()})
                return Observation(status="ok", source="synthetic_news", snapshot_id=raw, symbol=symbol,
                    data={"articles": [{"title": "合成事件", "available_at": CUTOFF.isoformat(), "locator": "articles/0"}]})

        class Model:
            def __init__(self, role):
                self.role, self.calls = role, 0

            def complete(self, messages, tools, timeout):
                self.calls += 1
                if self.role == "news" and self.calls == 1:
                    return call_reply(("fetch", "get_stock_news", {"symbol": "600519.SH", "start": "2025-05-01", "end": "2025-05-08"}))
                if self.role == "news":
                    observation = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
                    if observation.get("error"):
                        raise AssertionError(str(observation["error"]))
                    return call_reply(("submit", "submit_analysis", {"content_status": "partial", "summary": "合成事件", "report_markdown": "合成事件",
                        "claims": [{"claim_id": "n1", "kind": "fact", "text": "合成事件", "evidence_refs": [
                            {"result_id": observation["result_id"], "snapshot_id": observation["snapshot_id"], "locator": "/data/articles/0/title"}]}],
                        "missing": ["宏观覆盖不足"]}))
                if self.role == "review":
                    return call_reply(("review", "submit_analysis", {"summary": "合成审阅", "issues": []}))
                return call_reply(("report", "submit_analysis", {"report_markdown": "综合判断：合成事件。需关注后续披露。", "claim_refs": ["news:n1"]}))

        coordinator = ResearchCoordinator(settings, model_factory=Model, service_factory=lambda: NewsService(settings))
        request = self.request().model_copy(update={"analysts": ["news"], "depth": "specialist"})
        state = coordinator.run(request)
        self.assertEqual(state["content"], "partial", state)
        self.assertEqual(state["budget"]["calls"]["model"], 4)
        self.assertEqual(state["budget"]["calls"]["tool"], 4)
        self.assertTrue(all(t["status"] == "completed" for t in state["tasks"]), state["tasks"])
        self.assertGreater(len(coordinator.store.grants(state["id"])), 1)
        self.assertIn("合成事件", Path(state["report"]).read_text(encoding="utf-8"))
