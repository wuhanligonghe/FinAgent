"""s04 lifecycle hooks; host-owned hard permission gates are not overridable."""
from collections import defaultdict


class Hooks:
    EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"}

    def __init__(self):
        self.callbacks = defaultdict(list)

    def register(self, event, callback, priority=0):
        if event not in self.EVENTS:
            raise ValueError("Unknown hook event")
        self.callbacks[event].append((priority, callback))
        self.callbacks[event].sort(key=lambda pair: pair[0])

    def trigger(self, event, **context):
        decision = None
        for _, callback in self.callbacks[event]:
            result = callback(**context)
            if result is not None and event in {"PreToolUse", "Stop"} and decision is None:
                decision = result
        return decision
