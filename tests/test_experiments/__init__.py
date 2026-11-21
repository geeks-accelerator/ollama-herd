"""Tests for the experiment scripts in ``docs/experiments/``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

EXPERIMENTS = Path(__file__).resolve().parents[2] / "docs" / "experiments"


def load_experiment(filename: str, name: str) -> ModuleType:
    """Import ``docs/experiments/<filename>`` as module ``name`` (the filenames
    have hyphens), registered in ``sys.modules`` before it runs like
    ``tests.test_scripts.load_script``."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, EXPERIMENTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
