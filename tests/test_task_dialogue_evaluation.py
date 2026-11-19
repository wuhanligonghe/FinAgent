"""Cost caps and isolation for the dialogue evaluation runner."""
from datetime import datetime
import importlib
import json
from types import ModuleType
import sys
from copy import deepcopy
from unittest.mock import patch

from finagent.dialogue_evaluation import ModelBudget, evaluate, evaluation_clock, load_dialogues
from finagent.errors import AgentError
from finagent.models import ModelReply
from finagent.runtime import TZ

from .support import ServiceCase


class DialogueEvaluationTests(ServiceCase):
    def test_corpus_has_one_positive_research_and_multiple_turn_categories(self):
        corpus = load_dialogues()
        turns = [t for c in corpus["dialogues"] for t in c["turns"]]
        self.assertEqual(sum(t["expect"]["research_launches"] for t in turns), 1)
        self.assertEqual(sum(a.get("tool") == "start_deep_research" for t in turns for a in t["replay"]), 1)
        self.assertGreaterEqual(len(corpus["dialogues"]), 30)
        self.assertGreaterEqual(len({c["category"] for c in corpus["dialogues"]}), 10)

    def test_shared_model_cap_rejects_before_another_remote_request(self):
        class Model:
            calls = 0
            def complete(self, messages, tools, timeout):
                self.calls += 1
                return ModelReply({"role": "assistant", "content": "合成"}, usage={"prompt_tokens": 10})
        model = Model()
        budget = ModelBudget(2)
        first, second = budget.wrap(model), budget.wrap(model)
        first.complete([], [], 1)
        second.complete([], [], 1)
        with self.assertRaises(AgentError):
            first.complete([], [], 1)
        self.assertEqual(model.calls, 2)
        self.assertEqual(budget.usage["prompt_tokens"], 20)

    def test_clock_is_restored_including_new_module_aliases(self):
        from finagent import runtime
        original = runtime.now
        stamp = datetime(2025, 1, 1, tzinfo=TZ)
        module = ModuleType("finagent._eval_clock_test")
        try:
            with evaluation_clock(stamp):
                self.assertEqual(runtime.ExecutionContext().as_of, stamp)
                module.now = runtime.now
                sys.modules[module.__name__] = module
            self.assertIs(runtime.now, original)
            self.assertIs(module.now, original)
        finally:
            sys.modules.pop(module.__name__, None)

    def test_retired_frame_replay_fails_before_remote_calls_or_output(self):
        with patch("finagent.models.CompatibleChatModel.complete", side_effect=AssertionError("must not call model")):
            with self.assertRaisesRegex(ValueError, "已移除"):
                evaluate(self.settings, self.root / "retired", live=True)
        self.assertFalse((self.root / "retired").exists())
