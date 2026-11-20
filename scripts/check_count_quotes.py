"""Every quoted test and health-check count must match the real one.

Usage: python scripts/check_count_quotes.py <test-count>

The repo quotes both counts in CLAUDE.md, README.md, AGENTS.md and the 37
skills.  ``health.sh`` used to compare only the test count, in two files, so a
release could refresh those two lines, pass, and leave everything else stale:
three releases on 2026-11-19 did exactly that (docs/issues.md, "Count quotes
drift because health.sh checks two of them").

Matching is by shape, never by value: a check written with literal numbers
stops matching once they move, which is the drift it exists to catch.

CLAUDE.md's release-history bullets (``- **Version:`` / ``- **Prior release``,
with their indented continuation paragraphs) are skipped: their numbers record
what each release had, and rewriting them would falsify the record.  Historical
values elsewhere are spelled out in words for the same reason (CLAUDE.md
§ Skills), so they do not match.

Exit status 0 when every quote matches, 1 otherwise (each stale quote listed).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_QUOTE = re.compile(r"\b(\d{3,4}) tests\b")
CHECK_QUOTES = (
    re.compile(r"\b(\d{2}) (?:automated |health |distinct )?checks\b"),
    re.compile(r"Health \((\d{2})\b"),
)
CHECK_ID = re.compile(r'check_id="([^"]+)"')
HISTORY_BULLET = re.compile(r"^- \*\*(?:Version:|Prior release)")


def real_check_count() -> int:
    source = (ROOT / "src/fleet_manager/server/health_engine.py").read_text()
    return len(set(CHECK_ID.findall(source)))


def quoted_files() -> list[Path]:
    files = [ROOT / "CLAUDE.md", ROOT / "README.md", ROOT / "AGENTS.md"]
    return files + sorted((ROOT / "skills").glob("*/SKILL.md"))


def live_lines(path: Path):
    """(line number, text) for every line whose numbers should be current."""
    in_history = False
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if path.name == "CLAUDE.md":
            if HISTORY_BULLET.match(line):
                in_history = True
            elif in_history and line and not line[0].isspace():
                in_history = False  # the next top-level bullet or heading
            if in_history:
                continue
        yield number, line


def stale_quotes(tests: int, checks: int) -> list[str]:
    stale = []
    for path in quoted_files():
        rel = path.relative_to(ROOT)
        for number, line in live_lines(path):
            for m in TEST_QUOTE.finditer(line):
                if int(m.group(1)) != tests:
                    stale.append(f"{rel}:{number}: says {m.group(0)!r}, the suite ran {tests}")
            seen = set()  # "Health (56 checks)" matches both patterns: report once
            for pattern in CHECK_QUOTES:
                for m in pattern.finditer(line):
                    if m.start(1) in seen:
                        continue
                    seen.add(m.start(1))
                    if int(m.group(1)) != checks:
                        stale.append(
                            f"{rel}:{number}: says {m.group(0)!r}, the engine has {checks} checks"
                        )
    return stale


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print(__doc__.splitlines()[2], file=sys.stderr)
        return 2
    tests, checks = int(argv[1]), real_check_count()
    stale = stale_quotes(tests, checks)
    for entry in stale:
        print(entry)
    if not stale:
        print(f"all quotes match: {tests} tests, {checks} checks")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
