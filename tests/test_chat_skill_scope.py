"""Chat skill scope and independent research compatibility; synthetic only."""
import json

from finagent.analysts.methods import ROLE_SKILLS
from finagent.config import HarnessSettings
from finagent.harness.host import SessionHost
from finagent.harness.kernel import Ledger
from finagent.models import ModelReply, ToolCall
from finagent.skill_library import SkillLibrary
from finagent.tools.perception import build_registry
from finagent.tools.skills import register_skill_tools
from finagent.research.role_prompts import ROLE_PROMPTS, role_method
from tests.support import ServiceCase, ScriptedModel


CHAT_METHODS = {
    "technical-analysis", "technical-divergence", "fundamentals-analysis",
    "financial-growth", "financial-margin", "market-sentiment",
    "news-events", "policy-impact",
    "deep-research",
}

RETIRED_ROLE_SKILLS = {
    "bull-thesis", "bear-thesis", "evidence-review", "quality-resolution",
    "research-arbitration", "trade-proposal", "risk-opportunity", "risk-downside",
    "risk-comparison", "portfolio-decision", "research-synthesis",
}


class ChatSkillScopeTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.settings.harness = HarnessSettings(
            workspace=self.root, memory_enabled=False, auto_memory=False)
        self.hosts = []

    def host(self, **kwargs):
        host = SessionHost(self.settings, model=ScriptedModel(ModelReply(
            {"role": "assistant", "content": "合成答复"})), service=self.service, **kwargs)
        self.hosts.append(host)
        return host

    def tearDown(self):
        for host in reversed(self.hosts):
            host.close()
        super().tearDown()

    def execute(self, host, tool, **args):
        return host.pool.execute(ToolCall("scope", tool, json.dumps(args)),
                                 host.ctx, Ledger(self.settings.limits))

    def test_catalog_and_both_loaders_exclude_all_workflow_methods(self):
        host = self.host()
        context = json.loads(host.model_messages([], host.ctx)[1]["content"])
        catalog = context["skills_catalog"]["available_skills"]
        self.assertEqual({row["name"] for row in catalog}, CHAT_METHODS)
        self.assertEqual(set(SkillLibrary().skills), CHAT_METHODS)
        for name in RETIRED_ROLE_SKILLS:
            with self.subTest(name=name):
                self.assertEqual(self.execute(host, "load_skill", name=name)["error"], "SKILL_DENIED")
                self.assertEqual(self.execute(host, "read_skill", name=name)["error"]["code"], "SKILL_DENIED")
                # A loaded flag left by an old session cannot bypass the view.
                host.ctx.loaded_skills[name] = "old-fingerprint"
                denied = self.execute(host, "read_skill_resource", name=name,
                                      path="references/example.md")
                self.assertEqual(denied["error"]["code"], "SKILL_DENIED")

    def test_all_chat_methods_and_financial_reference_still_load(self):
        host = self.host()
        for name in CHAT_METHODS:
            with self.subTest(name=name):
                self.assertTrue(self.execute(host, "load_skill", name=name)["instructions"])
                self.assertEqual(self.execute(host, "read_skill", name=name)["status"], "ok")
        value = self.execute(host, "read_skill_resource", name="financial-growth",
                             path="references/comparability.md")
        self.assertEqual(value["status"], "ok")
        self.assertTrue(value["data"]["content"])

    def test_resume_clears_obsolete_loaded_flags_but_keeps_chat_methods(self):
        host = self.host()
        self.execute(host, "load_skill", name="financial-growth")
        fingerprint = host.ctx.loaded_skills["financial-growth"]
        host.ctx.loaded_skills["bull-thesis"] = "old-fingerprint"
        host.checkpoint()
        host.close()
        restored = self.host()
        self.assertEqual(restored.ctx.loaded_skills, {"financial-growth": fingerprint})
        self.assertEqual(restored.state["loaded_skills"], restored.ctx.loaded_skills)
        self.assertEqual(self.execute(restored, "load_skill", name="bull-thesis")["error"], "SKILL_DENIED")

    def test_research_registries_still_load_every_role_method(self):
        host = self.host()
        self.assertEqual(len(host.finance.skill_library.skills), 9)
        for role, names in ROLE_SKILLS.items():
            with self.subTest(role=role):
                registry = build_registry(self.service)
                register_skill_tools(registry, names)
                self.assertEqual(set(registry.skill_library.skills), set(names))
                for name in names:
                    self.assertEqual(registry.execute("read_skill", {"name": name}, host.ctx).status, "ok")
                self.assertNotIn("deep-research", registry.skill_library.skills)
        self.assertEqual(len(ROLE_PROMPTS), 11)
        for role in ROLE_PROMPTS:
            self.assertTrue(role_method(role))
        self.assertEqual(set(host.finance.skill_library.skills), CHAT_METHODS)

    def test_general_child_uses_same_chat_scope(self):
        parent = self.host()
        child = self.host(session_id="skill-child", parent=parent)
        self.assertEqual(set(child.finance.skill_library.skills), CHAT_METHODS)
        self.assertEqual(self.execute(child, "load_skill", name="portfolio-decision")["error"], "SKILL_DENIED")
