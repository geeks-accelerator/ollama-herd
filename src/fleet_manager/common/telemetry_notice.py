"""One-time, visible first-run telemetry notice.

This is a published promise, not a nicety.  ollamaherd.com/telemetry says the
notice appears on first start and that ``FLEET_NODE_TELEMETRY=false`` works
"including the first run".  Default-on telemetry is only defensible *because*
of those two things, so treat this module as load-bearing:

- It prints to stdout via the caller's echo function.  It must never be
  demoted to ``logger.debug`` -- a notice nobody sees is the definition of
  the sneaky default this design exists to avoid.
- It is shown BEFORE anything is ever sent, on the run that would send first.
- It names the leaderboard.  Unnamed herds are listed publicly under a handle
  derived from ``install_id``, so "anonymous" means *pseudonymous*, not absent.
  Disclosing that here is what keeps pseudonymous listing honest -- the site
  gates listing on ``agent_version``, so only installs that saw THIS text are
  ever listed.  If you change the wording, do not drop the leaderboard line.

The marker file only records that the notice was displayed.  It deliberately
does not gate sending: if a user deletes ``~/.fleet-manager`` they get the
notice again, which is the harmless direction to fail.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_NOTICE_MARKER = Path.home() / ".fleet-manager" / "telemetry_notice_shown"

NOTICE = """
  ┌─ Anonymous usage stats ──────────────────────────────────────────────┐
  │ Herd sends one anonymous summary a day: which models ran, how many   │
  │ requests, and error categories. Never prompts, never your hostname.  │
  │ It is a random id, not you, and it helps decide what to fix next.    │
  │                                                                      │
  │ That random id also gives you a spot on the public leaderboard, as a │
  │ handle like "herd-7f3a2b". Want your own name shown there instead?   │
  │   FLEET_NODE_HERD_NICKNAME="My Herd"   (or the dashboard Settings)   │
  │                                                                      │
  │ Opt out of all of it:  FLEET_NODE_TELEMETRY=false                    │
  │ Exactly what is sent:  https://ollamaherd.com/telemetry              │
  └──────────────────────────────────────────────────────────────────────┘
"""


def has_shown_notice(marker: Path | None = None) -> bool:
    return (marker or DEFAULT_NOTICE_MARKER).exists()


def _record_shown(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1\n", encoding="utf-8")
    except OSError as exc:
        # Non-fatal: the cost is showing the notice again next start, which is
        # the safe direction. Never let this stop an agent from booting.
        logger.debug("could not record telemetry notice marker: %s", exc)


def show_first_run_notice_if_needed(
    enabled: bool,
    echo: Callable[[str], None] = print,
    marker: Path | None = None,
) -> bool:
    """Show the one-time notice.  Returns True if it was displayed.

    When telemetry is disabled we show nothing and record nothing: a user who
    opted out should not be nagged, and should not have files created on their
    behalf about a feature they declined.
    """
    if not enabled:
        return False

    target = marker or DEFAULT_NOTICE_MARKER
    if target.exists():
        return False

    echo(NOTICE)
    _record_shown(target)
    return True
