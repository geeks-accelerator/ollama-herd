"""Tests for the first-run telemetry notice.

Every assertion here backs a sentence published at ollamaherd.com/telemetry.
If one of these starts failing, the page became untrue -- fix the code or
change the page, but do not delete the test.
"""

from __future__ import annotations

from fleet_manager.common.telemetry_notice import (
    NOTICE,
    show_first_run_notice_if_needed,
)


class TestFirstRunNotice:
    def test_shown_on_first_run_when_enabled(self, tmp_path):
        seen: list[str] = []
        shown = show_first_run_notice_if_needed(
            True, echo=seen.append, marker=tmp_path / "marker"
        )
        assert shown is True
        assert seen, "notice must actually be emitted, not just recorded"

    def test_not_shown_twice(self, tmp_path):
        marker = tmp_path / "marker"
        seen: list[str] = []
        show_first_run_notice_if_needed(True, echo=seen.append, marker=marker)
        show_first_run_notice_if_needed(True, echo=seen.append, marker=marker)
        assert len(seen) == 1

    def test_marker_is_persisted(self, tmp_path):
        marker = tmp_path / "nested" / "marker"
        show_first_run_notice_if_needed(True, echo=lambda _: None, marker=marker)
        assert marker.exists()


class TestOptOutIsHonouredOnFirstRun:
    """'FLEET_NODE_TELEMETRY=false works including the first run.'"""

    def test_no_notice_when_disabled(self, tmp_path):
        seen: list[str] = []
        shown = show_first_run_notice_if_needed(
            False, echo=seen.append, marker=tmp_path / "marker"
        )
        assert shown is False
        assert seen == []

    def test_no_files_created_when_disabled(self, tmp_path):
        """A user who declined should not get state written on their behalf."""
        marker = tmp_path / "marker"
        show_first_run_notice_if_needed(False, echo=lambda _: None, marker=marker)
        assert not marker.exists()
        assert list(tmp_path.iterdir()) == []


class TestNoticeContent:
    """The notice is only useful if it says how to say no."""

    def test_names_the_opt_out_variable(self):
        assert "FLEET_NODE_TELEMETRY=false" in NOTICE

    def test_links_the_published_field_list(self):
        assert "ollamaherd.com/telemetry" in NOTICE

    def test_states_what_is_never_sent(self):
        lowered = NOTICE.lower()
        assert "prompt" in lowered and "hostname" in lowered


class TestResilience:
    def test_unwritable_marker_still_shows_notice(self, tmp_path):
        """Disclosure must not depend on the disk being writable."""
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        seen: list[str] = []
        shown = show_first_run_notice_if_needed(
            True, echo=seen.append, marker=blocker / "marker"
        )
        assert shown is True
        assert seen
