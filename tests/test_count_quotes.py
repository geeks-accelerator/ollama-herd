"""scripts/check_count_quotes.py: every quoted count is checked, history is not.

The script is what ``health.sh`` runs; these pin its two judgements on a fake
repo, so a regex change cannot quietly stop it matching (the drift it exists to
catch) or start rewriting CLAUDE.md's release record.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_count_quotes.py"


@pytest.fixture
def checker(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("check_count_quotes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    engine = tmp_path / "src/fleet_manager/server/health_engine.py"
    engine.parent.mkdir(parents=True)
    engine.write_text('check_id="a"\ncheck_id="b"\ncheck_id="a"\n')  # 2 distinct
    (tmp_path / "skills/one").mkdir(parents=True)
    for name in ("CLAUDE.md", "README.md", "AGENTS.md", "skills/one/SKILL.md"):
        (tmp_path / name).write_text("nothing quoted here\n")
    return module


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text)


def test_check_count_is_distinct_ids(checker):
    assert checker.real_check_count() == 2


def test_current_quotes_pass(checker, tmp_path):
    _write(tmp_path, "README.md", "run all 1234 tests\n12 automated checks? no: Health (12)\n")
    assert checker.stale_quotes(1234, 12) == []


@pytest.mark.parametrize(
    "name,text",
    [
        ("AGENTS.md", "uv run pytest  # 1200 tests\n"),
        ("skills/one/SKILL.md", "- 1200 tests, async Python\n"),
        ("skills/one/SKILL.md", "# Fleet health — 11 automated checks\n"),
        ("skills/one/SKILL.md", "five tabs: Overview, Health (11 checks)\n"),
        ("README.md", "| Health Engine | 11 checks |\n"),
    ],
)
def test_stale_quote_anywhere_is_reported_with_its_line(checker, tmp_path, name, text):
    _write(tmp_path, name, "first line\n" + text)
    (entry,) = checker.stale_quotes(1234, 12)
    assert entry.startswith(f"{name}:2: ")


def test_claude_release_history_is_skipped_with_its_continuations(checker, tmp_path):
    _write(
        tmp_path,
        "CLAUDE.md",
        "uv run pytest  # run all 1234 tests\n"
        "## Current State\n"
        "- **Version: `0.9.0` — published** with 1000 tests, 40 checks.\n"
        "\n"
        "  **Soaked** on 1000 tests and 40 health checks.\n"
        "- **Prior release (`0.8.0`)** 900 tests.\n"
        "- **Health:** 40 distinct checks\n",  # a live bullet again: stale
    )
    (entry,) = checker.stale_quotes(1234, 12)
    assert entry.startswith("CLAUDE.md:7: ")


def test_history_skip_applies_to_claude_md_only(checker, tmp_path):
    _write(tmp_path, "README.md", "- **Version: `0.9.0`** with 1000 tests\n")
    assert len(checker.stale_quotes(1234, 12)) == 1
