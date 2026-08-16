"""Tests for the anonymous install identifier.

The hostname tests here are the ones the public telemetry contract at
ollamaherd.com/telemetry points at when it says "there is a test that fails the
build if a hostname ever appears in a payload".  They are load-bearing for a
published promise, not decoration — do not weaken them.
"""

from __future__ import annotations

import socket
import uuid

from fleet_manager.common.install_id import get_install_id


def _id_file(tmp_path):
    return tmp_path / ".fleet-manager" / "install_id"


class TestGeneration:
    def test_returns_a_valid_uuid(self, tmp_path):
        value = get_install_id(_id_file(tmp_path))
        assert uuid.UUID(value)  # raises if malformed

    def test_persists_and_is_stable_across_calls(self, tmp_path):
        path = _id_file(tmp_path)
        first = get_install_id(path)
        assert path.exists(), "id must be written on first call"
        assert get_install_id(path) == first
        # Stable across a fresh import too, not just a warm cache.
        assert path.read_text().strip() == first

    def test_deleting_the_file_yields_a_new_install(self, tmp_path):
        """The contract promises this is how a user resets their identity."""
        path = _id_file(tmp_path)
        first = get_install_id(path)
        path.unlink()
        assert get_install_id(path) != first

    def test_two_installs_get_different_ids(self, tmp_path):
        """Guards against anyone 'improving' this into a machine-derived hash."""
        a = get_install_id(tmp_path / "a" / "install_id")
        b = get_install_id(tmp_path / "b" / "install_id")
        assert a != b


class TestNoMachineFingerprint:
    """The id must be random, never derived from anything about the machine."""

    def test_id_is_not_the_hostname_in_any_form(self, tmp_path):
        value = get_install_id(_id_file(tmp_path))
        hostname = socket.gethostname()

        assert hostname.lower() not in value.lower()
        # ...nor any component of it ("Neons-Mac-Studio" -> neons/mac/studio).
        for part in hostname.replace(".", "-").replace("_", "-").split("-"):
            if len(part) > 2:
                assert part.lower() not in value.lower(), (
                    f"hostname fragment {part!r} leaked into install_id"
                )

    def test_id_is_not_a_hash_of_the_hostname(self, tmp_path):
        """A stable hash would fingerprint the machine across reinstalls."""
        import hashlib

        value = get_install_id(_id_file(tmp_path)).replace("-", "")
        hostname = socket.gethostname()
        for algo in ("md5", "sha1", "sha256"):
            digest = hashlib.new(algo, hostname.encode()).hexdigest()
            assert not digest.startswith(value[:16])
            assert value not in digest


class TestResilience:
    """Telemetry is a background nicety; it must never take down an agent."""

    def test_corrupt_file_is_replaced_not_raised(self, tmp_path):
        path = _id_file(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("not-a-uuid-at-all")
        value = get_install_id(path)
        assert uuid.UUID(value)
        assert path.read_text().strip() == value, "corrupt file should be rewritten"

    def test_empty_file_is_replaced(self, tmp_path):
        path = _id_file(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("   \n")
        assert uuid.UUID(get_install_id(path))

    def test_unwritable_location_still_returns_an_id(self, tmp_path):
        """Read-only home or full disk: degrade, do not crash."""
        blocker = tmp_path / "blocked"
        blocker.write_text("I am a file, not a directory")
        # mkdir() on a path whose parent is a regular file raises OSError,
        # which get_install_id must swallow.
        value = get_install_id(blocker / "install_id")
        assert uuid.UUID(value)
