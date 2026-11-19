"""Research failures retain the original model error and persisted evidence."""
import json
from threading import Event

import httpx

from finagent.errors import AgentError
from finagent.models import CompatibleChatModel, ModelSettings
from finagent.research.budget import ResearchBudget
from finagent.research.contracts import ResearchRequest
from finagent.research.coordinator import ResearchCoordinator
from finagent.research.decision_contracts import DecisionScope
from finagent.research.decision_runner import DecisionRunner
from finagent.research.runner import AnalystRunner
from finagent.research.store import ResearchStore
from finagent.runtime import now
from tests.support import ServiceCase, ScriptedModel, final_reply


class ResearchFailureTests(ServiceCase):
    def runner(self, cls, model):
        scope = DecisionScope(goal="SYNTHETIC", symbol="600519.SH", as_of=now())
        database = ResearchStore(self.root)
        run = database.create(scope, self.settings.decision, "synthetic")
        owner = database.acquire(run, "synthetic")
        budget = ResearchBudget(database, run, owner, self.settings.decision, Event())
        return cls(self.settings, database, budget, scope,
                   model_factory=lambda role: model, service_factory=lambda: self.service)

    def timeout_model(self):
        def timeout(request):
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return CompatibleChatModel(ModelSettings(base_url="http://localhost/v1", name="synthetic"),
                                   client=httpx.Client(transport=httpx.MockTransport(timeout)))

    def test_model_timeout_survives_analyst_review_and_decision_handoffs(self):
        for cls, role, task in [(AnalystRunner, "news", "news"),
                                (AnalystRunner, "review", "review"),
                                (DecisionRunner, "bull", "bull_1")]:
            with self.subTest(role=role):
                runner = self.runner(cls, self.timeout_model())
                with self.assertRaises(AgentError) as caught:
                    runner.run(role, task, 1, artifacts=[])
                self.assertEqual(caught.exception.code, "MODEL_UNAVAILABLE")
                self.assertTrue(caught.exception.retryable)
                self.assertIn("ReadTimeout", caught.exception.message)
                path = next(runner.store.directory(runner.budget.run).glob("tasks/*/1/loop/*/result.json"))
                saved = json.loads(path.read_text())
                self.assertEqual(saved["error_code"], "MODEL_UNAVAILABLE")
                self.assertEqual(runner.store.usage(runner.budget.run)["calls"]["model"], 3)

    def test_plain_answer_without_submission_still_fails(self):
        runner = self.runner(AnalystRunner, ScriptedModel(final_reply(answer="未通过交接的文字")))
        with self.assertRaises(AgentError) as caught:
            runner.run("news", "news", 1)
        self.assertEqual(caught.exception.code, "SUBMISSION_MISSING")

    def test_research_report_exposes_timeout_instead_of_missing_submission(self):
        coordinator = ResearchCoordinator(self.settings,
            model_factory=lambda role: self.timeout_model(), service_factory=lambda: self.service)
        progress = []
        state = coordinator.run(ResearchRequest(goal="SYNTHETIC", symbol="600519.SH",
            analysts=["news"], depth="specialist", as_of=now()), progress=progress.append)
        self.assertEqual(state["tasks"][0]["reason"], "MODEL_UNAVAILABLE")
        report = coordinator.store.directory(state["id"]) / "report.md"
        self.assertIn("ReadTimeout", report.read_text())
        self.assertTrue(any("ReadTimeout" in line for line in progress))
        self.assertNotIn("SUBMISSION_MISSING", report.read_text())
