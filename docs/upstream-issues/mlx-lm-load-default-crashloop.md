# Upstream issue draft: mlx-lm v0.31.3 — `load_default` → `snapshot_download` → thread_map crash

**Repo:** [`ml-explore/mlx-lm`](https://github.com/ml-explore/mlx-lm)
**Affects:** v0.31.3 (latest as of 2026-04-26), Python 3.14.3 on macOS arm64
**Status:** Not yet filed. This file is a paste-ready draft.

To file:
```bash
gh issue create --repo ml-explore/mlx-lm \
  --title "v0.31.3: load_default → snapshot_download → thread_map crashes mlx_lm.server in a tight loop" \
  --body-file docs/upstream-issues/mlx-lm-load-default-crashloop.md
```

---

# Title
v0.31.3: `load_default` → `snapshot_download` → `thread_map` crashes `mlx_lm.server` in a tight loop ("cannot schedule new futures after interpreter shutdown")

# Description

Running `mlx_lm.server v0.31.3` with `--model mlx-community/Qwen3-Coder-Next-4bit` (a 80B MoE quantized to 4-bit, ~42 GB on disk) on macOS Apple Silicon, after some hours of normal serving the process enters a state where every chat-completion request kills it with this stack trace:

```
File ".../mlx_lm/server.py", line 699, in _generate
  self.model_provider.load_default()
File ".../mlx_lm/server.py", line 385, in load_default
  self.load("default_model", None, "default_model")
File ".../mlx_lm/server.py", line 394, in load
  self._load(*model_key)
File ".../mlx_lm/server.py", line 349, in _load
  model, tokenizer = load(model_path, ...)
File ".../mlx_lm/utils.py", line 489, in load
  model_path = _download(path_or_hf_repo, revision=revision)
File ".../mlx_lm/utils.py", line 249, in _download
  snapshot_download(path_or_hf_repo, revision=revision, allow_patterns=allow_patterns)
File ".../huggingface_hub/_snapshot_download.py", line 455, in snapshot_download
  thread_map(_inner_hf_hub_download, ...)
File ".../tqdm/contrib/concurrent.py", line 69, in thread_map
  return _executor_map(ThreadPoolExecutor, fn, *iterables, **tqdm_kwargs)
File ".../tqdm/contrib/concurrent.py", line 51, in _executor_map
  return list(tqdm_class(ex.map(fn, *iterables, chunksize=chunksize), **kwargs))
File ".../concurrent/futures/_base.py", line 618, in map
  fs = [self.submit(fn, *args) for args in zipped_iterables]
File ".../concurrent/futures/thread.py", line 207, in submit
  raise RuntimeError('cannot schedule new futures after interpreter shutdown')
RuntimeError: cannot schedule new futures after interpreter shutdown
```

The request log shows the previous request returned HTTP 200 successfully — the response was emitted, then the process died. Supervisor restarts; on next request the same cycle repeats.

# Repro environment

- macOS 15.x on Apple Silicon (M3 Ultra, 512 GB unified memory)
- Python 3.14.3
- mlx-lm 0.31.3 (latest)
- huggingface-hub 0.20+
- tqdm latest
- Server launched with: `mlx_lm.server --model mlx-community/Qwen3-Coder-Next-4bit --host 0.0.0.0 --port 11440 --prompt-cache-size 4 --prompt-cache-bytes 17179869184 --kv-bits 8 --kv-group-size 64`
- Model weights are present in `~/.cache/huggingface/hub/` (no actual download needed)

(Note: we apply a small local patch to enable `--kv-bits` because [PR #1073](https://github.com/ml-explore/mlx-lm/pull/1073) hasn't merged yet. The patch only affects argument parsing + `KVQuantizedCache` wiring; it doesn't touch `load_default` or `_load`.)

# Why this seems like a bug

1. Files are present in the HF cache; `snapshot_download` should be a fast no-op (verify-only).
2. Even when verifying, `thread_map` shouldn't fail unless the interpreter is genuinely shutting down. The request handler thread is an active worker, not a daemon thread that's been signaled to exit. Why is the threadpool refusing new submissions while the request is still being served?
3. `load_default` calls `self.load("default_model", None, "default_model")` — the third positional arg is `draft_model_path`, which gets looked up in `self._draft_model_map`. If we didn't pass `--draft-model` (we didn't), `_draft_model_map["default_model"]` should be `None` and `_load` should skip the draft download branch entirely. The fact that we're hitting `snapshot_download` for the MAIN model suggests this is firing on the model_path side, not the draft side — but `_model_map.get("default_model", "default_model")` should resolve to the actual configured model name (which IS a valid HF repo with cached files), so the verify pass should be a no-op.

# Reproducer (synthetic)

We don't have a minimal reproducer yet — this happened naturally after several hours of serving real Claude Code traffic with prompts in the 60K–120K token range. **It would be useful to know:**

- What conditions cause `_generate` to enter `load_default` repeatedly, vs the model already being resident
- Why `thread_map` sees a shutting-down interpreter mid-request

We hypothesize it might be triggered by:
- Long-running prefill (60s+) racing with concurrent request handling
- A signal arriving during prefill (we don't send signals; could be macOS-internal)
- Prompt cache eviction triggering a model reload via the `default_model` path

Once it starts, **every subsequent chat completion crashes the process the same way** until the model files are removed and re-cached, OR the server is restarted, OR Python is restarted.

# Suggested investigation

1. Why does `snapshot_download` get called on a request when the model is already loaded? Is `_model_map["default_model"]` getting overwritten somewhere with a path that's not in the cache?
2. What's holding a reference to a `ThreadPoolExecutor` that's been shut down? `thread_map` creates a fresh pool each call — unless `_executor_map` is reusing one across calls.
3. Is there a code path in `_generate` that decrefs the threadpool early?

# Workaround we shipped (downstream)

In our [`ollama-herd`](https://github.com/geeks-accelerator/ollama-herd) supervisor we now detect 5 crashes within a 5-minute rolling window and switch to a 10-minute restart interval ("quarantine") with a CRITICAL health-check recommendation. Without this guard, our fleet logged 420 crash-restarts over 2.5 hours at 60s cadence on 2026-04-26 — burning CPU and flooding logs with no chance of self-recovery.

Source: [`mlx_supervisor.py::_record_crash_and_check_quarantine`](https://github.com/geeks-accelerator/ollama-herd/blob/main/src/fleet_manager/node/mlx_supervisor.py)

We'd much rather drop the workaround and rely on the upstream fix. Happy to provide more diagnostics, full logs, or test a candidate patch on our reference fleet (M3 Ultra 512 GB + M4 Max 128 GB).

# Logs

Full restart log + crash trace available on request. Key signal in our supervisor:

```
mlx_lm.server(port=11440) exited unexpectedly (rc=1); restarting in 60.0s
mlx_lm.server(port=11441) exited unexpectedly (rc=1); restarting in 60.0s
[420 such lines over 2.5 hours]
```

And the recurring stack trace shown above in every `mlx-server-<port>.log`.

---

*Filed by the ollama-herd team — happy to triage further or test patches.*
