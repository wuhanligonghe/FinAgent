"""Unicode-aware line editing for interactive terminal prompts."""
from __future__ import annotations

import sys


def read_input(prompt: str = "") -> str:
    if sys.stdin.isatty():
        try:
            # Importing readline enables input() to edit characters rather than
            # relying on the terminal driver's byte-oriented erase handling.
            import readline  # noqa: F401
        except ImportError:
            # Windows and some Python builds provide their own input handling.
            pass
    return input(prompt)
