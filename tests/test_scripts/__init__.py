"""Tests for the measurement scripts in ``scripts/``."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load_script(name: str) -> ModuleType:
    """Import ``scripts/<name>.py`` as module ``name``.

    Registered in ``sys.modules`` before it runs: ``dataclasses`` looks a
    class's module up there, and an unregistered module fails at the first
    ``@dataclass``.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
