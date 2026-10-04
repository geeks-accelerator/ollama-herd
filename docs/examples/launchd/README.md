# launchd agents (macOS)

Keeps `herd` and `herd-node` running across reboots and crashes. Without these,
every reboot needs a manual restart — which on the reference fleet caused three
unattended outages in a month, one of them 21 hours, because nothing reports that
the router is simply absent.

## Install

Replace `YOUR_USER` and `path/to/ollama-herd` in both plists, then:

```bash
cp com.geeksaccelerator.ollama-herd.*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.geeksaccelerator.ollama-herd.router.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.geeksaccelerator.ollama-herd.node.plist
launchctl list | grep ollama-herd     # 3rd column = last exit code
```

Start order does not matter — `herd-node` retries the router.

## Operating them

`pkill` is **not** a stop once these are loaded: `KeepAlive` respawns within
`ThrottleInterval` (30s), so a pkill-then-start sequence races itself.

```bash
launchctl kickstart -k gui/$UID/com.geeksaccelerator.ollama-herd.router   # restart
launchctl bootout   gui/$UID/com.geeksaccelerator.ollama-herd.node        # really stop
pkill -9 -f mlx_lm.server   # MLX children use start_new_session; reap separately
```

## Two things that will bite you

**Do not put the logs in the project directory if it lives under `~/Desktop`.**
macOS 26 TCC blocks launchd from *writing* there. Executing from `~/Desktop` is
fine; writing is not. These plists log to `~/.fleet-manager/logs/`.

**launchd gives almost no `PATH`.** `MlxSupervisor` resolves `mlx_lm.server`
(typically `~/.local/bin`) and `mlx.launch` (`/opt/homebrew/bin`) from it, so the
plists set `PATH` explicitly. Omit it and MLX servers silently fail to spawn.

`FLEET_*` variables need no duplication here — both entry points auto-load
`~/.fleet-manager/env` at startup (`common/env_file.py`).

## Optional: environment for Ollama's llama-server

`com.geeksaccelerator.ollama-env.plist` re-applies `launchctl setenv
LLAMA_ARG_CACHE_RAM 0` at every login, because `launchctl setenv` does not survive a
reboot. That variable disables llama-server's host-RAM prompt cache (default 8 GiB per
loaded model); see `docs/configuration-reference.md` § Ollama environment before using
it, because the right value depends on the measured hit rate. Ollama launched at login
can win the race against this agent. After a reboot, check with
`ps -E -ww -o command= -p $(pgrep -f "ollama serve") | tr ' ' '\n' | grep LLAMA_ARG`,
and relaunch Ollama if it is missing.
