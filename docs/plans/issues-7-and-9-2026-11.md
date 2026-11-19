# Issues #7 (Homebrew on macOS 27) and #9 (GPU memory on AMD APUs)

**Created:** 2026-11-19 · **Status:** #9: override released in 0.12.8; **detection built on `main`, unreleased** (steps 1-8 done, 2253 tests), **verified on real NVIDIA hardware 2026-11-19** (below); the AMD half held until the reporter's `mem_info_*` and `inference compute` output confirm the reading matches what Ollama uses. Step 9 (site) ships with that release. #7: replied 2026-11-19, troubleshooting entry shipped in 0.12.8, close if no reply by 2026-12-03. Replies: [#9](https://github.com/geeks-accelerator/ollama-herd/issues/9#issuecomment-7013240786), [#9 follow-up](https://github.com/geeks-accelerator/ollama-herd/issues/9#issuecomment-7014640317), [#7](https://github.com/geeks-accelerator/ollama-herd/issues/7#issuecomment-7013241316).
**Issues:** [#7](https://github.com/geeks-accelerator/ollama-herd/issues/7) (open since 2026-10-05,
waiting on the reporter since 2026-10-26) · [#9](https://github.com/geeks-accelerator/ollama-herd/issues/9)
(opened 2026-11-19, no reply yet).

**Replies are drafts below. Nothing is posted to GitHub without the maintainer's approval**: a
comment goes out under their name.

---

## #9: herd refuses models that Ollama runs, on an AMD APU with a large GPU carve-out

### What the reporter has

Ubuntu 26.04.1, Python 3.14.4, herd 0.12.6, Ollama 0.40.16, four nodes, one of them a **GMKtec
EVO-X5 Pro**: AMD Ryzen AI Max+ PRO 495 ("Gorgon Halo"), Radeon 8065S iGPU, **192 GB** LPDDR5X, of
which the BIOS (*UMA Frame Buffer Size*) can give **up to 160 GB to the GPU**. 192 − 160 ≈ 32 GB,
which is the "~30 GB" herd shows: they have set a large carve-out. Ollama runs 100 GB+ models on the
iGPU; herd will not route them there.

### Why herd refuses (code, 2026-11-19)

- `common/system_metrics.get_memory_metrics()` reports only `psutil.virtual_memory()`: OS RAM.
  Nothing in herd reads GPU memory anywhere (no `rocm`, `amdgpu` or `mem_info_vram` in `src/`).
- `server/scorer._eliminate()`: a model that is not loaded must satisfy
  `available_gb >= _model_fit_cost_gb(model)`, or the node is eliminated for that request
  ("insufficient memory"). `_score_memory_fit()` scores on the same number.
- A BIOS carve-out is firmware-reserved and **excluded from `/proc/meminfo` MemTotal**, so on this
  machine the gate compares a 100 GB model with ~30 GB and the node can never cold-load it. A model
  Ollama already has loaded is not gated, which is why "herd can see the node, read the node, but
  will not run the model".
- This is a known gap: the website's positioning notes already say "memory fit uses system RAM,
  not VRAM". It is restrictive on carve-out APUs and merely
  permissive on discrete GPUs (a 64 GB RAM + 24 GB GPU box passes today because RAM is large).

### Facts the fix rests on (research 2026-11-19: kernel amdgpu docs and source, ROCm docs, Ollama
v0.40.16 `discover/`, LocalAI `pkg/xsysinfo/gpu.go`)

- **amdgpu sysfs** (`/sys/class/drm/card*/device/`): `mem_info_vram_total`/`_used` is the carve-out
  (on a discrete card, its VRAM); `mem_info_gtt_total`/`_used` is the GTT pool.
- **VRAM/carve-out is outside MemTotal; GTT is ordinary system RAM, inside MemTotal** and competing
  with `MemAvailable`. GTT free must never be added on top of `MemAvailable`.
- **Small carve-out + large GTT** (`ttm.pages_limit`, AMD's `amd-ttm` tool) is the other common
  Strix/Gorgon Halo setup. There MemTotal is already large and herd sees the memory today.
- **Ollama has no device-memory endpoint.** `/api/ps` has per-model `size_vram` only; the device
  report is the startup log line `inference compute … type=iGPU total="X GiB" available="Y GiB"`.
  So herd must read the hardware itself.
- **NVIDIA:** `nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free
  --format=csv,noheader,nounits` (MiB, one row per GPU). Missing binary or no devices (a container
  without `--gpus`) means "no GPU information", never "0 GB". DGX Spark/GB10 (unified) reports
  "Not Supported": use `/proc/meminfo` there, as today.
- **Prior art:** LocalAI reads the same sysfs files (no `rocm-smi` dependency) and treats a device
  as an APU when `gtt_total >= 8 × vram_total`. GPUStack uses VRAM only and closed an APU fix
  unmerged.

### The fix

One idea: **the memory a model can be loaded into = OS `MemAvailable` + free dedicated GPU memory
that the OS does not count.** Dedicated means VRAM: an AMD card's or APU carve-out's
`vram_total − vram_used`, and NVIDIA's `memory.free`. GTT is never added (it is already in RAM).
This is correct for all three shapes:

| Machine | Today | With the fix |
|---|---|---|
| APU, 160 GB carve-out, 32 GB to Linux | ~30 GB: refuses big models | ~30 + up to 160 GB |
| APU, small carve-out + large GTT | Full RAM (already right) | Same (+ ~0.5 GB carve-out) |
| 64 GB RAM + 24 GB NVIDIA | 64 GB (permissive) | 64 + 24 GB (Ollama splits layers across both) |
| Apple Silicon | Unified RAM (right) | Unchanged: no sysfs, no `nvidia-smi` |

1. **Node: detect.** New `common/gpu_memory.py`: Linux only. amdgpu from sysfs (no new
   dependency, works inside containers because `/sys` is visible), NVIDIA from `nvidia-smi` with a
   2 s timeout (the collector already shells out to it for the chip name). Returns a list of
   `{name, kind (discrete|integrated|unknown), total_gb, free_gb, source}`. Errors and absence
   return an empty list, logged once at DEBUG.
2. **Node: override, which is what the reporter asked for.** `FLEET_NODE_GPU_MEMORY_GB`: memory
   models can use beyond what the OS reports. When set it replaces detection, for platforms we
   cannot read (Windows, an unknown driver) and for anyone whose numbers disagree with what Ollama
   really uses. Documented in `docs/configuration-reference.md`.
3. **Heartbeat.** `MemoryMetrics` gains `gpu_total_gb` / `gpu_free_gb` (default 0, "not reported")
   plus the per-device list. `models/node.py` has no `extra="forbid"`, so mixed versions are safe
   both ways: an old router ignores the fields, an old node omits them.
4. **Router: one definition.** `NodeState.model_memory_available_gb()` = `available_gb +
   gpu_free_gb` (and a matching total). 20 files read `memory.available_gb`/`total_gb`; switch only
   the **"will a model fit here" decisions**, checked 2026-11-19:
   - `scorer._eliminate` (the gate that refuses the reporter's models) and `_score_memory_fit`;
   - `model_preloader` (picks the node with the most free memory, then checks the fit, ~line 500);
   - `routes/routing.py` (~line 800, the fallback's fit check);
   - `model_recommender` (sizes recommendations from `total_gb`) and `model_knowledge.models_fitting_ram`;
   - `benchmark_engine` / `benchmark_runner` (KV-cache budget and pull limits from `available_gb`).
   Everything else keeps meaning RAM: the dashboard's RAM bars, `health_engine`'s RAM checks
   (~lines 451 and 1765), and telemetry.
5. **Memory pressure stays about RAM.** On a carve-out machine the OS has ~30 GB; if it is
   pressured, cold loads are withheld as today. Watch for whether that bites the reporter; change
   it only on evidence.
6. **Dashboard:** the node card shows the GPU pool next to RAM ("GPU 152 / 160 GB free",
   source in a tooltip), so the operator can see what herd counts.
7. **Telemetry: unchanged.** `devices[].memory_gb` stays system RAM, because the `/telemetry`
   page is the contract and the leaderboard ranks it. A separate `gpu_memory_gb` field is a later,
   server-first decision (`extra="forbid"` rejects the whole payload on an unknown key), not part
   of this fix.
8. **Tests:** fake sysfs trees for the three AMD shapes, `nvidia-smi` output fixtures (one GPU,
   two, "Not Supported", missing binary, timeout), the override winning, and a scorer test where a
   100 GB model is eliminated on RAM alone and survives with a 160 GB carve-out.
9. **Site, after release only:** the positioning facts change from "memory fit uses system RAM, not
   VRAM" to "memory fit counts dedicated GPU memory (NVIDIA VRAM, AMD carve-out)". The honesty rule
   still holds for everything else: herd does not read GPU *utilisation* and does not route by GPU
   load. `stale_audit.py` should learn the new phrasing.

### Built (2026-11-19, unreleased)

- `common/gpu_memory.py`: amdgpu sysfs (`mem_info_vram_*`, connectors and non-amdgpu cards skipped,
  GTT never counted) and `nvidia-smi` (`[N/A]` skipped, any failure = no GPU). Linux only.
- The setting is now `float | None`: unset = detect, a number = override, `0` = off (a change from
  0.12.8, where unset and 0 both meant none; in the changelog).
- `MemoryMetrics` carries `gpu_used_gb`, `gpu_source` (`detected` | `override` | ""), `gpu_devices`.
  `NodeState.gpu_memory_free_gb()`: detected uses measured use; override subtracts `size_vram`.
- Fit decisions now also cover `model_recommender` (sort and usable RAM) and the benchmark engine and
  runner (via `/fleet/status`'s new `model_memory_available_gb`); the report fields stay RAM.
- Dashboard node card: "GPU X free of Y", "(set)" for an override. Checked in a browser.
- Docs: configuration reference, troubleshooting, Docker guide (NVIDIA memory needs `--gpus`).
- Not done on purpose: memory pressure stays RAM-only (step 5); telemetry unchanged (step 7).

### Tested on real NVIDIA hardware (2026-11-19)

A Lambda Cloud `gpu_1x_rtx6000` (Quadro RTX 6000, 22.5 GiB; 54 GB RAM, 52.8 GB available; Ubuntu
22.04; driver 580; Docker 29.9 with the NVIDIA Container Toolkit; Ollama installed by its script).
Router and node on that machine, telemetry off. The test model, `llama3.3:70b-instruct-q6_K`
(57 GB), is bigger than free RAM and smaller than RAM plus the GPU.

| Test | Result |
|---|---|
| A. Released 0.12.8, no setting | **The bug, on NVIDIA:** node eliminated for "insufficient memory", `503` after the 30 s hold, GPU idle (1 MiB used) |
| B. `main` (`81a220f`), no setting | `nvidia-smi` detected: 22.5 GB, 0.16 used; fit figure **74.89 GB**; `200` from `gpu-node` in 163 s (loading 59 GB); Ollama ran it **63%/37% CPU/GPU** |
| B, after the load | detected use 20.77 GB = `nvidia-smi` 21,270 MiB; Ollama's `size_vram` 20.58 GB (the 0.2 GB gap is the CUDA context, which detection rightly counts); fit figure fell to 53.0 GB |
| C. Container with `--gpus all` | chip "AMD EPYC-Rome Processor + Quadro RTX 6000", detection inside the container (`nvidia-smi`), `200` from `gpu-container` |
| D. Container without `--gpus` | no `nvidia-smi`, so no GPU reported (`gpu_source` ""), chip shows the CPU only: absent, not a wrong figure |

So the NVIDIA half of detection is verified end to end. The AMD half rests on the kernel's
documented sysfs files and unit tests until the reporter's output arrives.

### Unknowns to settle with the reporter before relying on the numbers

- Their four `mem_info_*` values and `free -g`, to confirm the carve-out reading.
- Ollama's `inference compute` line: whether it chose ROCm or Vulkan, and what total/available it
  sees. (Vulkan on an iGPU sums carve-out and GTT; ROCm reports `max(vram, gtt)`.) The fix's
  number should be within a few GB of Ollama's.
- Whether the 495 is gfx1151. Ollama drops iGPUs by default except ROCm gfx1151, so they may have
  set `OLLAMA_IGPU_ENABLE`; worth knowing for the docs.

### Draft reply to #9 (for approval)

> Thanks for the clear report, and you're right: this is a herd limitation, not your setup.
>
> Before loading a model, herd checks that it fits in the node's free memory, and it only reads the
> memory Linux reports. On the EVO-X5 Pro the BIOS gives a large share of the 192 GB to the GPU
> (the UMA frame buffer), and that share is invisible to Linux, which is why herd sees about 30 GB
> while Ollama happily runs 100 GB+ models on the GPU. A model Ollama already has loaded still
> routes, which matches what you see.
>
> The fix we're planning: herd reads the GPU's dedicated memory from the amdgpu driver
> (`/sys/class/drm/card*/device/mem_info_vram_*`, no extra tools needed) and counts it in the fit
> check, plus a `FLEET_NODE_GPU_MEMORY_GB` setting to set it by hand, which is the override you
> asked for.
>
> To make sure the numbers match what Ollama really uses, could you share the output of these on
> that machine?
>
> ```
> free -g
> grep . /sys/class/drm/card*/device/mem_info_{vram,gtt}_{total,used}
> journalctl -u ollama --no-pager | grep "inference compute" | tail -2
> ```
>
> (If Ollama doesn't run under systemd, the `inference compute` line is near the top of its log.)

---

## #7: `brew install ollama-herd` fails on macOS 27

### Where the thread stands

- The build fails installing `aiosqlite`'s build backend (`flit_core`) under Homebrew's
  `pip --no-binary :all:`. Not reproduced on macOS 26.3.1 / Homebrew 7.0.7 with cold caches.
- The reporter's log showed the build hook could not import **`json`** or **`traceback`**
  (standard library). Their checks then showed Python works with `-I -S`, `-S` and `-E -S`: the
  failure needs normal **site startup**. Suspect: a stray `google_generativeai-…-py3.13-nspkg.pth`
  in Homebrew's **python3.12** site-packages, which `.pth` processing runs at every startup.
- 2026-10-26 we asked for three commands (plain `import json`, `python -m site`, the `.pth`
  list). **No answer in 24 days.**
- The reporter's real goal was different: "My old M1 had the server running. The other M5 was
  connected, but couldn't execute tasks." They installed with pip before and it worked.

### New facts since (2026-11-19)

- **0.12.7 installs cleanly from a fresh, untrusted tap on macOS 27.2 with Homebrew 7.0.14** (step
  15 of the release: 12 min warm, 40 min cold). So macOS 27 itself is not the problem.
- On this Mac, after the macOS 27 upgrade, Homebrew refused **all** source builds until the
  Command Line Tools were updated: `Error: Your Command Line Tools are too outdated.`
  `xcode-select --install` only says they are installed; the update is in Software Update
  (`softwareupdate --list` shows "Command Line Tools for Xcode 27.x"). That is a different error
  from #7's, but anyone upgrading to macOS 27 will hit it, and it belongs in the docs.

### Plan

1. **Reply** (draft below): share that 0.12.7 installs on macOS 27.2, keep the `.pth` hypothesis
   and the three commands, give the safe test (move the file aside, not delete), and offer the path
   that already worked for them (`uv tool install ollama-herd` or `pipx`). Ask separately about the
   M5 that "couldn't execute tasks" on 0.12.7, with `/fleet/status`, since several causes of that
   symptom have been fixed since 0.10.0 (a node advertising an address the router cannot reach, the
   node id changing with the network, `FLEET_NODE_OLLAMA_HOST` ignored from the CLI).
2. **Docs, regardless of #7:** a "Homebrew refuses to build after a macOS upgrade" entry in
   `docs/troubleshooting.md` and on ollamaherd.com `/download` troubleshooting, with the
   `softwareupdate` command. Also a line in the release checklist (already added to `CLAUDE.md`).
3. **If no reply in two weeks** (2026-12-03): close as "not reproducible on macOS 27.2 with herd
   0.12.7", with an invitation to reopen. The pip/uv path works for them, so closing loses nothing.

### Draft reply to #7 (for approval)

> Picking this back up with some news: herd 0.12.7, released today, installs cleanly from a fresh tap
> on macOS 27.2 with Homebrew 7.0.14, so macOS 27 itself isn't the blocker.
>
> Two things worth trying:
>
> 1. If you upgraded to macOS 27 recently, check `softwareupdate --list` for "Command Line Tools for
>    Xcode 27". Homebrew needs current ones to build from source.
> 2. The earlier checks pointed at something in Python's startup. If
>    `/opt/homebrew/opt/python@3.12/bin/python3.12 -c 'import json; print("ok")'` fails, move the
>    `google_generativeai-…-py3.13-nspkg.pth` file out of
>    `/opt/homebrew/lib/python3.12/site-packages/` (move it, don't delete it) and retry
>    `brew install ollama-herd`.
>
> Or skip Homebrew: `uv tool install ollama-herd` (or `pipx install ollama-herd`) installs the same
> thing in its own environment.
>
> You also mentioned the M5 connected but couldn't run tasks. Several causes of exactly that have
> been fixed since 0.10.0. If it still happens on 0.12.7, could you paste the output of
> `curl -s http://<router>:11435/fleet/status` while the M5 is connected? That shows what the
> router thinks of it.

---

## Order of work

1. Get the maintainer's OK on both replies; post them.
2. #7 docs entry (OSS troubleshooting, site `/download`), independent of the reporter.
3. #9 steps 1-8 on OSS `main`, with tests. Not released until the reporter's numbers confirm the
   reading, unless the override alone (step 2) is worth shipping first: it unblocks them today with
   one setting and carries no detection risk.
4. #9 step 9 (site) only with the release that ships it.
