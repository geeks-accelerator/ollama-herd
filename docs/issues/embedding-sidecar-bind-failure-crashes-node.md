# Bind failure on an optional embedding sidecar crashes the entire node

**Status:** ✅ Fixed 2026-07-08
**Severity:** High (an optional capability failing takes down core inference routing)
**Filed:** 2026-07-08
**Fixed:** 2026-07-08 — eager bind + fail-soft in `node/agent.py`

## Fix landed

`_bind_sidecar_socket()` (new, `node/agent.py`) binds the listening socket
eagerly during setup and returns `None` on `OSError`. Both
`_ensure_embedding_server()` (vision, 11438) and
`_ensure_text_embedding_server()` (text, 11439) now bind before creating the
uvicorn task, log a warning and return with the port set to `0` on conflict,
and hand the pre-bound socket to `Server.serve(sockets=[sock])`. The "started"
log now only fires after a confirmed bind.

Verified live: starting a second `herd-node` while the first held 11438/11439
logged `text/vision embedding server port … unavailable (Address already in
use) — … disabled; node continues serving` and the second node **stayed
alive** instead of crashing. Regression tests in
`tests/test_node/test_agent.py`:
`test_bind_sidecar_socket_free_port_returns_socket`,
`test_bind_sidecar_socket_busy_port_returns_none`,
`test_ensure_embedding_server_survives_port_conflict`. Full suite: 1009 passed.

---


## Summary

When the native **text** embedding server (port 11439) or **vision** embedding
server (port 11438) fails to bind its port, the failure is `uvicorn`'s
`sys.exit(1)`, which raises `SystemExit`. Because the server runs as a
fire-and-forget `asyncio.create_task(...)`, asyncio re-raises `SystemExit` out
of the event loop and **the whole `herd-node` process exits with code 1** —
killing heartbeats, LAN proxy, and LLM routing along with the embedding sidecar.

An optional embedding capability that can't grab its port should degrade *itself*
(log a warning, leave the port at 0), not take down the entire node. This
violates node sovereignty / fail-soft-for-optional-features.

## Reproduction

1. On a node with `fastembed` installed (`uv sync --extra embedding`), start
   `herd-node`. The text embedding server binds 11439.
2. Restart the node before the previous process has released the port
   (e.g. `pkill -f "bin/herd-node"; sleep 2; uv run herd-node`). uvicorn's
   graceful shutdown can outlast the sleep, so 11439 is still held.
3. The new node logs `Text embedding server started on 0.0.0.0:11439`,
   then `[Errno 48] address already in use`, then `SystemExit: 1`.
4. `pgrep -fl "bin/herd-node"` → nothing. The node is dead, not just the
   embedding server.

Observed live on 2026-07-08 (Mac / M4 Max). Note the port holder was the
*previous, still-shutting-down* node process, **not** a persistent orphan —
`lsof -iTCP:11439` was empty seconds after the crash, confirming the
predecessor finished dying and released the port. The trigger is a
restart/shutdown race, but the crash itself reproduces for *any* cause of a
busy port (another app, a stale process, a second node instance).

## Root cause

Four things line up in `node/agent.py`:

1. **The "started" log is optimistic.** `_ensure_text_embedding_server()`
   (agent.py:496) logs "started on 11439" immediately after
   `asyncio.create_task(server.serve())` — *before* `serve()` has actually
   bound the socket. So the log claims success and the bind fails afterward.
2. **The failure is `sys.exit(1)`, not an exception.** uvicorn's `startup()`
   calls `sys.exit(1)` on bind failure (`uvicorn/server.py:182`), raising
   `SystemExit`.
3. **asyncio re-raises `SystemExit` out of the loop.** Unlike a normal
   `Exception` in a fire-and-forget task (which asyncio merely logs as
   "Task exception was never retrieved"), `SystemExit`/`KeyboardInterrupt`
   are special-cased and propagated into `run_forever()`, tearing down the
   loop and exiting the process.
4. **The `try/except` can't catch it.** The `except Exception` at
   agent.py:500 misses on two counts: the failure happens *later, inside the
   task*, not during setup; and `SystemExit` is a `BaseException`, not an
   `Exception`.

The **vision** embedding server (`_ensure_embedding_server()`, agent.py:445)
has the identical pattern and the same bug.

## Proposed fix

Bind the socket **eagerly during setup**, inside the existing `try/except`, so
a bind failure surfaces as a catchable `OSError` before the task is created:

- Create the socket and `sock.bind((host, port))` explicitly inside the
  `try` block; on `OSError`, log a warning, set `_text_embedding_port = 0`,
  and return — node keeps running.
- Hand the already-bound socket to uvicorn (`Server.serve(sockets=[sock])`)
  or pre-check the port and let uvicorn bind, but either way catch the failure
  at setup time, not inside a fire-and-forget task.
- Move the "started" log to *after* a confirmed bind.

This also makes the restart race a non-event: the node logs
`text embedding port 11439 busy — text embedding disabled` and continues
serving inference, instead of crashing.

Apply the same fix to `_ensure_embedding_server()` (vision, 11438). Consider a
shared helper since transcription (agent.py:399, a `subprocess.Popen`) and MLX
already have their own lifecycle handling — the two in-process uvicorn sidecars
are the ones that can escalate a bind failure to a process exit.

## Notes

- `_ensure_text_embedding_server` and `_ensure_embedding_server` run in-process
  as asyncio tasks (not subprocesses), so they die with the node — there is no
  orphan process to reap. The reliability problem is purely the crash-on-bind.
- A regression test can assert that a pre-bound port on 11439/11438 leaves the
  node's main loop alive (embedding port reported as 0), rather than raising
  `SystemExit`.
