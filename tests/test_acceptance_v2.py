"""Versioned gold and evaluator accounting checks, not classifier accuracy."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from finagent import acceptance as a
from finagent.config import Settings
from finagent.intent_routing import GROUPS, QwenIntentClassifier
from finagent.models import ModelReply
from tests.support import ScriptedModel


SOURCE=Path('evaluation/acceptance_v2')


class AcceptanceV2Tests(unittest.TestCase):
    def test_current_taxonomy_and_lineage_leave_v1_unchanged(self):
        report=a.validate(SOURCE)
        self.assertEqual((report['dialogues'],report['turns']),(140,171))
        labels=a.jsonl(SOURCE/'gold.jsonl')
        groups={g for case in labels for turn in case['turns'] for g in turn['route_required']+turn['route_optional']}
        self.assertEqual(groups,set(GROUPS))
        self.assertNotIn('documents',groups)
        lineage=a.read(SOURCE/'lineage.json')
        for name,expected in lineage['parent_files'].items():
            self.assertEqual(a.digest(Path('evaluation/acceptance_v1')/name),expected)
        self.assertEqual(report['annotation_status'],'pending_independent_review')

    def test_action_score_keeps_missing_and_fallback_in_denominator(self):
        expected=[{'route_required':['research'],'route_optional':[],'route_actions':['research_start']}]*3
        rows=[{'source':'qwen','tool_groups':['research'],'action':'research_start'},
              {'source':'fallback','tool_groups':['research'],'action':'research_start'},None]
        report=a.route_metrics(rows,expected,groups=GROUPS)
        self.assertEqual(report['action_accuracy'],1/3)
        self.assertEqual(report['action_and_groups_match'],1/3)
        self.assertNotIn('documents',report['per_class'])
        rows[0]['action']='research_followup'
        self.assertEqual(a.route_metrics(rows,expected,groups=GROUPS)['action_accuracy'],0)

    def test_retired_group_or_unknown_action_in_v2_is_rejected(self):
        import shutil
        with TemporaryDirectory() as directory:
            target=Path(directory)/'dataset'
            shutil.copytree(SOURCE,target)
            labels=a.jsonl(target/'gold.jsonl')
            labels[0]['turns'][0]['route_required']=['documents']
            (target/'gold.jsonl').write_text('\n'.join(json.dumps(row) for row in labels))
            with self.assertRaisesRegex(ValueError,'标签'):a.validate(target)
            labels[0]['turns'][0]['route_required']=[]
            labels[0]['turns'][0]['route_actions']=['launch_everything']
            (target/'gold.jsonl').write_text('\n'.join(json.dumps(row) for row in labels))
            with self.assertRaisesRegex(ValueError,'动作'):a.validate(target)

    def run_case(self, root, *, budget=2, chain=False):
        settings=Settings(data_dir=root/'data')
        bundle=root/'bundle'
        a.prepare(SOURCE,bundle,settings)
        requests=[]
        def respond(request):
            requests.append(json.loads(request.content))
            payload={'action':'query','data_domains':[] if len(requests)==1 else ['market'],'confidence':0.95}
            return httpx.Response(200,json={'model':'qwen3.5:9b','done':True,'done_reason':'stop',
                'message':{'role':'assistant','content':json.dumps(payload)},'prompt_eval_count':100,'eval_count':20})
        with patch('finagent.acceptance.QwenIntentClassifier',side_effect=lambda config:
                QwenIntentClassifier(config,transport=httpx.MockTransport(respond))):
            result=a.run(bundle,root/'out',settings,case_ids=['a013'],max_calls=budget,
                mode='chain' if chain else 'route',
                model_factory=lambda _:ScriptedModel(ModelReply({'role':'assistant','content':'数据缺失'})))
        return result,requests

    def test_repair_logs_and_counts_each_http_request(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            result,requests=self.run_case(root)
            self.assertEqual(result['model_calls'],2)
            self.assertEqual(result['usage'],{'prompt_tokens':200,'completion_tokens':40})
            self.assertEqual(result['route']['exact_match'],1)
            self.assertEqual(result['route']['action_accuracy'],1)
            records=list((root/'out/requests').glob('intent_*.json'))
            self.assertEqual(len(records),2)
            self.assertEqual([a.read(p)['attempt'] for p in sorted(records)],[1,2])
            self.assertNotIn('route_required',json.dumps(requests))
            self.assertTrue((root/'bundle/lineage.json').exists())

    def test_global_budget_blocks_second_http_attempt(self):
        with TemporaryDirectory() as directory:
            result,requests=self.run_case(Path(directory),budget=1)
            self.assertEqual(len(requests),1)
            self.assertEqual(result['model_calls'],1)
            self.assertEqual(result['usage']['prompt_tokens'],100)
            self.assertEqual(result['route']['fallback_or_missing_rate'],1)

    def test_retired_chain_never_seeds_library_or_calls_models(self):
        with TemporaryDirectory() as directory, patch('finagent.acceptance.seed_fixture',side_effect=AssertionError('retired')):
            root=Path(directory)
            with self.assertRaisesRegex(ValueError,'已移除'):
                self.run_case(root,budget=4,chain=True)
            self.assertFalse((root/'out').exists())
