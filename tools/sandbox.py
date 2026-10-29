"""Compatibility import for the evaluation module's bounded Docker bridge."""
from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.evaluation.queued_sandbox import sandbox as _implementation

# Share helper globals with the implementation, including patched test hooks.
sys.modules[__name__] = _implementation
