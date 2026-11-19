"""Adversarial rule and delivery-gate checks using synthetic source snapshots."""
from datetime import datetime
from decimal import Decimal
import json

from finagent.config import FactCheckSettings
from finagent.data.contracts import Observation
from finagent.fact_check import inspect_answer
from finagent.harness.host import SessionHost
from finagent.models import ModelReply
from finagent.runtime import ExecutionContext, TZ
from tests.support import ServiceCase, ScriptedModel, call_reply


class FactCheckTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.ctx=ExecutionContext()
        self.ctx.as_of=datetime(2026,11,3,12,0,tzinfo=TZ)
        self.ctx.symbol='600519.SH'
        self.state={}
        self.hosts=[]

    def tearDown(self):
        for host in self.hosts:host.close()
        super().tearDown()

    def source(self,tool='get_quote',symbol='600519.SH',data=None,**kwargs):
        raw=self.store.save_raw(b'synthetic facts',{'synthetic':True})
        kwargs.setdefault('currency','CNY')
        value=self.store.save_result(Observation(status='ok',source='synthetic',symbol=symbol,
            snapshot_id=raw,data=data or {'name':'贵州茅台','price':10,'previous_close':9,'volume_shares':200000},**kwargs))
        self.ctx.result_tools[value.result_id]=tool
        self.ctx.evidence_ids.update({raw,value.result_id})
        return value

    def check(self,text):return inspect_answer(text,self.ctx,self.store,self.state)

    def test_plain_quotes_and_markdown_tables(self):
        self.source()
        for text in ('贵州茅台报10.00元。','贵州茅台股价为10元，前收9元。',
                     '| 项目 | 数值 |\n|---|---|\n| 最新价 | 10.00元 |\n| 前收 | 9元 |',
                     '|公司|股价（元）|\n|---|---|\n|贵州茅台|10.00|'):
            with self.subTest(text=text):self.assertFalse(self.check(text)['issues'])
        for text in ('茅台报99元。','|项目|数值|\n|---|---|\n|股价|99元|',
                     '|公司|股价（元）|\n|---|---|\n|贵州茅台|99.00|'):
            self.assertTrue(self.check(text)['issues'])

    def test_matching_other_fields_or_other_entities_is_not_support(self):
        self.source()
        self.source(symbol='000858.SZ',data={'name':'五粮液','price':99,'previous_close':10})
        self.assertTrue(self.check('贵州茅台股价99元')['issues'])
        self.assertTrue(self.check('贵州茅台股价9元')['issues'])
        self.assertFalse(self.check('五粮液股价99元')['issues'])

    def test_multi_entity_header_binds_each_column(self):
        self.source()
        self.source(symbol='000858.SZ',data={'name':'五粮液','price':99})
        good='|指标|贵州茅台|五粮液|\n|---|---|---|\n|股价（元）|10|99|'
        bad=good.replace('|10|99|','|99|10|')
        self.assertFalse(self.check(good)['issues'])
        self.assertTrue(self.check(bad)['issues'])
        self.assertTrue(self.check('股价10元')['issues'])

    def test_units_precision_and_periods(self):
        self.source('get_financial_statements',data={'reports':[
            {'report_period':'2024-12-31','metrics':{'revenue':{'value':123450000},'net_profit':{'value':20}}},
            {'report_period':'2025-12-31','metrics':{'revenue':{'value':800000000}}}]})
        self.assertFalse(self.check('2024年营业收入1.23亿元')['issues'])
        self.assertFalse(self.check('2024年营业收入12,345万元')['issues'])
        self.assertTrue(self.check('2025年营业收入1.23亿元')['issues'])
        self.assertTrue(self.check('2024年营业收入12345元')['issues'])
        self.assertTrue(self.check('2024年净利润123450000元')['issues'])

    def test_date_and_time_roles_cannot_be_swapped(self):
        self.source(data={'name':'贵州茅台','price':10},data_time='2026-11-03T11:19:03+08:00',
                    fetched_at=datetime(2026,11,3,11,21,tzinfo=TZ))
        for text in ('行情时间2026-11-03 11:19','获取时间2026-11-03 11:21','数据时间11:19:03'):
            self.assertFalse(self.check(text)['issues'])
        for text in ('行情时间2026-11-03 11:21','行情时间11:21','数据时间2026-11-04 11:19'):
            self.assertTrue(self.check(text)['issues'])

    def test_entity_name_code_mismatch(self):
        self.source()
        self.assertTrue(self.check('宁德时代（600519.SH）')['issues'])
        self.assertTrue(self.check('宁德时代（600519）')['issues'])
        self.assertFalse(self.check('贵州茅台（600519.SH）')['issues'])

    def test_no_evidence_stale_after_cutoff_or_unauthorized_snapshots_do_not_support_facts(self):
        self.assertTrue(self.check('股价10元')['issues'])
        value=self.source()
        self.ctx.invalidated_result_ids.add(value.result_id)
        self.assertTrue(self.check('股价10元')['issues'])
        self.ctx.invalidated_result_ids.clear()
        self.ctx.evidence_ids.clear()
        self.assertTrue(self.check('股价10元')['issues'])
        self.ctx.evidence_ids.add(value.snapshot_id)
        self.ctx.result_tools.clear()
        self.source(data={'price':10},data_time='2026-11-04T10:00:00+08:00')
        self.assertTrue(self.check('股价10元')['issues'])

    def test_draft_calculation_does_not_count_as_executed_evidence(self):
        self.source('generate_program',data={'kind':'financial_calculation','purpose':'growth','value':'15','unit':'%'})
        self.assertTrue(self.check('涨幅15%')['issues'])

    def test_unknowns_and_hypotheticals_are_not_certified_facts(self):
        self.assertEqual(self.check('我不知道，现有信息无法确认。')['status'],'not_verified')
        report=self.check('假设股价100元，仅作示例。')
        self.assertFalse(report['issues'])
        self.assertTrue(report['unverified'])
        self.source()
        self.assertTrue(self.check('我无法确认其他信息，但是股价99元。')['issues'])
        self.assertTrue(self.check('股价99元不是最终收盘价')['issues'])
        self.assertTrue(self.check('不是没有证据股价99元')['issues'])
        self.assertTrue(self.check('若羽臣股价99元')['issues'])

    def test_user_attribution_is_separate_from_external_verification(self):
        self.state['user_updates']=[{'text':'我提供的股价是100元'}]
        report=self.check('按你提供的信息，股价100元。')
        self.assertFalse(report['issues'])
        self.assertTrue(report['unverified'])
        report=self.check('按你提供的股价100元')
        self.assertFalse(report['issues'])
        self.assertTrue(report['unverified'])
        self.assertTrue(self.check('按你提供的股价999元')['issues'])

    def test_unlabelled_amount_is_not_matched_to_any_number_in_sources(self):
        self.source()
        self.assertTrue(self.check('10元')['issues'])
        self.assertTrue(self.check('腾讯控股股价10元')['issues'])

    def test_index_points_and_foreign_currency_are_not_yuan(self):
        self.ctx.symbol='000001.SH'
        self.source(symbol='000001.SH',data={'name':'上证指数','price':3000})
        self.assertFalse(self.check('上证指数点位3000点')['issues'])
        self.assertTrue(self.check('上证指数点位3000元')['issues'])
        self.ctx.result_tools.clear();self.ctx.symbol='600519.SH'
        self.source('get_financial_statements',data={'reports':[{'report_period':'2025-12-31','currency':'USD','metrics':{'revenue':{'value':100}}}]})
        self.assertFalse(self.check('2025年营收100美元')['issues'])
        self.assertTrue(self.check('2025年营收100元')['issues'])

    def test_executed_growth_and_decline_sign(self):
        self.source('execute_program',data={'kind':'financial_calculation','purpose':'growth','value':'15','unit':'%'})
        self.assertFalse(self.check('同比增长15%')['issues'])
        self.assertTrue(self.check('同比增长20%')['issues'])
        self.source('calculate_market_metrics',data={'change_amount':'-1','change_percent':'-10'})
        self.assertFalse(self.check('下跌1元，跌幅10%')['issues'])
        self.assertTrue(self.check('下跌2元')['issues'])

    def test_clarification_cannot_bypass_final_fact_checks(self):
        host=self.host(ScriptedModel(call_reply(('q','ask_user',{'question':'股价99元，你要继续吗？'})),
            ModelReply({'role':'assistant','content':'我不知道现价，请提供明确的对象。'})))
        result=host.submit('报价')
        self.assertEqual(result.status,'completed',result.reason)
        self.assertNotIn('99',result.answer)

    def host(self,model):
        self.settings.harness.memory_enabled=False
        host=SessionHost(self.settings,'fact'+str(len(self.hosts)),model=model,service=self.service)
        self.hosts.append(host)
        return host

    def test_bad_answer_is_repaired_before_delivery(self):
        model=ScriptedModel(call_reply(('q','get_quote',{'symbol':'600519.SH'})),
            ModelReply({'role':'assistant','content':'茅台报99元。'}),
            ModelReply({'role':'assistant','content':'我不知道，现有信息无法确认。'}))
        host=self.host(model)
        result=host.submit('查茅台报价')
        self.assertEqual(result.status,'completed',result.reason)
        self.assertNotIn('99',result.answer)
        self.assertIn('不知道',result.answer)
        self.assertEqual(host.fact_check_attempts,1)

    def test_persistent_conflict_fails_closed_even_with_stop_hook(self):
        host=self.host(ScriptedModel(ModelReply({'role':'assistant','content':'股价99元。'})))
        host.hooks.register('Stop',lambda **_: {'status':'completed'})
        result=host.submit('查报价')
        self.assertEqual(result.status,'failed')
        self.assertIn('FACT_CHECK_FAILED',result.reason)
        self.assertNotIn('99',result.answer)
        self.assertEqual(host.fact_check_attempts,2)

    def test_stop_limit_exhaustion_never_publishes_rejected_draft(self):
        host=self.host(ScriptedModel(ModelReply({'role':'assistant','content':'股价99元。'})))
        host.stop_limit=0
        result=host.submit('报价')
        self.assertEqual(result.status,'failed')
        self.assertNotIn('99',result.answer)

    def test_model_budget_exhaustion_during_repair_never_publishes_draft(self):
        host=self.host(ScriptedModel(ModelReply({'role':'assistant','content':'股价99元。'})))
        host.settings.limits.model_calls=1
        result=host.submit('报价')
        self.assertEqual(result.status,'failed')
        self.assertNotIn('99',result.answer)

    def test_tool_call_commentary_does_not_leak_conflicting_facts(self):
        initial=call_reply(('q','get_quote',{'symbol':'600519.SH'}))
        initial.message['content']='股价99元，我再检查。'
        host=self.host(ScriptedModel(initial,ModelReply({'role':'assistant','content':'现价10元。'})))
        displayed=[]
        host.write=displayed.append
        result=host.submit('查报价')
        self.assertEqual(result.status,'completed',result.reason)
        self.assertNotIn('99',''.join(displayed))


if __name__=='__main__':
    import unittest
    unittest.main()
