"""Offline v2.2 entry/authority/actual stage-execution regression cases."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from finagent.errors import AgentError
from finagent.nlu.contracts import NLUResult
from finagent.nlu.parser import semantic_route
from finagent.research.selection import select, SelectedScope, authorize_resume, config_for
from finagent.research.coordinator import ResearchCoordinator
from finagent.research.menu import menu
from finagent.research.store import ResearchStore
from finagent.research.workflow import plan
from tests.support import ServiceCase
from tests.research21_support import tracker, SyntheticRunner
from tests.test_research_v21 import CUTOFF


class V22Tests(ServiceCase):
    def selected(self, mode="deep_research", **kw):
        return select(self.settings, mode=mode, symbol="600519.SH", source="cli_flag", as_of=CUTOFF, **kw)

    def coordinator(self, t):
        return ResearchCoordinator(self.settings, runner_factory=lambda *a: SyntheticRunner(t, *a))

    def result(self, **kw):
        return NLUResult(model_id="fixture", bundle_sha256="a"*64, task_type="stock_analysis", **kw)

    def test_chat_news_never_escalates(self):
        r=self.result(dimension_state={k:"include" if k=="news" else "unspecified" for k in ("news","technical","policy","sentiment","fundamentals")})
        self.assertEqual(semantic_route(r), ("specialist", ["news"], "research"))

    def test_chat_multiple_dimensions_stay_specialist(self):
        r=self.result(dimension_state={k:"include" if k in ("fundamentals","policy") else "exclude" for k in ("news","technical","policy","sentiment","fundamentals")})
        self.assertEqual(semantic_route(r), ("specialist", ["fundamentals","policy"], "research"))

    def test_chat_unspecified_dimensions_clarify(self):
        with self.assertRaisesRegex(AgentError,"NEEDS_CLARIFICATION"):
            semantic_route(self.result())

    def test_concept_has_no_research_route(self):
        r=self.result().model_copy(update={"task_type":"concept_qa"})
        self.assertEqual(semantic_route(r),("direct",[],None))

    def test_model_cannot_provide_mode_or_removed_heads(self):
        for fields in ({"execution_mode":"full_decision"},{"depth":"deep"},{"workflow_preference":"full_decision"}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                self.result(**fields)

    def test_menu_cancel_does_not_create_selection_or_run(self):
        with patch("finagent.nlu.qwen3.get_parser",side_effect=AssertionError("no model")):
            self.assertIsNone(menu(self.settings,read=lambda _:"0",write=lambda _:None))
        with ResearchStore(self.root).connect() as db:
            self.assertEqual(db.count('research_runs'),0)

    def test_menu_explicit_begin_works_without_nlu(self):
        answers=iter(["1","600519.SH","news fundamentals","","","开始"])
        with patch("finagent.nlu.qwen3.get_parser",side_effect=AssertionError("no model")):
            req=menu(self.settings,read=lambda _:next(answers),write=lambda _:None,as_of=CUTOFF)
        self.assertEqual((req.execution_mode,req.selection_source,req.analysts),("deep_research","terminal_menu",["news","fundamentals"]))

    def test_deep_stops_at_manager_with_truthful_complete_report(self):
        from finagent.config import NLUSettings
        self.settings.nlu = NLUSettings(model_dir=self.root/'missing-nlu-model')
        req=self.selected();t=tracker();scope=SelectedScope(**req.model_dump())
        self.assertEqual(plan(scope)[-1]['role'],'research_manager')
        self.assertLess(config_for(scope,self.settings).model_calls,self.settings.decision.model_calls)
        with patch('finagent.nlu.qwen3.get_parser',side_effect=AssertionError('no NLU')):
            state=self.coordinator(t).run(req)
        report=json.loads(Path(state['report']).with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual(report['content_status'],'complete', report['errors'])
        self.assertEqual(t['calls'][-1],'research_manager')
        self.assertFalse(any(x=='trader' or x=='portfolio_manager' or x.startswith('risk_') for x in t['calls']))
        self.assertIsNone(report['portfolio_decision'])
        self.assertEqual(report['execution_mode'],'deep_research')
        self.assertEqual(report['research_decision']['role'],'research_manager')
        self.assertEqual([x for x in t['calls'] if x.startswith(('bull_','bear_'))],['bull_1','bear_1','bull_2','bear_2'])

    def test_full_decision_executes_risks_and_portfolio(self):
        req=self.selected('full_decision');t=tracker();state=self.coordinator(t).run(req)
        self.assertEqual(state['content'],'complete')
        self.assertEqual(t['calls'][-1],'portfolio_manager')
        self.assertEqual(len([x for x in t['calls'] if x.startswith('risk_')]),6)

    def test_native_executor_deep_report_uses_manager_final_pool(self):
        from tests.research21_support import native_factory
        from finagent.research.workflow import access
        req=self.selected();scope=SelectedScope(**req.model_dump())
        self.assertEqual(access('research_manager',scope),'final')
        state=ResearchCoordinator(self.settings,runner_factory=native_factory(self.settings)).run(req)
        report=json.loads(Path(state['report']).with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual(report['content_status'],'complete',report['errors'])
        self.assertEqual(report['research_decision']['role'],'research_manager')

    def test_forged_selection_and_scope_change_rejected_before_run(self):
        req=self.selected();c=self.coordinator(tracker())
        for changes in ({'selection_id':'f'*32},{'symbol':'002298.SZ'},{'execution_mode':'full_decision','risk_rounds':2}):
            with self.subTest(changes=changes),self.assertRaisesRegex(AgentError,'RESEARCH_DENIED'):
                c.run(req.model_copy(update=changes))
        with c.store.connect() as db:
            self.assertEqual(db.count('research_runs'),0)

    def test_selection_single_use(self):
        req=self.selected();c=self.coordinator(tracker());c.run(req)
        with self.assertRaisesRegex(AgentError,'RESEARCH_DENIED'):
            c.run(req)

    def test_expired_selection_is_not_executable(self):
        req=self.selected();store=ResearchStore(self.root)
        with store.transaction() as db:
            db.change("research_execution_selections", {"created":"2000-01-01T00:00:00+08:00"})
        with self.assertRaisesRegex(AgentError,'RESEARCH_DENIED'):
            self.coordinator(tracker()).run(req)

    def test_legacy_without_authorization_is_read_only(self):
        from finagent.research.decision_contracts import DecisionScope
        scope=DecisionScope(goal='legacy',symbol='600519.SH',as_of=CUTOFF)
        store=ResearchStore(self.root);run=store.create(scope,self.settings.decision,'legacy')
        with self.assertRaisesRegex(AgentError,'LEGACY_SELECTION_REQUIRED'):
            authorize_resume(store,run)
        self.assertEqual(json.loads(store.get(run)['scope'])['scope_hash'],scope.scope_hash)

    def test_authorized_resume_references_frozen_scope(self):
        state=self.coordinator(tracker()).run(self.selected());store=ResearchStore(self.root)
        before=store.get(state['id'])['scope'];authorize_resume(store,state['id'])
        self.assertEqual(store.get(state['id'])['scope'],before)
        self.assertEqual(store.events(state['id'])[-1]['kind'],'explicit_resume')

    def test_deep_rejects_risk_configuration(self):
        with self.assertRaisesRegex(AgentError,'INVALID_RESEARCH_MODE'):
            self.selected(risk_rounds=2)

    def test_cli_requires_explicit_mode_without_calls(self):
        from finagent.cli import main
        with patch('finagent.cli.load_settings',return_value=self.settings),patch('sys.stdin.isatty',return_value=False),patch('finagent.research.coordinator.ResearchCoordinator.run',side_effect=AssertionError('no research')),redirect_stdout(StringIO()),redirect_stderr(StringIO()):
            self.assertEqual(main(['research','--symbol','600519.SH']),2)
            self.assertEqual(main(['research','--mode','full_decision','--depth','deep','--symbol','600519.SH']),2)

    def test_tool_cannot_upgrade_legacy_context(self):
        from finagent.tools.delegation import register_delegation
        from finagent.tools.registry import ToolRegistry
        from finagent.runtime import ExecutionContext
        registry=ToolRegistry();register_delegation(registry,self.service)
        with patch('finagent.research.coordinator.ResearchCoordinator.run',side_effect=AssertionError('no run')):
            value=registry.execute('start_stock_research',{'goal':'深入研究','depth':'deep','pipeline':'trading'},ExecutionContext(symbol='600519.SH'))
        self.assertEqual(value.error.code,'RESEARCH_DENIED')
