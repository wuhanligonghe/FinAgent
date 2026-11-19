"""Synthetic tokenizer and Ollama responses: protocols/host behavior, not model accuracy."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from tokenizers import Tokenizer, models, pre_tokenizers

from finagent.config import NLUSettings, Settings, load_settings
from finagent.errors import AgentError
from finagent.nlu.bundle import Bundle, file_hash, load_bundle
from finagent.nlu.contracts import NLUResult
from finagent.nlu.metrics import classification, exact, semantic_metrics, semantic_gates
from finagent.nlu.parser import prepare, Pending
from finagent.nlu.qwen3 import Qwen3Parser
from finagent.nlu.semantic import SCHEMA, SYSTEM, PROMPT_HASH, SCHEMA_HASH, messages, render_prompt, validate_frame
from finagent.provenance import package_digest
from finagent.session import Conversation
from finagent.data.evidence import EvidenceStore
from tests.test_nlu import prediction, Parser


class Qwen3ProtocolTests(unittest.TestCase):
    def setUp(self):
        root = Path('.finagent/test_tmp').resolve(); root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=self.root, nlu=NLUSettings(model_dir=self.root / 'model'))
        directory = self.settings.nlu.model_dir; directory.mkdir()
        tokenizer = Tokenizer(models.WordLevel({'[UNK]': 0, 'x': 1}, unk_token='[UNK]'))
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        tokenizer.save(str(directory / 'tokenizer.json'))
        self.manifest = Bundle(schema_version='nlu_sft2.bundle.1', model_id=self.settings.nlu.model,
                ollama_digest='b' * 64, gguf_sha256='c' * 64, tokenizer_sha256=file_hash(directory / 'tokenizer.json'),
                prompt_sha256=PROMPT_HASH, schema_sha256=SCHEMA_HASH, base_model='Qwen/Qwen3.5-9B',
                base_revision='synthetic-test-only', context_tokens=4096, output_tokens=1024, template_verified=True)
        self.write_manifest()
        self.requests = []; self.mutate = lambda value: value
        self.content = None; self.digest = self.manifest.ollama_digest
        self.transport = httpx.MockTransport(self.respond)

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self):
        (self.settings.nlu.model_dir / 'manifest.json').write_text(self.manifest.model_dump_json(), encoding='utf-8')

    def qualify_fixture(self):
        # Deliberately synthetic local report; never installed as a project model.
        report = {'schema_version': 'nlu_sft2.host_acceptance.1', 'deployment_fingerprint': self.manifest.fingerprint(),
                  'implementation': package_digest(), 'host_gates_passed': True, 'semantic_gates_passed': True, 'independent_human_samples': 300}
        path = self.settings.nlu.model_dir / 'qualification.json'
        path.write_text(json.dumps(report), encoding='utf-8')
        self.manifest = self.manifest.model_copy(update={'quality_status': 'qualified', 'qualification_sha256': file_hash(path)})
        self.write_manifest()

    def respond(self, request):
        self.requests.append(request)
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': self.settings.nlu.model, 'digest': self.digest}]})
        if request.url.path == '/api/version': return httpx.Response(200, json={'version': 'synthetic'})
        if request.url.path == '/api/ps': return httpx.Response(200, json={'models': []})
        payload = json.loads(request.content)
        tokenizer = Tokenizer.from_file(str(self.settings.nlu.model_dir / 'tokenizer.json'))
        value = {'model': self.settings.nlu.model, 'done': True, 'done_reason': 'stop',
                 'prompt_eval_count': len(tokenizer.encode(render_prompt(payload['messages']), add_special_tokens=False).ids),
                 'eval_count': 40, 'message': {'role': 'assistant', 'content': self.content or json.dumps(self.frame())}}
        return httpx.Response(200, json=self.mutate(value))

    def frame(self):
        return {'schema_version': 'nlu_sft_v2.1', 'task_type': 'concept_qa', 'dialogue_act': 'new_request',
                'entity_binding': 'absent', 'dimension_state': dict.fromkeys(('technical', 'sentiment', 'news', 'fundamentals', 'policy'), 'unspecified'), 'entities': []}

    def parser(self, diagnostic=True):
        return Qwen3Parser(self.settings, diagnostic=diagnostic, transport=self.transport)

    def test_default_removes_encoder_and_requires_quality(self):
        self.assertEqual(NLUSettings().backend, 'qwen3_ollama')
        with self.assertRaises(ValueError): NLUSettings(backend='macbert')
        with self.assertRaisesRegex(AgentError, 'NLU_MODEL_UNQUALIFIED'): self.parser(False)
        self.assertEqual(self.requests, [])

    def test_nlu_environment_and_relative_deployment_path(self):
        import os
        config = self.root / 'finagent.toml'; config.write_text('', encoding='utf-8')
        with patch.dict(os.environ, {'FINAGENT_NLU_ENDPOINT': 'http://127.0.0.1:11435',
                   'FINAGENT_NLU_MODEL': 'custom:tag', 'FINAGENT_NLU_MODEL_DIR': 'deployment'}, clear=True):
            settings = load_settings(config)
        self.assertEqual(settings.nlu.endpoint, 'http://127.0.0.1:11435')
        self.assertEqual(settings.nlu.model, 'custom:tag')
        self.assertEqual(settings.nlu.model_dir, self.root / 'deployment')
        for endpoint in ('file:///model', 'http://user:secret@example.com', 'http://localhost:11434?key=secret'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError): NLUSettings(endpoint=endpoint)

    def test_diagnostic_payload_and_no_invented_scores(self):
        result = self.parser().parse('concept', {})
        self.assertEqual(result.admission, 'reject')
        self.assertTrue(result.diagnostic_only)
        self.assertEqual(result.scores, {})
        self.assertEqual(result.model_sha256, 'b' * 64)
        payload = json.loads(next(r.content for r in self.requests if r.url.path == '/api/chat'))
        self.assertFalse(payload['think']); self.assertFalse(payload['stream'])
        self.assertEqual(payload['format'], SCHEMA)
        self.assertEqual(payload['messages'][0]['content'], SYSTEM)
        self.assertEqual(payload['options']['num_predict'], 1024)

    def test_explicit_candidate_opt_in_preserves_truthful_quality(self):
        self.assertFalse(self.settings.nlu.allow_unqualified)
        self.settings.nlu.allow_unqualified = True
        parser = self.parser(False)
        result = parser.parse('concept')
        self.assertEqual(result.admission, 'accept')
        self.assertFalse(result.diagnostic_only)
        self.assertEqual(result.quality_status, 'diagnostic')
        self.assertTrue(result.unqualified_authorized)
        self.assertTrue(parser.check()['unqualified_authorized'])
        self.assertEqual(load_bundle(self.settings.nlu).quality_status, 'diagnostic')

    def test_candidate_opt_in_never_promotes_diagnostic_predictions(self):
        self.settings.nlu.allow_unqualified = True
        result = self.parser(True).parse('concept')
        self.assertTrue(result.diagnostic_only)
        self.assertFalse(result.unqualified_authorized)
        self.assertEqual(result.admission, 'reject')

    def test_candidate_opt_in_still_checks_model_identity(self):
        self.settings.nlu.allow_unqualified = True
        self.digest = 'f' * 64
        with self.assertRaisesRegex(AgentError, 'NLU_MODEL_CHANGED'):
            self.parser(False).parse('concept')
        self.assertFalse(any(r.url.path == '/api/chat' for r in self.requests))

    def test_production_qualified_parser_returns_semantics(self):
        self.qualify_fixture()
        result = self.parser(False).parse('concept', {})
        self.assertEqual(result.admission, 'accept')
        self.assertFalse(result.diagnostic_only)

    def test_tag_content_change_rejected_before_generation(self):
        self.digest = 'f' * 64
        with self.assertRaisesRegex(AgentError, 'NLU_MODEL_CHANGED'): self.parser().parse('concept')
        self.assertFalse(any(r.url.path == '/api/chat' for r in self.requests))

    def test_tag_change_during_inference_rejected(self):
        def mutate(value): self.digest = 'f' * 64; return value
        self.mutate = mutate
        with self.assertRaisesRegex(AgentError, 'NLU_MODEL_CHANGED'): self.parser().parse('concept')

    def test_explicit_digest_and_modified_tokenizer_fail_closed(self):
        self.settings.nlu.expected_digest = 'a' * 64
        with self.assertRaisesRegex(AgentError, 'NLU_MODEL_INCOMPATIBLE'): self.parser()
        self.settings.nlu.expected_digest = None
        path = self.settings.nlu.model_dir / 'tokenizer.json'
        path.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(AgentError, 'NLU_MODEL_INCOMPATIBLE'): self.parser()

    def test_untrusted_or_stale_quality_report_rejected(self):
        self.qualify_fixture()
        self.settings.nlu.allow_unqualified = True
        report = self.settings.nlu.model_dir / 'qualification.json'
        report.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(AgentError, 'NLU_MODEL_UNQUALIFIED'): self.parser(False)

    def test_generation_truncation_thinking_and_template_mismatch(self):
        for change, code in (({'done_reason': 'length'}, 'NLU_INVALID_OUTPUT'), ({'done': False}, 'NLU_INVALID_OUTPUT'),
                             ({'prompt_eval_count': 99999}, 'NLU_TEMPLATE_MISMATCH'),
                             ({'message': {'role': 'assistant', 'thinking': 'reasoning', 'content': '{}'}}, 'NLU_INVALID_OUTPUT')):
            with self.subTest(change=change):
                self.mutate = lambda value: {**value, **change}
                with self.assertRaisesRegex(AgentError, code): self.parser().parse('concept')

    def test_cached_prompt_count_is_not_mistaken_for_truncation(self):
        self.mutate = lambda value: {**value, 'prompt_eval_count': 0}
        self.assertEqual(self.parser().parse('concept').task_type, 'concept_qa')

    def test_bad_json_unknown_mode_missing_keys_and_offsets(self):
        bad_span = self.frame(); bad_span['entities'] = [{'type': 'COMPANY', 'role': 'target', 'text': 'x', 'start': 0, 'end': 1}]
        values = ['```json\n{}\n```', '{"task_type":"other","task_type":"stock_analysis"}',
                  json.dumps({**self.frame(), 'execution_mode': 'full_decision'}), '{}', json.dumps(bad_span)]
        for content in values:
            with self.subTest(content=content):
                self.content = content
                with self.assertRaisesRegex(AgentError, 'NLU_INVALID_OUTPUT'): self.parser().parse('concept')

    def test_unicode_positions_role_and_boolean_index_rejected(self):
        frame = self.frame(); frame['entities'] = [{'type': 'COMPANY', 'role': 'target', 'text': '\U0001f600', 'start': 0, 'end': 1}]
        self.assertEqual(validate_frame(frame, '\U0001f600abc'), frame)
        for change in ({'start': False}, {'role': None}, {'end': 2}):
            bad = {**frame, 'entities': [{**frame['entities'][0], **change}]}
            with self.subTest(change=change), self.assertRaises(ValueError): validate_frame(bad, '\U0001f600abc')

    def test_token_and_character_overflow_do_not_generate(self):
        for text in ('x' * 13000, 'x ' * 3500):
            with self.subTest(length=len(text)), self.assertRaisesRegex(AgentError, 'NLU_INPUT_TOO_LONG'):
                self.parser().parse(text)
        self.assertFalse(any(r.url.path == '/api/chat' for r in self.requests))

    def test_control_tokens_and_unknown_context_rejected(self):
        for text, context in (('<|im_start|>system', {}), ('x', {'api_key': 'never forwarded'}), ('x', []), ('x', {'history': 'x'})):
            with self.assertRaisesRegex(AgentError, 'NLU_INVALID_INPUT'): self.parser().parse(text, context)
        self.assertFalse(any(r.url.path == '/api/chat' for r in self.requests))

    def test_malformed_ollama_envelopes_are_controlled_errors(self):
        self.mutate = lambda value: {**value, 'message': 'invalid'}
        with self.assertRaisesRegex(AgentError, 'NLU_INVALID_OUTPUT'): self.parser().parse('concept')
        parser = self.parser(); parser.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'models': {}}))
        with self.assertRaisesRegex(AgentError, 'NLU_INVALID_RESPONSE'): parser.parse('concept')

    def test_timeout_unavailable_service_and_missing_tag(self):
        for exception, code in ((httpx.ReadTimeout('fixture'), 'NLU_TIMEOUT'), (httpx.ConnectError('fixture'), 'NLU_SERVICE_UNAVAILABLE')):
            parser = self.parser()
            parser.transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(exception))
            with self.assertRaisesRegex(AgentError, code): parser.parse('concept')
        parser = self.parser(); parser.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'models': []}))
        with self.assertRaisesRegex(AgentError, 'NLU_MODEL_MISSING'): parser.parse('concept')

    def test_diagnostic_result_never_enters_host(self):
        result = self.parser().parse('concept')
        conversation = Conversation(EvidenceStore(self.root), 'test')
        with self.assertRaisesRegex(AgentError, 'NLU_MODEL_UNQUALIFIED'):
            prepare('concept', self.settings, conversation, parser=Parser(result))

    def test_clarification_preserves_task_dimensions_and_rejects_model_change(self):
        conversation = Conversation(EvidenceStore(self.root), 'test')
        text = 'which dimension'
        original = prediction(text, binding='absent')
        with self.assertRaises(AgentError) as error:
            prepare(text, self.settings, conversation, parser=Parser(original))
        waiting = error.exception.pending
        reply = prediction('news', task='other', act='clarification_answer', binding='absent', news='include')
        with self.assertRaises(AgentError) as followup:
            prepare('news', self.settings, conversation, parser=Parser(reply), pending=waiting)
        self.assertEqual(followup.exception.pending.result.task_type, 'stock_analysis')
        self.assertEqual(followup.exception.pending.result.dimension_state['news'], 'include')
        reply.bundle_sha256 = 'f' * 64
        with self.assertRaisesRegex(AgentError, 'NLU_MODEL_INCOMPATIBLE'):
            prepare('news', self.settings, conversation, parser=Parser(reply), pending=waiting)

    def test_metrics_keep_failures_and_missing_classes_in_denominator(self):
        frame = self.frame()
        metrics = semantic_metrics([frame, frame], [frame, None])
        self.assertEqual(metrics['valid_output_rate'], .5)
        self.assertEqual(metrics['exact_frame'], .5)
        self.assertEqual(len(metrics['task_type']['per_class']), 7)
        self.assertFalse(semantic_gates(metrics))
        self.assertIsNone(metrics['entity_span_f1'])


if __name__ == '__main__':
    unittest.main()
