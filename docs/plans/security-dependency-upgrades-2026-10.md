# Security dependency audit and upgrade plan — 2026-10-22

**Status:** PLANNED. Nothing below has been changed yet.
**Trigger:** PHP and Node.js published security releases on 2026-10-21, and we wanted to know our exposure.
**Revised 2026-10-22** after a codebase audit (§ "Codebase audit"). The audit swapped two pieces of planned new tooling for things that already exist, cut Phase 1 to a single call site, and turned up one piece of dead code to delete.

## TL;DR

- **PHP2Shell (CVE-2026-109618/109619) does not affect this repo.** No PHP code ships here and PHP isn't installed on this box. Per commit `5982a9d`, the *site* repo has its own upgrade plan, so check PHP there.
- **The Node.js October release does not affect this repo either.** herd has no Node runtime dependency. This box's Homebrew Node is already `v26.12.1`, the patched 26.x release.
- **The real exposure comes from Python, and from two places:**
  1. **Pillow 12.3.0 has 28 advisories (3 CRITICAL), all fixed in 13.0.0.** We can't upgrade, because `fastembed 0.9.0` (the latest release) caps `pillow<13`. Pillow decodes images that clients upload to the vision embedding server, which listens on `0.0.0.0:11438` with no auth. **This is the one reachable from the network.**
  2. **The Homebrew formula pins versions with 25 known vulnerabilities across 9 packages,** including Starlette, python-multipart and zeroconf, all of which sit on herd's network surface. pip users already resolve fixed versions. Every Homebrew user runs the vulnerable pins today.
- The dev venv (`uv.lock`, gitignored) is clean except for Pillow.

## Ground rules for this work

These come from CLAUDE.md § Design Principles, applied to this plan:

- **No feature gating.** No `FLEET_*` switch for the image-format restriction and no "strict mode". The restricted decoder *is* the behaviour, and the allowed formats are a module constant, the way scorer weights are class constants.
- **No second code path.** The format restriction goes through the existing decode-error branch and the router's existing 4xx passthrough. Nothing parallel gets built.
- **Reuse before build.** Every phase below names the existing helper, tool or pattern it uses. Where the first draft proposed something new, the audit found an existing equivalent, and the plan now uses it.
- **Temporary things carry their removal condition** in `docs/issues.md`, not in a TODO comment.

## How this was measured

```bash
# Locked venv (all extras): what pip/uv users get today
uv export --all-extras --no-hashes --format requirements-txt > reqs.txt
uvx pip-audit -r reqs.txt --no-deps --disable-pip          # pip-audit 2.10.1

# Homebrew formula: what brew users get today
gh api repos/geeks-accelerator/homebrew-ollama-herd/contents/Formula/ollama-herd.rb \
  --jq .content | base64 -d > formula.rb
# extract name==version from each resource URL → brew-reqs.txt, then:
uvx pip-audit -r brew-reqs.txt --no-deps --disable-pip

# Pillow severities
curl -s -X POST https://api.osv.dev/v1/query \
  -d '{"package":{"name":"pillow","ecosystem":"PyPI"},"version":"12.3.0"}'
```

The two pip-audit runs report different counts for Pillow: the locked venv run lists 24 CVE IDs, and OSV returns 28 GHSA advisories. The ID schemes overlap only partly, so read both counts as "a lot, three critical".

## Findings

### F1 — Pillow 12.3.0: 28 advisories, reachable over the LAN (HIGHEST)

| Severity | Count | Examples |
|---|---|---|
| CRITICAL | 3 | heap overflow in `ImagingJpegDecode`; argument injection in the Ghostscript (EPS) call → RCE; heap overflow in the TIFF write path |
| HIGH | 16 | OOB read in TIFF decode leaking heap memory, GIF/APNG/MSP decompression-bomb bypasses, MIC infinite loop, PDF recursion |
| MODERATE | 9 | XBM ReDoS, quadratic GIF comment handling, uninitialised memory reads |

**Why it is reachable:** `node/embedding_server.py:98` calls `Image.open(io.BytesIO(raw))` on client-supplied bytes. The call doesn't restrict `formats`, so every Pillow decoder is live, including EPS, TIFF, MIC, MSP, XBM and GIF. The server binds `0.0.0.0` (`node/agent.py:494`) because the router reaches it over the LAN, and it has no auth. Anyone on the LAN can reach it, as can anyone the router forwards for. The bind is a design requirement and stays as it is.

**Why we're still on 12.3:** `fastembed 0.9.0` declares `pillow<13.0` (and `huggingface-hub<2.0`). Pillow 13.0.0 came out 2026-10-16. Our own code doesn't use fastembed's image path: vision embeddings run on our own ONNX code in `embedding_models.py`. The cap only matters to the resolver.

**Who is affected:** anyone who installs with `--extra embedding` and runs the vision embedding server. The Homebrew formula ships no Pillow resource.

### F2 — The Homebrew formula pins 25 known vulnerabilities (HIGH)

| Package | Brew pin | Vulns | Fixed by | Latest | Where herd uses it |
|---|---|---|---|---|---|
| starlette | 1.0.0 | 5 | 1.3.1 | 1.9.0 | every HTTP request; **imported directly** in `server/cors.py` |
| python-multipart | 0.0.24 | 5 | 0.0.31 | 0.0.32 | `routes/transcription_compat.py` (audio uploads), its only user |
| zeroconf | 0.148.0 | 5 | 0.149.16 | 0.151.5 | `common/discovery.py`: parses mDNS from the LAN, in router and node |
| cryptography | 47.0.0 | 4 | 50.0.0 | 50.0.2 | platform signing (opt-in) |
| anyio | 4.13.0 | 2 | 4.14.2 | 4.15.1 | async runtime |
| certifi | 2026.2.25 | 1 | 2026.10.19 | 2026.10.19 | outbound TLS trust store |
| idna | 3.11 | 1 | 3.15 | 3.20 | hostname handling |
| json-repair | 0.59.5 | 1 | 0.60.1 | 0.64.1 | `server/tool_call_repair.py:252`: parses model output |
| pydantic-settings | 2.13.1 | 1 | 2.14.2 | 2.16.1 | env/config parsing |

Another 21 resources are behind with no known advisories, and eight are current. The formula's resource set has also drifted from the real dependency closure:

- **Missing `requests`, `urllib3`, `charset-normalizer`.** `tiktoken` requires `requests`. Without it, `anthropic_translator.estimate_tokens()` hits its `except Exception` and silently falls back to `len(text) // 4`. This is unverified because no brew install exists on this box.
- **Missing `opentelemetry-api`**, which current FastAPI requires.
- **Missing `httptools`, `uvloop`, `watchfiles`**, which `uvicorn[standard]` declares.
- **`sniffio` is no longer needed** (`anyio` ≥ 4.14 dropped it).

All four have the same root cause: the formula is curated by hand. Phase 3 replaces that with Homebrew's own generator.

### F3 — Chart.js loaded from a CDN without a pinned version or integrity hash (LOW)

`server/routes/dashboard.py` L1681/1692/1704/1715 each repeat the literal `<script src="https://cdn.jsdelivr.net/npm/chart.js@4">`, a floating major version with no `integrity=`. It is the **only** external script herd serves. Chart.js has no known advisory, so this is supply-chain hygiene. Latest is `4.5.1`.

### F4 — Non-security drift (INFO)

`huggingface-hub` 1.33.0 → 2.2.0 (held back by the same fastembed cap), `pycparser` 3.11 → 3.12.

## Codebase audit: what each phase reuses

| Need | Existing thing to use | Location |
|---|---|---|
| Reject image formats | the existing decode-failure `400` branch: same `{"error": …}` shape, no new response type | `node/embedding_server.py:95–103` |
| Get the 400 to the client | router already passes a node's status and body through, adding `node` | `routes/embedding_compat.py:164–184` (the comment there records why a 400 must not become a 502) |
| Name the constant | the `ALLOWED_*` module-constant convention | `server/cors.py:48`, `node/daily_rollup.py:31`, `node/anonymous_rollup.py:42` |
| HTTP-level test of a router | `with TestClient(app) as c:` | `tests/test_server/test_native_text_proxy.py:28` and four others |
| Regenerate formula resources | **`brew update-python-resources`**, built into Homebrew (7.0.10 here) | replaces the hand rebuild in the first draft |
| Run a vulnerability audit routinely | **`scripts/health.sh`** with its `record_pass` / `record_fail` helpers; the release already gates on it | replaces a free-floating command in the first draft |
| Track a temporary override | `docs/issues.md` (OPEN, with removal condition) | per CLAUDE.md conventions |
| Single source for repeated `<head>` markup | module constants beside `_FAVICON_SVG`, passed via `_dashboard_page(extra_head=…)` | `routes/dashboard.py:120`, `:2048` |

**Also found in the audit:**

- **Dead code to delete:** `StreamingProxy.make_embedding_process_fn` (`server/streaming.py:1018–1048`) is a queued vision-embedding path that nothing calls. The only reference is `tests/test_server/test_vision_embedding.py:383`, which just checks that it exists with `hasattr`. If it were ever revived it would count client 400s as node failures (`mark_failed` plus an ERROR log), so delete it with Phase 1 rather than leave a wrong path lying around.
- **Vision embedding requests are never traced.** `embedding_compat.py` makes no `record_trace` call, while `text_embedding_compat.py` makes four. No local traffic exists either: the node log has no `Embedded N image(s)` lines. So there is **no data on which formats clients send**. The allowed list below is chosen from what embedding clients produce, not measured. **Fixed 2026-10-22, ahead of this plan:** vision embeds now leave a trace for every outcome (CHANGELOG `[Unreleased]`, tags `embed` + `vision-embed`), so after Phase 1 ships, rejected formats show up as `failed` rows carrying `HTTP 400` on the serving node.
- **`scripts/health.sh` has `EXPECTED_MIN_TESTS=212`**, a stale floor far below the real suite size. It does no harm, since check 4 compares the exact count against CLAUDE.md, but it's meaningless. Fix it in the Phase 6 edit since that edit touches the file anyway.

## Plan

Ordered by exposure removed per unit of risk. Phases 1, 2, 4, 5 and 6 ship as `0.11.3`. **Phase 3 is a tap-only commit** that needs no PyPI republish (per CLAUDE.md), **so do it first.** It's the largest exposure and doesn't depend on the code changes.

### Phase 1 — Shrink Pillow's reachable surface (one call site)

```python
# node/embedding_server.py
from PIL import Image, UnidentifiedImageError

# Decoders exposed to network input.  Everything else Pillow can parse
# (EPS via Ghostscript, TIFF, GIF, MSP, MIC, XBM, ...) carries most of its
# advisories — see docs/plans/security-dependency-upgrades-2026-10.md.
ALLOWED_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP")
...
        try:
            raw = base64.b64decode(b64)
            pil_images.append(Image.open(io.BytesIO(raw), formats=ALLOWED_IMAGE_FORMATS))
        except UnidentifiedImageError:
            return JSONResponse(
                {"error": f"Image {i} is not one of {', '.join(ALLOWED_IMAGE_FORMATS)}"},
                status_code=400,
            )
        except Exception as exc:  # existing branch, unchanged
```

- A tuple, not a frozenset: Pillow tries the formats in order. It's a module constant, not a setting.
- **Verified on the installed Pillow 12.3.0:** JPEG, PNG and WebP open; GIF, TIFF and BMP raise `UnidentifiedImageError`. Pillow's own message is `cannot identify image file <_io.BytesIO …>`, which tells the client nothing, so the separate `except` names the accepted formats.
- **No router change.** `embedding_compat.py` already returns the node's 400 and body unchanged, with `node` attribution added.
- **This doesn't fix the critical `ImagingJpegDecode` overflow.** Only Pillow 13 does (Phase 2).
- **Delete** `make_embedding_process_fn` and its `hasattr` test (see audit).
- **Tests** go in `tests/test_server/test_vision_embedding.py::TestEmbeddingServer`. Use the `TestClient(app)` pattern on a bare `FastAPI()` that includes `embedding_server.router`, and monkeypatch `_get_backend` to a stub so no model loads. Cover: JPEG, PNG and WebP accepted; TIFF and GIF give a 400 naming the formats; non-base64 input still gives the existing 400.
- **Docs:** add the accepted formats to the vision row of `docs/api-reference.md` (~L602), and add a CHANGELOG `[Unreleased]` entry under *Security*.

### Phase 2 — Get to Pillow 13

1. **Upstream:** look at `qdrant/fastembed`'s issue tracker first, since this has probably been reported already. Then comment on the existing issue, or open an issue/PR to lift `pillow<13` and `huggingface-hub<2`.
2. **Local fleet and dev, now:**
   ```toml
   [tool.uv]
   # Remove when fastembed lifts pillow<13 — tracked in docs/issues.md.
   override-dependencies = ["pillow>=13.0.0"]
   ```
   This only affects `uv` resolution inside this repo and doesn't change the wheel's metadata. **Gate:** the full suite passes, then exercise both native servers live: `/api/embed` with `nomic-embed-text` (fastembed declared the cap and the tests mock most of it) and one vision embed. Confirm the vision one from its `request_traces` row (`tags` contains `vision-embed`, `status='completed'`).
3. **Add an OPEN entry to `docs/issues.md`:** "Pillow pinned below 13 for PyPI users by fastembed's cap." Give it the removal condition (a fastembed release without the cap → delete the override, and consider raising `Pillow>=13` in both the `embedding` and `dev` extras).
4. **PyPI users:** we can't override a dependency's cap from a library. Until fastembed releases, Phase 1 is their mitigation. The CHANGELOG entry should say so.
5. **Rejected: a health check that warns when Pillow is below 13.** It would fire on every embedding node with no action available. That is the "standing WARNING with no action" pattern CLAUDE.md warns against.

### Phase 3 — Regenerate the Homebrew formula with Homebrew's own tool (tap-only, first)

The first draft proposed rebuilding all 39 resource blocks by hand from `uv export`. Homebrew already does exactly that:

```bash
cd "$(brew --repository geeks-accelerator/ollama-herd)"
brew update-python-resources ollama-herd --print-only   # review first
brew update-python-resources ollama-herd
```

- It resolves the closure of the formula's own sdist (0.11.2 on PyPI) and writes every `resource` block. That fixes the missing `requests`/`urllib3`/`charset-normalizer`/`opentelemetry-api` and the stale `sniffio` without hand curation.
- **Accept its output as written, including `uvicorn[standard]`'s `httptools`/`uvloop`/`watchfiles`.** Leaving them out would be a hand-maintained divergence from what `pyproject.toml` declares, which is the very thing that went stale. `depends_on "rust" => :build` is already present for `watchfiles`.
- **Watch its release cooldown:** it skips very recent releases, and `certifi 2026.10.19` was three days old at audit time. That's why the next step is required.
- **Then audit the result** with the F2 command and expect **0** vulnerabilities. If the cooldown kept anything vulnerable, bump that one block by hand and note it in the commit.
- Then run release-checklist steps 13–15 with `brew untrust --tap` first, plus:
  ```bash
  /opt/homebrew/Cellar/ollama-herd/0.11.2/libexec/bin/python -c \
    "import tiktoken; print(len(tiktoken.get_encoding('cl100k_base').encode('hello world')))"
  ```
  This must print a token count without error. It proves the `requests` gap is closed.
- The main `url`/`sha256` stay at 0.11.2.

### Phase 4 — Raise pyproject floors for packages that parse untrusted input

`5982a9d` kept the floors deliberately (*"raising a library's floors forces every user to upgrade"*). For security fixes on code that parses network input, forcing the upgrade is the point. I recommend raising **only** these four:

| Package | Floor now | Proposed | Why this one |
|---|---|---|---|
| `starlette` (**add**) | — | `>=1.3.1` | `server/cors.py` imports it directly, so it should be declared anyway. Today it arrives only through FastAPI. |
| `python-multipart` | `>=0.0.9` | `>=0.0.31` | parses transcription uploads |
| `zeroconf` | `>=0.136.0` | `>=0.149.16` | parses mDNS packets from anyone on the LAN |
| `json-repair` | `>=0.29` | `>=0.60.1` | parses model output (`tool_call_repair.py`) |

Before raising each floor, read its advisories and confirm herd actually reaches the vulnerable path. Drop any package whose advisories herd doesn't reach. Leave `cryptography`, `certifi`, `idna`, `anyio` and `pydantic-settings` alone: a fresh resolve already picks fixed versions, and none of them parses attacker input in herd's main path.

### Phase 5 — One pinned, integrity-checked Chart.js constant

```python
# routes/dashboard.py, beside _FAVICON_SVG
_CHART_JS_HEAD = (
    '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js" '
    'integrity="sha384-…" crossorigin="anonymous"></script>'
)
```

The four `extra_head=` call sites use `_CHART_JS_HEAD`, replacing four copies of a literal. Generate the hash from the exact file: `curl -s <url> | openssl dgst -sha384 -binary | openssl base64 -A`. Verify in the browser preview that all four chart pages render and the console shows no integrity error.

### Phase 6 — Put the audit where the release already looks

Add check 6 to `scripts/health.sh`, written in its existing style:

```bash
# 6. No known vulnerabilities in the locked dependency set
echo "Auditing dependencies..."
AUDIT_REQS=$(mktemp)
uv export --all-extras --no-hashes --format requirements-txt > "$AUDIT_REQS"
AUDIT_OUTPUT=$(uvx pip-audit -r "$AUDIT_REQS" --no-deps --disable-pip 2>&1) && AUDIT_EXIT=0 || AUDIT_EXIT=$?
rm -f "$AUDIT_REQS"
if [ "$AUDIT_EXIT" -eq 0 ]; then
    record_pass "No known vulnerabilities (pip-audit)"
else
    record_fail "$(echo "$AUDIT_OUTPUT" | grep -oE 'Found [0-9]+ known vulnerabilit[a-z]+ in [0-9]+ packages?')"
fi
```

- **While Phase 2's override is in place, Pillow is at 13 locally, so this check passes honestly.** Without the override it would fail on every run until fastembed releases. That's another reason Phase 2.2 comes before Phase 6.
- Also fix `EXPECTED_MIN_TESTS=212` (audit finding).
- In CLAUDE.md's release checklist, add a step after `ruff check`: *"`./scripts/health.sh` — includes the dependency CVE audit."* At step 12, add *"`brew update-python-resources`, then pip-audit the formula's pins"*. **F2 happened because nothing ever audited what brew users install.** The last three releases bumped only the formula's version and sha256, so the pinned transitive versions grew months old.

## Greenfield check

| Principle | How the plan meets it |
|---|---|
| No `FLEET_*` on/off flags | none added; the format list is a module constant |
| No second code path | format rejection uses the existing 400 branch and the existing router passthrough; the dead queued path is *removed* |
| Reuse before build | Homebrew's own resource generator, the existing `health.sh` harness, the existing `TestClient` pattern, the `ALLOWED_*` and `_FAVICON_SVG` constant conventions |
| Minimal debt | one temporary item (the uv override), with its removal condition in `docs/issues.md`; four copies of the Chart.js tag become one constant |
| Fail loud | rejected formats get a 400 that names what's accepted; the CVE audit fails `health.sh` rather than printing a warning |

## Release

**Now:** Phase 3 as a tap commit.

**`0.11.3` (patch):** Phases 1, 2, 4 (the floors that survive the reachability review), 5 and 6. Follow the full checklist, including the soak. Phase 1 changes a path the soak won't exercise on its own, since this fleet has no vision traffic. Send at least one real vision embed and one rejected TIFF, and confirm both in `request_traces`: one `completed`, one `failed` with `HTTP 400`.

## Out of scope, worth a look elsewhere

- **Client 4xx count against the serving node's error rate** on every directly-proxied path (native text embed, rerank, and now vision). That matches how the LLM path records backend 4xx, and `high_error_rate`'s sample floor damps it. But a client that keeps sending a format Phase 1 rejects would show up as a node with a high error rate. Watch for it after Phase 1. Don't add a new status without evidence that it happens.
- **The site repo** (PHP/Node, if that's what it is). That's where PHP2Shell and the Node advisories would actually apply.
- **Global npm packages on this box** (`npm ls -g --depth=0`). The `openclaw` daemon from the August co-tenancy incident was a global npm install and is now disabled, not removed.
- **Ollama and `mlx-lm`** aren't Python deps of herd, but they parse the same untrusted prompts. `mlx-lm` is pinned exactly at 0.32.0 in `scripts/setup-mlx.sh`.

## Sources

- PHP2Shell: [Rapid7](https://www.rapid7.com/blog/post/etr-cve-2026-109618-and-cve-2026-109619-critical-unauthenticated-remote-code-execution-in-php/), [Tenable FAQ](https://www.tenable.com/blog/php2shell-cve-2026-109618-cve-2026-109619-frequently-asked-questions-about-critical-php-remote)
- Node.js October 2026 security release: [Cyber Kendra](https://www.cyberkendra.com/2026/10/nodejs-october-2026-security-releases-cve-2026-111497.html), [CloudLinux](https://blog.cloudlinux.com/node-js-october-2026-security-releases)
- Pillow advisories: OSV query above; fixed in Pillow 13.0.0 (released 2026-10-16)
