"""Action/domain contracts, bounded repair and accounting; synthetic, no model accuracy claims."""
import json
import unittest
from unittest.mock import patch

import httpx

from finagent.config import IntentSettings
from finagent.errors import AgentError
from finagent.intent_routing import (
    Classification, IntentRouter, QwenIntentClassifier, routing_context,
)
from finagent.harness.host import SessionHost
from finagent.harness.kernel import Ledger
from finagent.models import ModelReply
from tests.support import ServiceCase, ScriptedModel
from tests.test_intent_routing import mock_classifier


def body(payload, **changes):
    result = {'model':'qwen3.5:9b','done':True,'done_reason':'stop',
              'message':{'role':'assistant','content':json.dumps(payload)},
              'prompt_eval_count':100,'eval_count':20}
    result.update(changes)
    return result


def decision(action='query', domains=None):
    return {'action':action,'data_domains':['market'] if domains is None else domains,'confidence':0.95}


class RoutingContractTests(unittest.TestCase):
    def router(self, responses, *, callback=None):
        requests = []
        def respond(request):
            requests.append(request)
            if callback:
                callback(request)
            return httpx.Response(200,json=responses[min(len(requests)-1,len(responses)-1)])
        settings=IntentSettings()
        return IntentRouter(settings,classifier=QwenIntentClassifier(settings,transport=httpx.MockTransport(respond))),requests

    def test_host_mapping_makes_missing_action_tools_impossible(self):
        for action, groups in [('research_start',['research']),('research_followup',['research']),
                               ('memory_write',['memory']),('automation',['automation']),
                               ('workspace',['workspace']),('extensions',['extensions']),('task_control',[])]:
            with self.subTest(action=action):
                router,requests=self.router([body(decision(action,[]))])
                route=router.route('请处理这项请求')
                self.assertEqual(route.tool_groups,groups)
                self.assertEqual(route.action,action)
                self.assertEqual(len(requests),1)
                self.assertEqual(set(json.loads(requests[0].content)['format']['properties']),
                                 {'action','data_domains','confidence'})

    def test_cross_fields_and_duplicate_domains_rejected(self):
        for value in [decision('query',[]),decision('research_start',['fundamentals']),
                      decision('conversation',['market']),decision('query',['news','news'])]:
            with self.assertRaises(ValueError):
                Classification.model_validate(value)

    def test_targeted_retry_is_charged_and_both_attempts_count_usage(self):
        router,requests=self.router([body(decision('query',[])),body(decision('memory_write',[]))])
        charges=[]
        route=router.route('记住我偏好简短中文',before_model=lambda:charges.append(1))
        self.assertEqual(route.source,'qwen')
        self.assertEqual(route.tool_groups,['memory'])
        self.assertEqual(route.model_calls,len(charges))
        self.assertEqual(route.model_calls,2)
        self.assertEqual(route.usage,{'prompt_tokens':200,'completion_tokens':40})
        self.assertEqual(route.diagnostics[0]['reason'],'DATA_ACTION_REQUIRES_DOMAIN')
        second=json.loads(requests[1].content)['messages'][1]['content']
        self.assertIn('DATA_ACTION_REQUIRES_DOMAIN',second)
        self.assertNotIn('ValueError',second)  # only whitelisted host codes

    def test_only_one_retry_then_diagnostic_fallback_with_usage(self):
        invalid={'action':'memory_write','data_domains':[],'confidence':'not numeric','secret_parameter':'untrusted'}
        router,requests=self.router([body(invalid)])
        route=router.route('请记住我的展示偏好')
        self.assertEqual(len(requests),2)
        self.assertEqual(route.source,'fallback')
        self.assertEqual(route.model_calls,2)
        self.assertEqual(route.usage['prompt_tokens'],200)
        self.assertEqual(len(route.diagnostics),2)
        self.assertNotIn('secret_parameter',json.dumps(route.diagnostics))

    def test_budget_exhaustion_prevents_retry(self):
        router,requests=self.router([body(decision('query',[])),body(decision())])
        charges=[]
        def charge():
            if charges:
                raise AgentError('MODEL_BUDGET','used')
            charges.append(1)
        route=router.route('茅台报价',before_model=charge)
        self.assertEqual(len(requests),1)
        self.assertEqual(route.model_calls,1)
        self.assertEqual(route.fallback_reason,'MODEL_BUDGET')
        self.assertEqual(route.usage['prompt_tokens'],100)

    def test_shared_deadline_shrinks_second_http_timeout(self):
        now=[0.0]
        def advance(request):now[0]+=3
        router,requests=self.router([body(decision('query',[])),body(decision())],callback=advance)
        with patch('finagent.intent_routing.monotonic',side_effect=lambda:now[0]):
            route=router.route('茅台报价',timeout=5)
        self.assertEqual([request.extensions['timeout']['read'] for request in requests],[5,2])
        self.assertEqual(route.fallback_reason,'INTENT_TIMEOUT')  # late successful body is rejected
        self.assertEqual(route.model_calls,2)
        self.assertEqual(route.usage['completion_tokens'],40)

    def test_exhausted_deadline_does_not_retry_or_charge(self):
        now=[0.0]
        router,requests=self.router([body(decision('query',[]))],callback=lambda _:now.__setitem__(0,6))
        charges=[]
        with patch('finagent.intent_routing.monotonic',side_effect=lambda:now[0]):
            route=router.route('股价',timeout=5,before_model=lambda:charges.append(1))
        self.assertEqual(len(requests),1)
        self.assertEqual(len(charges),1)
        self.assertEqual(route.fallback_reason,'INTENT_TIMEOUT')

    def test_wrong_model_and_protocol_never_repaired(self):
        for changes,reason in [({'model':'other'},'MODEL_MISMATCH'),({'done':False},'INCOMPLETE_ENVELOPE'),
             ({'message':{'role':'assistant','content':'{}','tool_calls':[{}]}},'MESSAGE_PROTOCOL_INVALID')]:
            router,requests=self.router([body(decision(),**changes)])
            route=router.route('股价')
            self.assertEqual(len(requests),1)
            self.assertEqual(route.diagnostics[0]['reason'],reason)

    def test_json_and_truncation_are_repairable(self):
        for changes in [{'done_reason':'length'}, {'message':{'role':'assistant','content':'{bad'}}]:
            router,requests=self.router([body(decision(),**changes),body(decision())])
            self.assertEqual(router.route('股价').source,'qwen')
            self.assertEqual(len(requests),2)

    def test_routing_uses_prose_history_without_private_runtime_state(self):
        state={"todos":[{"content":"PRIVATE", "status":"pending"}]}
        context=routing_context([{"role":"assistant","content":"runtime","_finagent_origin":"runtime_context"},
                                 {"role":"tool","content":"PRIVATE"},
                                 {"role":"user","content":"分析基本面"}], state=state)
        self.assertEqual(context["history"], [{"role":"user","content":"分析基本面"}])
        self.assertNotIn("active_task",context)
        self.assertNotIn("PRIVATE",json.dumps(context))


class RoutingHostV2Tests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.settings.intent.enabled = True
        self.settings.harness.memory_enabled = False
        self.classifier, self.seen = mock_classifier()
        self.hosts = []

    def host(self):
        host = SessionHost(self.settings, "route_v2", service=self.service,
            model=ScriptedModel(ModelReply({"role":"assistant","content":"测试"})),
            intent_classifier=self.classifier)
        self.hosts.append(host)
        return host

    def tearDown(self):
        for host in self.hosts:
            host.close()
        super().tearDown()

    def test_routing_does_not_modify_todos_or_expose_them_to_classifier(self):
        host=self.host()
        host.state["todos"]=[{"content":"分析基本面", "status":"in_progress"}]
        host.active_ledger=Ledger(host.settings.limits)
        host._route_tools("先暂停这个分析")
        context=json.loads(self.seen[0]["messages"][1]["content"])["context"]
        self.assertNotIn("active_task", context)
        self.assertEqual(host.state["todos"][0]["status"],"in_progress")

    def test_failed_attempt_usage_and_calls_are_recorded_on_host(self):
        settings=IntentSettings()
        host=self.host()
        host.intent_router=IntentRouter(settings,classifier=QwenIntentClassifier(settings,
            transport=httpx.MockTransport(lambda _:httpx.Response(200,json=body(decision('query',[]))))))
        host.submit('查询股价')
        self.assertEqual(host.state['last_ledger']['operations']['model'],3)  # 2 classifier + 1 main
        self.assertEqual(host.state['usage']['intent']['prompt_tokens'],200)
