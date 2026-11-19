"""Offline Agent Skills discovery, lazy loading, permissions and native loops."""
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from finagent.agent import AgentLoop, RunRequest
from finagent.analysts.catalog import CATALOG, prompt as analyst_prompt
from finagent.analysts.decision import prompt as decision_prompt
from finagent.analysts.methods import ROLE_SKILLS, CHAT_SKILLS
from finagent.errors import AgentError
from finagent.research.budget import ResearchBudget
from finagent.research.contracts import ResearchScope
from finagent.research.decision_contracts import DecisionScope
from finagent.research.decision_runner import DecisionRunner
from finagent.research.runner import AnalystRunner
from finagent.research.role_prompts import ROLE_PROMPTS, role_method
from finagent.research.store import ResearchStore
from finagent.runtime import ExecutionContext, TZ
from finagent.session import Conversation, restore_context
from finagent.skill_library import MAX_FILE, SkillLibrary
from finagent.tools.registry import ToolRegistry
from finagent.tools.skills import register_skill_tools
from tests.support import ServiceCase, ScriptedModel, call_reply


CUTOFF = datetime(2025, 5, 8, 23, 59, 59, tzinfo=TZ)


def skill_catalog(messages):
    for message in messages:
        try:
            value = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and "available_skills" in value:
            return value["available_skills"]
    raise AssertionError("没有发现技能目录")


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name="sample", header="name: sample\ndescription: Sample workflow.", body="Private method body."):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        (directory / "SKILL.md").write_text(f"---\n{header}\n---\n{body}")
        return directory

    def test_discovery_reads_only_frontmatter(self):
        self.write()

        class HeaderOnly(io.StringIO):
            def readline(self, *args):
                if self.tell() >= len("---\nname: sample\ndescription: Sample workflow.\n---\n"):
                    raise AssertionError("发现阶段读取了正文")
                return super().readline(*args)

            def read(self, *args):
                raise AssertionError("发现阶段读取了正文")

        with patch.object(Path, "open", return_value=HeaderOnly("---\nname: sample\ndescription: Sample workflow.\n---\nPrivate method body.")):
            library = SkillLibrary(self.root)
        catalog = library.catalog_message()
        self.assertNotIn("Private method body", catalog)
        self.assertEqual(library.catalog()[0]["name"], "sample")

    def test_invalid_metadata_is_rejected(self):
        for header in ("description: Missing name", "name: sample", "name: Sample\ndescription: test",
                       "name: sample\ndescription: ''", "name: sample\ndescription: [array]",
                       "name: sample\nname: sample\ndescription: duplicate", "name: sample\ndescription: !unsafe test"):
            with self.subTest(header=header):
                self.write(header=header)
                with self.assertRaisesRegex(AgentError, "INVALID_SKILL"):
                    SkillLibrary(self.root)

    def test_folded_yaml_and_optional_metadata(self):
        self.write(header="name: sample\ndescription: >-\n  Multi-line description\n  for discovery.\nmetadata:\n  version: '1.0'\nallowed-tools: imaginary_tool")
        library = SkillLibrary(self.root)
        self.assertEqual(library.catalog()[0]["description"], "Multi-line description for discovery.")
        registry = ToolRegistry()
        register_skill_tools(registry, library=library)
        self.assertNotIn("imaginary_tool", registry.tools)

    def test_name_must_match_directory_and_be_unique(self):
        self.write(name="different")
        with self.assertRaisesRegex(AgentError, "INVALID_SKILL"):
            SkillLibrary(self.root)
        (self.root / "different" / "SKILL.md").unlink()
        (self.root / "different").rmdir()
        self.write()
        self.write(name="z-duplicate")
        with self.assertRaisesRegex(AgentError, "DUPLICATE_SKILL"):
            SkillLibrary(self.root)

    def test_empty_or_oversized_body_fails_only_when_loaded(self):
        for body in (" ", "x" * (MAX_FILE + 1)):
            self.write(body=body)
            library = SkillLibrary(self.root)
            self.assertEqual(len(library.catalog()), 1)
            with self.assertRaisesRegex(AgentError, "INVALID_SKILL"):
                library.read("sample")

    def test_changed_metadata_cannot_be_loaded_under_old_description(self):
        self.write()
        library = SkillLibrary(self.root)
        self.write(header="name: sample\ndescription: Changed workflow.")
        with self.assertRaisesRegex(AgentError, "SKILL_CHANGED"):
            library.read("sample")

    def test_traversal_absolute_and_symlink_resources_are_denied(self):
        directory = self.write()
        (directory / "references").mkdir()
        secret = self.root / "outside.txt"
        secret.write_text("not a skill resource")
        (directory / "references" / "escape.txt").symlink_to(secret)
        library = SkillLibrary(self.root)
        for path in ("../outside.txt", str(secret), "references/../../outside.txt", "references\\escape.txt",
                     "references//escape.txt", "references/escape.txt", "references/./escape.txt"):
            with self.subTest(path=path), self.assertRaisesRegex(AgentError, "SKILL_PATH_DENIED"):
                library.read_resource("sample", path)

    def test_body_and_manifest_symlink_denied(self):
        directory = self.write()
        library = SkillLibrary(self.root)
        original = directory / "SKILL.md"
        external = self.root / "external.md"
        original.rename(external)
        original.symlink_to(external)
        with self.assertRaisesRegex(AgentError, "SKILL_PATH_DENIED"):
            library.read("sample")
        with self.assertRaisesRegex(AgentError, "SKILL_PATH_DENIED"):
            SkillLibrary(self.root)

    def test_general_methods_are_lazy_and_internal_role_methods_are_embedded(self):
        library = SkillLibrary()
        self.assertEqual(set(library.skills), set(CHAT_SKILLS))
        self.assertEqual(len(library.catalog()), 9)
        for role, names in ROLE_SKILLS.items():
            system = analyst_prompt(role)
            for name in names:
                self.assertNotIn(library.read(name)["instructions"], system)
        for role in set(ROLE_PROMPTS) - {"review", "synthesis"}:
            system = decision_prompt(role)
            self.assertIn(role_method(role), system)
            for other in set(ROLE_PROMPTS) - {role}:
                self.assertNotIn(role_method(other), system)

    def test_internal_method_loader_rejects_unknown_roles_and_missing_assets(self):
        for role in ("../../secret", "bull-thesis", "deep-research"):
            with self.subTest(role=role), self.assertRaisesRegex(AgentError, "UNKNOWN_RESEARCH_ROLE"):
                role_method(role)
        with patch("finagent.research.role_prompts.files") as resources:
            resources.return_value.joinpath.return_value.read_text.side_effect = FileNotFoundError()
            with self.assertRaisesRegex(AgentError, "ROLE_PROMPT_UNAVAILABLE"):
                role_method("bull")


class SkillToolsTests(ServiceCase):
    def test_role_filter_denies_hidden_skill_and_does_not_change_tools(self):
        register_skill_tools(self.registry, ROLE_SKILLS["fundamentals"])
        before = set(self.registry.tools)
        ctx = ExecutionContext()
        self.assertEqual(len(self.registry.skill_library.catalog()), 3)
        denied = self.registry.execute("read_skill", {"name": "risk-downside"}, ctx)
        self.assertEqual(denied.error.code, "SKILL_DENIED")
        result = self.registry.execute("read_skill", {"name": "financial-growth"}, ctx)
        self.assertEqual(result.status, "ok")
        self.assertEqual(set(self.registry.tools), before)
        self.assertFalse(ctx.source_requests)
        self.assertFalse(ctx.evidence_ids)
        self.assertIsNone(result.snapshot_id)

    def test_resources_require_activation_and_support_paging(self):
        register_skill_tools(self.registry, ("financial-growth",))
        ctx = ExecutionContext()
        args = {"name": "financial-growth", "path": "references/comparability.md", "offset": 10, "limit": 20}
        result = self.registry.execute("read_skill_resource", args, ctx)
        self.assertEqual(result.error.code, "SKILL_NOT_LOADED")
        self.registry.execute("read_skill", {"name": "financial-growth"}, ctx)
        result = self.registry.execute("read_skill_resource", args, ctx)
        self.assertEqual(len(result.data["content"]), 20)
        self.assertTrue(result.data["truncated"])
        self.assertNotIn("instructions", result.data)
        bad = self.registry.execute("read_skill_resource", dict(args, offset=-1), ctx)
        self.assertEqual(bad.error.code, "VALIDATION_ERROR")

    def test_skill_catalog_body_and_reference_arrive_in_separate_turns(self):
        register_skill_tools(self.registry, ("financial-growth",))
        instructions = self.registry.skill_library.read("financial-growth")["instructions"]
        reference = self.registry.skill_library.read_resource("financial-growth", "references/comparability.md")["content"]

        def choose(messages):
            self.assertEqual([s["name"] for s in skill_catalog(messages)], ["financial-growth"])
            initial = json.dumps(messages, ensure_ascii=False)
            self.assertNotIn(instructions, initial)
            self.assertNotIn(reference, initial)
            return call_reply(("load", "read_skill", {"name": "financial-growth"}))

        def details(messages):
            observation = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
            self.assertEqual(observation["data"]["instructions"], instructions)
            self.assertNotIn(reference, json.dumps(messages, ensure_ascii=False))
            return call_reply(("ref", "read_skill_resource", {"name": "financial-growth", "path": "references/comparability.md"}))

        def finish(messages):
            observation = json.loads(next(m["content"] for m in reversed(messages) if m["role"] == "tool"))
            self.assertEqual(observation["data"]["content"], reference)
            return self.answer()

        model = ScriptedModel(choose, details, finish)
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("说明可比性方法", require_data=False))
        self.assertEqual(result.status, "completed")
        self.assertEqual((result.model_calls, result.tool_calls, result.source_requests), (3, 2, 0))
        state = json.loads((result.run_dir / "state.json").read_text())
        self.assertIn("financial-growth", state["loaded_skills"])
        conversation = Conversation(self.store)
        conversation.remember(result)
        self.assertEqual(conversation.state["history"][-1]["user"], "说明可比性方法")

    @staticmethod
    def answer():
        from finagent.models import ModelReply
        return ModelReply({"role": "assistant", "content": "已核对方法。"})

    def test_loading_methods_does_not_satisfy_market_data_requirement(self):
        model = ScriptedModel(call_reply(("load", "read_skill", {"name": "financial-growth"})), self.answer())
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("数据研究", require_data=True))
        self.assertEqual(result.status, "partial")
        self.assertIn("DATA_UNAVAILABLE", result.reason)
        self.assertEqual(result.source_requests, 0)

    def test_loaded_skill_can_be_read_after_resume(self):
        ctx = ExecutionContext()
        ctx.limits.model_calls = 1
        model = ScriptedModel(call_reply(("load", "read_skill", {"name": "financial-growth"})))
        result = AgentLoop(model, self.registry, self.store).run(RunRequest("说明方法", require_data=False), ctx)
        state, restored = restore_context(result.run_id, self.store)
        self.assertEqual(restored.loaded_skills, state["loaded_skills"])
        resource = self.registry.execute("read_skill_resource", {"name": "financial-growth", "path": "references/comparability.md"}, restored)
        self.assertEqual(resource.status, "ok")

    def test_nlu_concept_is_advisory_for_methods_and_financial_data(self):
        ctx = ExecutionContext(nlu_admission={"task_type": "concept_qa"})
        result = self.registry.execute("read_skill", {"name": "financial-growth"}, ctx)
        self.assertEqual(result.status, "ok")
        quote = self.registry.execute("get_quote", {"symbol": "600519.SH"}, ctx)
        self.assertIn(quote.status, ("ok", "partial"))

    def budget(self, scope, config):
        database = ResearchStore(self.root)
        run = database.create(scope, config, "skills-test")
        owner = database.acquire(run, "skills-test")
        return database, ResearchBudget(database, run, owner, config, Event())

    def test_native_analyst_selects_method_before_submission(self):
        scope = ResearchScope(goal="研究基本面", symbol="600519.SH", as_of=CUTOFF, analysts=("fundamentals",))
        database, budget = self.budget(scope, self.settings.research)

        def choose(messages):
            self.assertEqual({s["name"] for s in skill_catalog(messages)}, set(ROLE_SKILLS["fundamentals"]))
            return call_reply(("method", "read_skill", {"name": "fundamentals-analysis"}))

        model = ScriptedModel(choose, call_reply(("submit", "submit_analysis", {
            "summary": "没有授权财务事实", "report_markdown": "资料不可用，保留缺口。", "claims": [],
            "content_status": "unavailable", "missing": ["缺少财务原文"], "limitations": []})))
        runner = AnalystRunner(self.settings, database, budget, scope, model_factory=lambda role: model, service_factory=lambda: self.service)
        result = runner.run("fundamentals", "fundamentals", 1)
        self.assertEqual(result["content_status"], "unavailable")
        self.assertEqual(model.index, 2)

    def test_native_decision_receives_method_and_submits_without_skill_read(self):
        scope = DecisionScope(goal="支持逻辑研究", symbol="600519.SH", as_of=CUTOFF)
        database, budget = self.budget(scope, self.settings.decision)

        def choose(messages):
            self.assertIn(role_method("bull"), messages[0]["content"])
            self.assertNotIn("available_skills", json.dumps(messages, ensure_ascii=False))
            return call_reply(("submit", "submit_analysis", {
                "side": "bull", "round": 1, "turn_index": 1, "arguments": [], "responses": [],
                "abstention_reason": "无支持依据"}))

        model = ScriptedModel(choose)
        runner = DecisionRunner(self.settings, database, budget, scope, model_factory=lambda role: model, service_factory=lambda: self.service)
        result = runner.run("bull", "bull_1", 1, bundle={})
        self.assertEqual(result["abstention_reason"], "无支持依据")
        self.assertEqual(model.index, 1)

    def test_native_review_and_legacy_synthesis_use_internal_methods(self):
        for role in ("review", "synthesis"):
            with self.subTest(role=role):
                scope = ResearchScope(goal="合成研究", symbol="600519.SH", as_of=CUTOFF)
                database, budget = self.budget(scope, self.settings.research)
                def choose(messages):
                    self.assertIn(role_method(role), messages[0]["content"])
                    self.assertNotIn("available_skills", json.dumps(messages, ensure_ascii=False))
                    body = ({"summary": "无问题", "issues": []} if role == "review" else
                            {"report_markdown": "合成主张", "claim_refs": ["technical:c"]})
                    return call_reply(("submit", "submit_analysis", body))
                model = ScriptedModel(choose)
                runner = AnalystRunner(self.settings, database, budget, scope,
                    model_factory=lambda _: model, service_factory=lambda: self.service)
                result = runner.run(role, role, 1, artifacts=[{
                    "task_id": "technical", "claims": [{"claim_id": "c", "text": "合成主张"}]}])
                self.assertEqual(model.index, 1)
                self.assertTrue(result)
