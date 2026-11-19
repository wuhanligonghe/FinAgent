"""Deterministic replay of a stored transcript; not an end-to-end speed benchmark."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from finagent.config import load_settings
from finagent.harness.context import ContextManager
from finagent.harness.state import atomic_json
from finagent.models import ModelReply
from finagent.runtime import ExecutionContext


class RecordedSummary:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools, timeout):
        self.calls += 1
        records = json.loads(messages[-1]['content'])
        source = records[0]['source_id']
        value = {key: [] for key in ['user_needs', 'confirmed_parameters', 'completed', 'current_progress', 'pending']}
        value['pending'] = [{'text': '合成回放摘要：原始历史已归档，需要时读取原文，不能据摘要补造事实。', 'source_ids': [source]}]
        return ModelReply({'role': 'assistant', 'content': json.dumps(value, ensure_ascii=False)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('transcript', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    settings = load_settings()
    source = args.transcript.resolve(strict=True)
    if not source.is_relative_to(settings.data_dir.resolve()):
        parser.error('Transcript must belong to the configured data directory')
    original = source.read_bytes()
    rows = [json.loads(line) for line in original.decode().splitlines() if line.strip()]
    state = {'user_updates': [], 'user_constraints': [], 'todos': []}
    model = RecordedSummary()
    manager = ContextManager(args.output.parent / 'replay', state, model, summary_config=settings.context_summary)
    ctx = ExecutionContext()
    messages, turns = [], []
    for index, record in enumerate(rows):
        item = deepcopy(record)
        if item.get('_finagent_origin') == 'user_input':
            if turns:
                turns[-1]['summary_calls'] = manager.budget.calls
            manager.begin_turn()
            state['user_updates'].append({'id': 'replay_' + str(index), 'text': item['content']})
            turns.append({'question': item['content'], 'main_rounds': 0, 'summary_calls': 0, 'peak_history_chars': 0})
        if item.get('_finagent_origin') == 'runtime_context':
            snapshot = json.loads(item['content'])
            for key in ['user_constraints', 'todos', 'focus']:
                if key in snapshot.get('session', {}):
                    state[key] = deepcopy(snapshot['session'][key])
            ctx.scope_constraints = deepcopy(snapshot.get('scope_constraints', {}))
        if item.get('role') == 'assistant' and turns:
            manager.compact(messages, settings.limits.context_chars, ctx=ctx)
            turns[-1]['main_rounds'] += 1
            turns[-1]['peak_history_chars'] = max(turns[-1]['peak_history_chars'], len(json.dumps(messages, ensure_ascii=False)))
        messages.append(item)
    if turns:
        turns[-1]['summary_calls'] = manager.budget.calls
    report = {'kind': 'deterministic_context_policy_replay_not_live_model_or_end_to_end_latency',
              'transcript': str(source), 'turns': turns, 'total_summary_calls': model.calls,
              'original_transcript_unchanged': source.read_bytes() == original,
              'limits': settings.context_summary.model_dump(),
              'limitations': ['Summary replies are synthetic; measured counts are policy replay only.',
                              'Recorded main-model actions/results remain unchanged; a live run may choose different actions.']}
    atomic_json(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k not in ['transcript', 'limitations']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
