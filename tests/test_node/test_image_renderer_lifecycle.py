"""A timed-out render must not outlive its request.

Reported by Krivo-dero (issue #6) with a standalone harness: `generate_image`
awaited `asyncio.wait_for(proc.communicate(), ...)` and, on timeout, returned
504 without signalling the child. `wait_for` cancels the *await*, not the
process. Two things followed:

* the renderer kept running -- holding GPU and memory on a node, with nothing
  left to reap it, which on this project has precedent (`mlx_lm.server`
  orphans holding ports for hours);
* the `finally` deleted `output_path` while the child was still alive, so mflux
  wrote its PNG *after* cleanup and left a stray file behind.

The reporter's table is reproduced directly below: normal completion leaves no
child and no files; a timeout used to leave both.

The fix reaps in `finally` rather than in the `except TimeoutError` branch, so
client disconnect -- which raises CancelledError through the same path -- is
covered by construction instead of needing its own handler.
"""

import asyncio
import os
import sys
import tempfile

import pytest

from fleet_manager.node import image_server


class _Child:
    """A real subprocess that writes a marker file after a delay.

    A real process, not a mock: the bug is about signal delivery and reaping,
    and a mock proves nothing about either.
    """

    def __init__(self, marker: str, delay: float):
        self.marker = marker
        self.delay = delay
        self.proc = None

    async def start(self):
        code = (
            f"import time,sys\n"
            f"time.sleep({self.delay})\n"
            f"open({self.marker!r},'w').write('late output')\n"
        )
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        return self.proc


@pytest.mark.asyncio
class TestReapRenderer:
    async def test_a_live_child_is_stopped_and_reaped(self):
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "late.png")
            child = _Child(marker, delay=5.0)
            proc = await child.start()
            assert proc.returncode is None, "child should be running"

            await image_server._reap_renderer(proc)

            assert proc.returncode is not None, "child must be reaped, not left running"
            # And it must not have had time to write its output.
            await asyncio.sleep(0.3)
            assert not os.path.exists(marker), (
                "a reaped child must not produce output after cleanup"
            )

    async def test_no_late_file_survives_the_handler_ordering(self):
        """The ordering bug: unlink ran while the child was still alive.

        Reaping first is what makes the delete final.
        """
        with tempfile.TemporaryDirectory() as d:
            marker = os.path.join(d, "out.png")
            child = _Child(marker, delay=0.25)
            proc = await child.start()

            # The real handler's finally: reap, then unlink.
            await image_server._reap_renderer(proc)
            if os.path.exists(marker):
                os.unlink(marker)

            await asyncio.sleep(0.6)  # well past the child's write delay
            assert not os.path.exists(marker), (
                "child wrote output after cleanup -- the reported defect"
            )

    async def test_an_already_exited_child_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            child = _Child(os.path.join(d, "x"), delay=0.0)
            proc = await child.start()
            await proc.wait()
            rc = proc.returncode
            await image_server._reap_renderer(proc)
            assert proc.returncode == rc

    async def test_none_is_a_no_op(self):
        """create_subprocess_exec can itself raise, leaving proc unbound/None."""
        await image_server._reap_renderer(None)

    async def test_a_child_ignoring_sigterm_is_killed(self, monkeypatch):
        """mflux can sit in a Metal call that ignores SIGTERM.

        Same reason the documented MLX restart recipe uses -9.
        """
        monkeypatch.setattr(image_server, "RENDERER_TERM_GRACE_S", 0.3)
        code = (
            "import signal,time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(30)\n"
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.sleep(0.2)  # let the handler install
        await image_server._reap_renderer(proc)
        assert proc.returncode is not None, "SIGTERM-immune child must be SIGKILLed"

    async def test_reaping_never_raises(self):
        """Cleanup must not replace the response the caller is already getting."""
        class Exploding:
            returncode = None
            pid = 1234
            def terminate(self): raise OSError("boom")
        await image_server._reap_renderer(Exploding())


class TestTimeoutConstantIsNotDuplicated:
    def test_the_log_line_does_not_hardcode_the_number(self):
        """The message said "180s" literally, so changing the timeout would lie."""
        import inspect

        src = inspect.getsource(image_server)
        assert "timed out after 180s" not in src
        assert "IMAGE_TIMEOUT_S" in src

    def test_the_handler_reaps_before_it_unlinks(self):
        """Pin the ordering, which is the actual defect.

        Deleting first and reaping second would still leave the stray file.
        """
        import inspect

        src = inspect.getsource(image_server.generate_image)
        reap = src.index("_reap_renderer(proc)")
        unlink = src.index("os.unlink(output_path)")
        assert reap < unlink, "must stop the renderer before deleting its output"

    def test_proc_is_bound_before_the_try(self):
        """Otherwise the finally raises NameError instead of cleaning up."""
        import inspect

        src = inspect.getsource(image_server.generate_image)
        assert "proc = None" in src
        # Match the CALL, not the word: the explanatory comment above the
        # binding mentions create_subprocess_exec and matched first.
        assert src.index("proc = None") < src.index(
            "await asyncio.create_subprocess_exec"
        )
