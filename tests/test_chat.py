"""v3 terminal migration: persistent sessions replace separate per-input runs.

Old required_tools/require_data CLI and run/chat routing assertions were retired
by R01/R04/R13/R14. Their replacements exercise the actual session host.
"""
from contextlib import redirect_stdout
import io
import json
from unittest.mock import patch

from finagent.agent import AgentLoop
from finagent.chat import chat_loop
from finagent.cli import main
from finagent.harness.state import RuntimeStore
from finagent.models import ModelReply
from .support import ServiceCase,ScriptedModel,call_reply,final_reply

class ChatTests(ServiceCase):
    def run_chat(self,model,inputs,**options):
        entries=iter(inputs);output=[]
        def read(prompt):
            entry=next(entries)
            if isinstance(entry,BaseException):raise entry
            return entry
        code=chat_loop(AgentLoop(model,self.registry,self.store),self.settings.limits,
                       read=read,write=output.append,**options)
        runtime=RuntimeStore(self.settings.data_dir/'v3')
        rows=runtime.events(kind='turn_completed')
        records=[]
        for row in rows:
            value=json.loads(row['body'])['payload']
            records.append({**value['result'],'result_tools':value['result_tools'],
                            'queried_symbols':value['subjects'],'as_of':value['cutoff'],
                            'source_requests':value['source_requests']})
        return code,output,records

    def test_greeting_is_one_call_without_nlu_or_data(self):
        model=ScriptedModel(final_reply(answer='你好，我是 FinSight。'))
        _,output,records=self.run_chat(model,['你好','/exit'])
        self.assertEqual((records[0]['status'],records[0]['model_calls'],records[0]['tool_calls']),('completed',1,0))
        self.assertIn('FinSight','\n'.join(output))
        self.assertEqual(self.requests,[])

    def test_two_inputs_keep_transcript_and_evidence(self):
        model=ScriptedModel(call_reply(('q','get_quote',{'symbol':'600519'})),final_reply(answer='行情已取得'),
                            ModelReply({'role':'assistant','content':'[]'}),
                            final_reply(answer='解释刚才的行情'), ModelReply({'role':'assistant','content':'[]'}))
        _,_,records=self.run_chat(model,['查茅台行情','解释刚才结果','/exit'])
        self.assertEqual(len(records),2)
        self.assertEqual(records[0]['result_tools'],records[1]['result_tools'])
        self.assertIn('行情已取得',json.dumps(model.messages_seen[-2],ensure_ascii=False))
        self.assertEqual(model.index, 5)  # Three main calls and two discoveries.

    def test_tool_clarification_and_reply_keep_session(self):
        model=ScriptedModel(call_reply(('a','ask_user',{'question':'哪家公司？'})),final_reply(answer='已收到公司'),
                            ModelReply({'role':'assistant','content':'[]'}))
        _,_,records=self.run_chat(model,['看看行情','茅台','/exit'])
        self.assertEqual([r['status'] for r in records],['needs_clarification','completed'])
        self.assertIn('哪家公司',json.dumps(model.messages_seen[-2],ensure_ascii=False))
        self.assertEqual(model.index, 3)  # The clarification itself is not discovered.

    def test_clarification_keeps_budget(self):
        self.settings.limits.model_calls=1
        model=ScriptedModel(call_reply(('a','ask_user',{'question':'哪家公司？'})),final_reply())
        _,_,records=self.run_chat(model,['看看行情','茅台','/exit'])
        self.assertIn('MODEL_BUDGET',records[-1]['reason'])
        self.assertEqual(model.index,1)

    def test_cancel_clears_pending_work_but_keeps_conversation(self):
        self.settings.limits.model_calls=1
        model=ScriptedModel(call_reply(('a','ask_user',{'question':'哪家公司？'})),final_reply(answer='你好'))
        _,_,records=self.run_chat(model,['查行情','/cancel','你好','/exit'])
        self.assertEqual(records[-1]['status'],'completed')
        self.assertEqual(model.index,2)

    def test_failed_response_does_not_end_terminal(self):
        model=ScriptedModel(ModelReply({'role':'assistant','content':''}),final_reply(answer='恢复回答'))
        _,output,records=self.run_chat(model,['解释概念','再试一下','/exit'])
        self.assertEqual([r['status'] for r in records],['failed','completed'])
        self.assertIn('恢复回答','\n'.join(output))

    def test_interrupt_in_model_cancels_work_and_next_input_works(self):
        def interrupted(messages):raise KeyboardInterrupt
        model=ScriptedModel(interrupted,final_reply(answer='继续回答'))
        _,_,records=self.run_chat(model,['开始','继续','/exit'])
        self.assertEqual([r['status'] for r in records],['cancelled','completed'])

    def test_commands_do_not_call_model(self):
        model=ScriptedModel(final_reply())
        _,output,records=self.run_chat(model,['/help','/sources','/details','/skills','/exit'])
        self.assertEqual(model.index,0)
        self.assertFalse(records)
        self.assertIn('/research','\n'.join(output))

    def test_cutoff_is_stable_when_explicit(self):
        from finagent.runtime import now
        cutoff=now()
        model=ScriptedModel(final_reply(),final_reply())
        _,_,records=self.run_chat(model,['解释','继续','/exit'],as_of=cutoff)
        self.assertEqual(records[0]['as_of'],records[1]['as_of'])

    def test_legacy_commands_are_rejected_without_models(self):
        for command in ['run','chat','resume']:
            with self.subTest(command=command),redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([command]),2)
                self.assertIn('持续会话',output.getvalue())

    def test_default_entry_is_persistent_terminal(self):
        with patch('finagent.cli.load_settings',return_value=self.settings), \
             patch('finagent.cli.run_terminal',return_value=0) as terminal, \
             patch('finagent.cli.SessionHost') as host:
            self.assertEqual(main([]),0)
            terminal.assert_called_once()
            host.assert_called_once()
