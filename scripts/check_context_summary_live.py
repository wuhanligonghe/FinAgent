"""Read-only live check of the bounded summary profile on an archived history."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
from time import monotonic

from finagent.config import load_settings
from finagent.harness.context import ContextManager
from finagent.harness.state import atomic_json
from finagent.models import CompatibleChatModel
from finagent.security import ProtectedModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    settings = load_settings()
    archive = args.archive.resolve(strict=True)
    if not archive.is_relative_to(settings.data_dir.resolve()):
        parser.error('Archive must belong to the configured data directory')
    messages = json.loads(archive.read_text())
    if not isinstance(messages, list) or any(not isinstance(m, dict) or 'role' not in m for m in messages):
        parser.error('Archive must contain a message array')
    original = deepcopy(messages)
    state = {'user_updates': [{'id': 'archive_user_' + str(i), 'text': m['content']} for i, m in enumerate(messages)
                             if m.get('_finagent_origin') == 'user_input']}
    events = []
    config = settings.context_summary
    base = CompatibleChatModel(settings.model)
    profile = base.with_options(max_output_tokens=config.max_output_tokens,
                               thinking='disabled' if config.disable_thinking else settings.model.thinking)
    model = ProtectedModel(profile, settings)
    manager = ContextManager(args.output.parent / 'sandbox', state, model, summary_config=config,
                             event=lambda kind, **payload: events.append({'kind': kind, **payload}))
    started = monotonic()
    try:
        result = manager._summarize(messages)
    finally:
        base.close()
    report = {'kind': 'archived_history_summary_live_smoke_not_financial_accuracy_evaluation',
              'archive': str(archive), 'model': settings.model.name,
              'model_settings': {'max_output_tokens': profile.settings.max_output_tokens, 'thinking': profile.settings.thinking},
              'elapsed_seconds': round(monotonic() - started, 3), 'budget': manager.budget.snapshot(),
              'events': events, 'summary_accepted': result is not None,
              'summary_chars': len(json.dumps(result[0], ensure_ascii=False)) if result else None,
              'summary': result[0] if result else None, 'original_messages_unchanged': messages == original}
    atomic_json(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k not in {'events', 'summary', 'archive'}}, ensure_ascii=False))
    return 0 if result is not None else 1


if __name__ == '__main__':
    raise SystemExit(main())
