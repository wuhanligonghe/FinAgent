"""Evaluator correctness tests; never masquerade as model acceptance results."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from finagent import acceptance as a
from finagent.config import Settings
from finagent.intent_routing import GROUPS, ToolRoute
from finagent.models import ModelReply
from tests.support import ScriptedModel


SOURCE = Path("evaluation/acceptance_v1")


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=self.root/"data")
        self.bundle = self.root/"bundle"
        a.prepare(SOURCE,self.bundle,self.settings)

    def tearDown(self):
        self.temp.cleanup()

    def test_coverage_and_family_split(self):
        report = a.validate(SOURCE)
        self.assertEqual(report["dialogues"],120)
        self.assertEqual(report["turns"],146)
        cases = a.jsonl(SOURCE/"inputs.jsonl")
        gold = a.jsonl(SOURCE/"gold.jsonl")
        groups={g for c in gold for t in c["turns"] for g in t["route_required"]}
        self.assertEqual(groups, a.FROZEN_ROUTE_GROUPS)
        self.assertTrue(any(len(c["turns"])>=3 for c in cases))
        self.assertEqual(report["annotation_status"],"pending_independent_review")

    def test_freeze_has_no_credentials_and_is_not_approved(self):
        manifest=a.verify(self.bundle)
        self.assertNotIn("api_key",json.dumps(manifest))
        review=a.read(self.bundle/"label_review.json")
        self.assertFalse(review["independent_of_authoring"])
        self.assertTrue(all(c["decision"]=="pending" for c in review["cases"]))
        with self.assertRaises(ValueError):
            a.require_label_review(self.bundle,self.bundle/"label_review.json",["a001"])

    def test_tampered_data_or_source_fails_verification(self):
        path=self.bundle/"gold.jsonl"
        path.write_text(path.read_text()+" ",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"摘要"):
            a.verify(self.bundle)

    def test_unknown_gold_and_cross_split_duplicates_are_rejected(self):
        rows=a.jsonl(self.bundle/"gold.jsonl")
        rows[0]["turns"][0]["route_required"]=["secret_tool"]
        (self.bundle/"gold.jsonl").write_text("\n".join(json.dumps(r) for r in rows),encoding="utf-8")
        with self.assertRaises(ValueError):a.validate(self.bundle)

    def test_all_groups_and_fallback_do_not_get_perfect_score(self):
        expected=[{"route_required":["market"],"route_optional":[]},
                  {"route_required":[],"route_optional":[]}]
        broad=[{"source":"qwen","tool_groups":list(GROUPS)}]*2
        score=a.route_metrics(broad,expected)
        self.assertEqual(score["exact_match"],0)
        self.assertLess(score["macro_f1_supported"],1)
        fallback=[{"source":"fallback","tool_groups":["market"]},None]
        self.assertEqual(a.route_metrics(fallback,expected)["exact_match"],0)
        self.assertEqual(a.route_metrics(fallback,expected)["fallback_or_missing_rate"],1)

    def test_optional_tools_are_not_required_or_false_positive(self):
        gold=[{"route_required":["fundamentals"],"route_optional":["documents"]}]
        for groups in (["fundamentals"],["fundamentals","documents"]):
            self.assertEqual(a.route_metrics([{"source":"qwen","tool_groups":groups}],gold)["exact_match"],1)

    def test_missing_rows_stay_in_denominator(self):
        gold=[{"route_required":["news"],"route_optional":[]}]*2
        score=a.route_metrics([{"source":"qwen","tool_groups":["news"]},None],gold)
        self.assertEqual(score["exact_match"],0.5)
        self.assertEqual(score["per_class"]["news"]["recall"],0.5)

    def test_acceptance_without_review_is_blocked_before_inference(self):
        with self.assertRaisesRegex(ValueError,"审核"):
            a.run(self.bundle,self.root/"out",self.settings,split="acceptance")
        self.assertFalse((self.root/"out").exists())

    def test_config_drift_is_detected(self):
        config=self.settings.model_copy(deep=True)
        config.model.max_output_tokens+=1
        with self.assertRaisesRegex(ValueError,"配置"):
            a.run(self.bundle,self.root/"out",config)
        config=self.settings.model_copy(deep=True)
        config.fact_check.enabled=False
        with self.assertRaisesRegex(ValueError,"配置"):
            a.run(self.bundle,self.root/"out",config)
        config=self.settings.model_copy(deep=True)
        config.security.enabled=False
        with self.assertRaisesRegex(ValueError,"配置"):
            a.run(self.bundle,self.root/"out",config)

    def test_retired_frame_chain_is_rejected_before_models_or_output(self):
        from unittest.mock import Mock
        factory=Mock(side_effect=AssertionError("must not call model"))
        with self.assertRaisesRegex(ValueError,"已移除"):
            a.run(self.bundle,self.root/"chain",self.settings,mode="chain",case_ids=["a001"],model_factory=factory)
        factory.assert_not_called()
        self.assertFalse((self.root/"chain").exists())

    def test_candidate_cannot_be_relabelled_as_final_acceptance(self):
        class Router:
            def __init__(self,*args):pass
            def route(self,*args,**kwargs):
                return ToolRoute(intent="query",tool_groups=["market"],confidence=1,source="qwen")
        a.run(self.bundle,self.root/"candidate",self.settings,mode="route",split="acceptance",candidate=True,
              case_ids=["a014"],router_factory=Router)
        with self.assertRaisesRegex(ValueError,"候选"):
            a.adjudicate(self.bundle,self.root/"candidate")

    def test_closed_world_financial_sources(self):
        import httpx
        data=a.read(self.bundle/"fixtures.json")
        respond=a.frozen_http(data)
        response=respond(httpx.Request("GET","https://unknown.example/anything"))
        self.assertEqual(response.status_code,503)
        search=respond(httpx.Request("GET","https://searchapi.eastmoney.com/api/suggest/get?input=宁德时代"))
        self.assertEqual(search.json()["QuotationCodeTable"]["Data"][0]["Code"],"300750")
        bars=respond(httpx.Request("GET","https://money.finance.sina.com.cn/CN_MarketData.getKLineData"))
        self.assertTrue(all(b["day"]<="2026-11-02" for b in bars.json()))
        self.assertEqual(bars.json()[-1]["close"],"9")

    def test_classifier_calls_share_global_hard_budget(self):
        import httpx
        from finagent.intent_routing import QwenIntentClassifier
        requests=[]
        def respond(request):
            requests.append(request)
            return httpx.Response(200,json={"model":"qwen3.5:9b","done":True,"done_reason":"stop",
                "message":{"role":"assistant","content":json.dumps({"action":"query","data_domains":["market"],"confidence":1})}})
        with patch("finagent.acceptance.QwenIntentClassifier",side_effect=lambda config:
                   QwenIntentClassifier(config,transport=httpx.MockTransport(respond))):
            report=a.run(self.bundle,self.root/"budget",self.settings,case_ids=["a007","a013"],max_calls=1)
        self.assertEqual(len(requests),1)
        self.assertEqual(report["model_calls"],1)
        self.assertEqual(report["turns_run"],2)
        self.assertEqual(report["route"]["fallback_or_missing_rate"],0.5)

    def test_initial_route_survives_agent_expansion(self):
        class Router:
            def route(self,*args,**kwargs):
                return ToolRoute(intent="query",tool_groups=["market"],confidence=1,source="qwen")
        router=a.CapturedRouter(Router())
        result=router.route("test")
        result.tool_groups.append("news")
        self.assertEqual(router.initial["tool_groups"],["market"])


if __name__=="__main__":unittest.main()
