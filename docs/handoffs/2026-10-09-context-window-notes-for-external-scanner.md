Your root cause is right and the five options are well ordered. Below is context from running a long-lived service against Ollama since July — mostly things that cost us real debugging time and that bear directly on options 1–5. A few of them will change what you build.

## The window arithmetic is worse than "4k / 32k / 256k"

Ollama launches `llama-server` with **`-c NumCtx × OLLAMA_NUM_PARALLEL`** and `-np numParallel` (`llm/llama_server.go`). The per-request window each slot gets is therefore `-c ÷ -np`, not `-c`.

Meanwhile `server/routes.go` picks `defaultNumCtx` from VRAM **with no `numParallel` term at all**. So on a large box an unconfigured request asks for a vastly bigger allocation than the tier table suggests — on a 512 GB machine, `defaultNumCtx = 262144` × 4 parallel = `-c 1048576`. That's [ollama#14116](https://github.com/ollama/ollama/issues/14116), still open; the community fix (#14120) was closed unmerged.

**Two consequences for your measurement.** First, your throwaway 4k server: if `OLLAMA_NUM_PARALLEL` was its default rather than 1, each slot got less than 4k, so your truncation numbers may be from a smaller window than you think. Worth re-checking, because it affects the 2-of-7 figure. Second, your observation that "a very large window with several slots made qwen3-coder:30b fail to load" is exactly this multiplication — the window and the slot count multiply into the allocation, which is why that failure appeared only at the large end.

## Don't trust `/api/ps` for the window — read the launch args

This is the single most useful practical thing here, and I'm giving you the uncertainty along with it.

```bash
ps -Ao args | grep llama-server | grep -oE '\-c [0-9]+ \-np [0-9]+'
# per-slot window = -c ÷ -np
```

Measured on Ollama **0.35.1** just now: launch args `-c 524288 -np 4`, and `/api/ps` reported `context_length: 131072` — which equals the **per-slot** value. But our own notes from an earlier version record the opposite: `-c 131072 -np 4` displaying as `131072` while each slot really had 32768, i.e. matching the **total**. Those two observations can't both be right, so either the field's meaning changed between versions or one of our measurements was wrong. I'm rechecking on our side.

Either way: **the launch args are unambiguous and `/api/ps` isn't.** For option 2 ("ask the server what window it loaded"), that argues for deriving it from `-c ÷ -np` where you can see the process, and treating the API field as a hint that needs validating per version.

## `OLLAMA_CONTEXT_LENGTH` beats the per-request value, despite the docs

If you pin a window for testing, know that this env var is documented as applying *"unless otherwise specified"* — and it doesn't behave that way. We set it to `32768` while our requests explicitly sent `num_ctx=131072` on the native API, and the env var won every time. The server logged the conflict and ignored the request value.

The cost was instructive for your option 3: **prefix-cache reuse collapsed (5,772 → 770 hits) and TTFT went 1.0 s → 6.3 s, while decode throughput did not move at all.** Every tokens-per-second number looked perfectly healthy for six days. If your scanner measures throughput to sanity-check a run, it will not catch a window problem — latency and cache-hit rate will, and throughput won't.

## `prompt_eval_count` is the full prompt, not the cache-missed part — which makes your option 3 viable

Your option 3 ("treat a server token count far below the estimate as a possible cut") depends on knowing what that count means, and the obvious assumption is wrong in a way that would make you discard the idea.

We believed for a while that `prompt_eval_count` reported only cache misses. It doesn't. Verified on 0.34.4 by sending an identical prompt twice:

```
run 1 (cold): prompt_eval_count=4074  prompt_eval_duration=2.235s
run 2 (warm): prompt_eval_count=4074  prompt_eval_duration=0.021s
```

A 106× drop in prefill duration — the cache was unambiguously hit — and the count didn't move. **So it's the full prompt length and it is directly comparable to your estimate.** Your option 3 works, and your own call log already has the number.

Two thresholds caveats from doing the comparison on real traffic: tokenizer disagreement is real (a cl100k estimate against the model's own tokenizer differs by a few percent routinely), and chat templates add tokens the client never sent. So a single short count is noise — the signature of truncation is *every* request to one model clipping at the same ceiling. A sustained ratio per model, not a per-request alarm.

## Option 4 has a false-positive trap: thinking models legitimately return zero content

"Explain an empty completion as a load failure, not a refusal" is right, and the naive check will misfire.

Reasoning models can spend their entire `num_predict` budget inside the thinking block and return **zero content tokens** on a perfectly successful call. We hit this often enough to carry a minimum-budget floor specifically for it. If your check reads content length, every thinking model with a tight budget becomes a "load failure."

Read **total** output including the reasoning channel, or gate the check on models you know aren't reasoning models. And the real discriminator for a load failure is the server side: `llama-server`'s log carries the failure, and the HTTP exchange is a clean 200. An empty completion with a 200 and nothing in the server log is a different thing from an empty completion with a load error in it.

## Option 5: the memory you can't see is bigger than you'd guess

Each `llama-server` process holds a host-RAM prompt cache that defaults to **`--cache-ram 8192` MiB**. Ollama never sets it, and `/api/ps` doesn't report it. So a model's real footprint is up to 8 GiB above whatever you're accounting for, per loaded model.

It reads `LLAMA_ARG_CACHE_RAM` and inherits Ollama's environment, so you can bound it — but check whether it's earning its keep first:

```bash
grep -c "found better prompt"        ~/.ollama/logs/server.log
grep -c "looking for better prompt"  ~/.ollama/logs/server.log
```

On our box that's 12,565 / 90,819 — about 14%, so it's buying something. On a different machine with a different workload we measured **0 hits in 100 lookups** and set it to `0`, reclaiming the whole 8 GiB. It's workload-dependent, so measure rather than assume. This is also part of the answer to "any spill to system memory" — some of what looks like spill is this cache.

## Detecting Ollama behind an OpenAI-compatible address (option 1)

The cheap probe: Ollama answers `GET /api/version` and `GET /api/ps` alongside `/v1/*`. Both respond here.

**But I can't tell you that's sufficient**, and this is the part to verify yourself rather than take from me. Recent LM Studio versions have been adding Ollama-compatible endpoints, so `/api/version` responding may not uniquely identify Ollama any more. Since your table is keyed on "server with a native API," the safer discriminator is probably the *shape* of the response — `/api/ps` returning a `models[]` array with `context_length` and `size_vram` fields is quite specific — plus a version-string sanity check. Test it against each of LM Studio, llama.cpp's own server, and vLLM before trusting it; a misdetection that silently switches clients would be worse than the current default.

For the others, from least to most certain: llama.cpp's server exposes `/props` and `/slots`, and `/slots` gives you per-slot state directly, which is the best signal of the four. vLLM's `/v1/models` includes `max_model_len`. LM Studio I haven't verified and wouldn't guess at.

## Two Ollama scheduler behaviours that will skew a concurrency-sensitive harness

Both are things we had to mirror explicitly and neither is visible from the client side.

**Ollama's MLX runner decodes one request at a time**, regardless of `OLLAMA_NUM_PARALLEL`. Selection is per-model by `IsMLX()`, which is exactly `ModelFormat == "safetensors"` — not a flag. So a safetensors model serialises every request while a GGUF one doesn't, and nothing in the API tells you which you got.

**`sched.go` also forces `numParallel = 1`** for non-completion models (embedders) and for a list of architectures — `mllama qwen3vl qwen3vlmoe qwen35 qwen35moe qwen3next lfm2 lfm2moe nemotron_h nemotron_h_moe nemotron_h_omni` at the version we last checked. If your harness sends concurrent requests and reasons about timing, those models behave differently from the rest with no indication. The list changes between releases, so it's worth re-reading `sched.go` on each upgrade rather than hardcoding ours.

## One suggestion on ordering

Your option 5 ("record the context the server used") is listed last and I'd argue it belongs first. It's the cheapest to build, it has no false-positive surface, and every other option becomes easier to validate once you have the recorded window next to the recorded token count in the same artifact. Options 3 and 4 are both "compare two numbers" checks — and they're much easier to threshold correctly when you already have a few days of both numbers on real runs to calibrate against.

The thing I'd most want in your `models.json`, from having needed it and not had it: the window **requested** alongside the window the server **reported**. When those disagree, that single pair explains most of this class of problem instantly, and nothing else in a log does.
