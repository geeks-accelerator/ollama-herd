# Dashboard color semantics — utilization is the product, not the problem

**Status:** Proposed — not yet scheduled
**Filed:** 2026-04-24
**Owner:** unassigned
**Risk:** Low code risk, moderate UX-decision risk (easy to ship a pretty but wrong palette)

## Motivation

The dashboard currently inherits a "busy = red, idle = green" color vocabulary from traditional server-ops tools (Datadog, Grafana, Prometheus). That vocabulary was invented for SaaS infrastructure where a spike could cost you uptime and headroom was the point.

**Our product thesis is the inverse.** The landing page says "your spare Mac is wasting compute." The research narrative is "my Mac sits idle 18 hours a day." The cooperative-compute framing treats high utilization as earned credits. The electricity math says idle 15W is pure loss; 80W under load is doing the thing you bought the hardware for.

Under our own framing, a Mac Studio at 5% CPU and 12% memory is the failure mode the product exists to solve. A Mac Studio at 95% memory serving real requests is success. But the current dashboard colors a fully-engaged Mac Studio in alarming pink/magenta and an idle one in reassuring green — it's actively contradicting the narrative the rest of the product tells.

**At the same time**, operators running fleets at 3am debugging weird failures do need to see when a node is memory-pressured, thermally throttled, or otherwise degraded. The fix isn't to throw away the warning signal — it's to split "high utilization" from "something concerning" into two independent dimensions.

## Current state

### Color variables (`src/fleet_manager/server/routes/dashboard.py`)

```css
--green:  #22c55e
--yellow: #eab308
--red:    #ef4444
--orange: #f97316
--blue:   #3b82f6
```

### The mismatched semantic — `barColor(pct)`

```javascript
function barColor(pct) {
  var h = 142 - (pct / 100) * 142;  // hue shifts green → red
  var s = 71 + (pct / 100) * 13;
  var l = 45 + (pct / 100) * 15;
  return 'hsl(' + h + ',' + s + '%,' + l + '%)';
}
```

Applied to `.cpu-bar` and `.mem-bar` on every node card. At 95% memory, the bar renders in near-red — communicating "something is wrong" when what's actually happening is "the product is doing its job."

### Scope — where `barColor` is actually called

Audited across the entire codebase (all HTML rendering lives in `dashboard.py` — no other templates, no external template engine). Four call sites split into two categories:

**Semantic-mismatch call sites (CHANGE these):**

| Page | Location | What it renders | Current semantic | Correct semantic |
|---|---|---|---|---|
| `/dashboard` | `dashboard.py:1911` | CPU bar SSE update | busy=red | blue→purple utilization |
| `/dashboard` | `dashboard.py:1913` | Memory bar SSE update | busy=red | blue→purple utilization |
| `/dashboard` | `dashboard.py:2010` | CPU bar initial render | busy=red | blue→purple utilization |
| `/dashboard` | `dashboard.py:2015` | Memory bar initial render | busy=red | blue→purple utilization |
| `/dashboard/recommendations` | `dashboard.py:4068` | Per-node "recommended RAM / usable RAM" bar | busy=red | blue→purple utilization |

**Keep-as-is call sites (DO NOT change):**

| Page | Location | What it renders | Why keep |
|---|---|---|---|
| `/dashboard` | `dashboard.py:1965` | Capacity learner availability score (already inverted: `barColor(100 - score*100)`) | High availability = low risk of interrupting a user's human work on that machine. Green-at-high and red-at-low IS correct here — it's a genuine user-preference signal, not a utilization metric. |
| `/dashboard/recommendations` | `dashboard.py:4073` | Per-node **disk** usage bar | Disk at 95% IS a warning — you're about to run out of disk space, which breaks things (model downloads fail, SQLite traces stop writing). Disk is a hard-ceiling capacity metric, not a product-value utilization metric. Busy-is-bad remains correct. |

**Pages that don't render utilization bars at all (no change needed):**

`/dashboard/trends`, `/dashboard/models`, `/dashboard/tags`, `/dashboard/benchmarks`, `/dashboard/health`, `/dashboard/settings`. These use Chart.js charts (categorical color palettes for differentiating lines/bars — no utilization-scale semantics), status tables, forms, or text. None use `barColor()`.

### What else is fine and should NOT change (non-barColor surfaces)

Beyond the `barColor` audit above, these color semantics are already correct:

- **Status dots** (`online` green / `degraded` yellow / `offline` red) — liveness, not utilization. A dead node is genuinely a problem.
- **Health badges** (info / warning / critical) — from the 18 health checks, represent real operational state.
- **Model chips type-coding** (ollama / mlx / image / stt / embed) — categorical, not ordinal. Purple-for-MLX doesn't imply "MLX is bad."
- **MLX server status table** (added 2026-04-24 with multi-MLX work) — `healthy` green / `starting` amber / `unhealthy` red / `memory_blocked` amber / `stopped` grey. Categorical-status semantics, not utilization.
- **Chart.js palettes** on `/dashboard/trends` and `/dashboard/models` — categorical line/bar differentiation, no utilization scale.

The only semantic flip is the five `barColor(pct)` call sites enumerated in the Scope table above: four on `/dashboard` (CPU + memory bars, two renders each: SSE update and initial render) and one on `/dashboard/recommendations` (node RAM bar).

## Design principles

1. **Two independent axes, not one combined scale.**
   - Axis A: **utilization intensity** — color saturation/brightness scales with how engaged the resource is. High = vivid, low = muted.
   - Axis B: **warning state** — a distinct visual channel (e.g. a tint, outline, or badge overlay) that fires only on genuinely concerning conditions.

2. **Celebrate earned utilization; stay neutral on idle.** Making an evaluator's dashboard feel bad because they haven't sent traffic yet is self-defeating. Low-utilization readings should read as "waiting," not as "failure." High-utilization readings should read as "working," not as "warning."

3. **Warnings fire on operational signal, not on raw %.** Memory at 95% due to loaded models a user explicitly pinned is success. Memory at 95% where macOS's vmstat is reporting compressor pressure IS a problem. Use the OS's own pressure signal (which we already collect in `node.memory.pressure`) instead of a % threshold.

4. **Preserve colorblind-friendly contrast.** Avoid pairs (red/green, blue/purple, yellow/orange) that fail common deficiencies. Use luminance contrast and shape/icon redundancy.

5. **The marketing screenshot and the live dashboard should both look good in the intended use-case state.** An idle demo dashboard shouldn't look broken; an engaged production dashboard shouldn't look alarmed.

## Proposed two-axis model

### Axis A — utilization intensity

Replace `barColor(pct)` with a **blue→purple gradient** that scales in hue, saturation, and opacity together. Low utilization reads as "waiting," high utilization reads as "engaged."

Why blue→purple (and not green→purple, which was considered):

1. **Warning-overlay contrast stays high at every utilization level.** Axis B uses yellow and red outlines for memory pressure and thermal throttling. Those pop cleanly off a blue-or-purple bar at 0% and at 100%. A green fill at low utilization would collapse contrast with a yellow warning outline — they're adjacent in hue space.
2. **No collision with existing green semantics.** Status dots still use green for "online" on every node card. If the CPU bar next to an online dot were also green at low utilization, viewers would read the bar color as "this node is healthy" rather than "this node is waiting for work." The muscle memory of green-means-good is deep enough that we can't ask users to consciously override it every time.
3. **Reads technical and calm.** Cool-to-cool progressions fit infrastructure tooling; warm-to-cool progressions borrow a story ("hot, working; cool, idle") from game UIs that doesn't fit the product's positioning.
4. **Colorblind-safe enough.** Blue→purple stays perceptible under protanopia and deuteranopia (the two most common deficiencies). Green→purple maps to similar perceptual values for those users at the midpoint of the scale — the gradient vanishes.

**Subtler variant (recommended):** use slightly different gradients per metric so CPU and memory bars are distinguishable at a glance in dense multi-node views. Both gradients stay inside the cool-hue family (190°–270°), so Axis B outlines have maximum contrast against either.

| Metric | Low utilization | High utilization |
|---|---|---|
| CPU | cyan `~#38bdf8` (hue 195°) | purple `~#6c63ff` (hue 255°) |
| Memory | soft blue `~#818cf8` (hue 225°) | deep purple `~#7c3aed` (hue 270°) |

Concrete function (as shipped, post-brightness-tune):

```javascript
function utilizationColor(pct, metric) {
  // metric: "cpu" | "mem"
  // Both scales live in the cool-hue family so yellow/red warning
  // outlines (Axis B) have maximum contrast at every utilization level.
  // Three channels compound: hue shift + saturation growth + opacity growth.
  // Colorblind-safe because saturation/opacity carry the signal even if
  // the hue shift is imperceptible.
  var hueStart = metric === "cpu" ? 195 : 225;  // cyan vs soft-blue
  var hueEnd   = metric === "cpu" ? 255 : 270;  // purple vs deep purple
  var hue        = hueStart + (pct / 100) * (hueEnd - hueStart);
  var saturation = 78 + (pct / 100) * 14;       // 78% → 92%
  var lightness  = 62 - (pct / 100) * 10;       // 62% → 52%
  var alpha      = 0.72 + (pct / 100) * 0.28;   // 0.72 → 1.00
  return `hsla(${hue}, ${saturation}%, ${lightness}%, ${alpha})`;
}
```

The initial proposal in this plan used lower saturation (60→85), lower lightness (55→45), and a lower alpha floor (0.45→0.90). That shipped on first pass and was correct in its progression but read as dull against the dark card background — low-% bars were washed out compared to the MLX chip borders and IMG/STT/VIS badges on the same card. The values above are the tuned version that matches chip/badge visual energy while preserving the "low = waiting, high = engaged" gradient. See commit 40c8e0e.

Why three channels instead of one:

- Hue shift alone (210°→270°) is subtle and invisible to red-green colorblind viewers.
- Saturation growth alone looks flat and washed out at low utilization.
- Opacity growth alone risks low-utilization bars disappearing against dark backgrounds.
- Together they compound: "engaged" feels qualitatively different from "waiting," and the difference carries through any single-channel perceptual deficiency.

### Axis B — warning state

Fire independently of utilization %, using the existing data we already collect:

| Warning signal | Data source | Visual treatment |
|---|---|---|
| Memory pressure | `node.memory.pressure` in `{warning, critical}` (from `psutil.virtual_memory()`, already reported) | Pulsing `--yellow` or `--red` outline around the memory bar; small icon (⚠) adjacent |
| Thermal throttling | Sustained high CPU + chip detection heuristic (already factored into scoring signal 1); expose as `node.thermal.throttling_likely` bool | Red accent icon on the CPU bar |
| KV cache bloat | Existing `kv_cache_bloat` health check | Badge on the node card (already surfaced via Recommendations panel) |
| Node degraded / offline | Existing status-dot (already correct) | No change |

Key distinction: **the bar's fill color encodes utilization; the bar's outline/badge encodes concern.** They're visually separable and semantically independent.

### What "health" at high utilization means

- 95% memory, pressure=`normal` → vivid accent fill, no outline. Reads as "fully engaged, all good."
- 95% memory, pressure=`warning` → vivid accent fill + yellow outline + ⚠ icon. Reads as "fully engaged AND the OS is starting to feel squeezed; consider eviction."
- 95% memory, pressure=`critical` → vivid accent fill + red outline + pulsing icon. "Fully engaged AND the OS is in compressor distress; something needs to give."
- 5% memory, pressure=`normal` → faint fill, no outline. Reads as "idle, waiting for work." NOT alarming.

## Changes by file

### `src/fleet_manager/server/routes/dashboard.py`

- **Add** a new `utilizationColor(pct, metric)` function alongside the existing `barColor()`. Don't delete `barColor` yet — it's still used for the disk bar (line 4073) and the capacity-score bar (line 1965), where the busy-is-bad semantic is correct.
- **Swap** five call sites from `barColor` to `utilizationColor`: lines 1911 (CPU SSE), 1913 (memory SSE), 2010 (CPU initial), 2015 (memory initial), 4068 (recommendations-page RAM).
- **Wrap** those five bar elements with a `<div class="bar-outer">` that can carry a warning-state class (Axis B).
- **Add** CSS classes: `.bar-warning`, `.bar-critical`, `.bar-thermal` — drive from `node.memory.pressure` and thermal signals.
- **Audit** the `--red`, `--orange` uses after the change to confirm they only appear on actual warning paths. Post-change audit list:
  - Status dots (offline) — keep red ✓
  - Badges (offline) — keep red ✓
  - Model-chip `image` (orange, categorical) — keep ✓
  - MLX server status colors (categorical) — keep ✓
  - `barColor` at high % on disk bar — keep (disk full is a real warning) ✓
  - Health recommendation cards — keep (driven by severity) ✓
  - `barColor` on CPU/memory/RAM bars — **removed by this change** ✓

### `src/fleet_manager/models/node.py`

Possibly extend `MemoryMetrics` with the pressure enum surfacing to the API response (already collected server-side; verify it reaches `/dashboard/api/status` and `/dashboard/api/recommendations`). Add a `thermal` field if we want a crisp throttling signal separate from raw CPU %.

### `docs/guides/` (new file)

Add `docs/guides/dashboard-color-reference.md` describing what each color means, for both operators and marketing contributors. Prevents future drift back into server-ops defaults. One page, no marketing fluff, one-screen cheat sheet.

### Marketing site + screenshots

Update `https://ollamaherd.com` landing-page screenshots to use the new palette. This is technically separate from the code change but should ship together so the live dashboard matches what marketing shows. Coordinate with the site repo (if separate) in the same PR week.

## Rollout phases

### Phase 1 — the palette swap on `/dashboard` (half-day)
1. Implement `utilizationColor(pct, metric)` with the two-gradient proposal (CPU = cyan→purple, memory = soft-blue→deep-purple).
2. Replace `barColor` at the four main-dashboard call sites (lines 1911, 1913, 2010, 2015).
3. Leave the disk bar (line 4073) and capacity-score bar (line 1965) on the original `barColor` — their semantics are correct.
4. Manual visual check at 0%, 25%, 50%, 75%, 95%, 100% utilization in both light (if supported) and dark themes.
5. Snapshot `/dashboard` at characteristic utilization levels for review.

### Phase 2 — warning-state overlay on `/dashboard` (half-day)
1. Add `.bar-warning` / `.bar-critical` / `.bar-thermal` CSS.
2. Wire them to `node.memory.pressure` in the node-card render (CPU and memory bars).
3. Verify thermal signal is actually surfaced to the API (may require a data-model addition — see "Thermal signal data model" in Open Decisions).
4. Manual test: force memory pressure on a test node (`stress-ng --vm 4 --vm-bytes 90%`) and verify the overlay fires independently of utilization color.

### Phase 3 — extend to `/dashboard/recommendations` (quarter-day)
1. Swap the node RAM bar at line 4068 to `utilizationColor`.
2. Leave the disk bar at line 4073 on the original `barColor` (disk is still hard-ceiling capacity — keep warning semantics).
3. Optionally add the Axis B warning overlay on the RAM bar if memory pressure is available in the recommendations data. If not available, skip — Axis A alone is still a correctness improvement on this page.
4. Manual visual check of a recommendations view with at least one node in the plan.

### Phase 4 — audit + docs (half-day)
1. Grep every remaining use of `--red`, `--orange` in the dashboard and confirm semantic.
2. Add `docs/guides/dashboard-color-reference.md`.
3. Update marketing screenshots; coordinate landing-page update.
4. Release note in CHANGELOG: "Dashboard: utilization now rendered in positive accent colors; warnings fire on OS-reported pressure, not raw %."

**Total scope: ~1.75 days.** Low code risk (isolated to dashboard.py — no other HTML-producing surfaces in the codebase). The recommendations page adds a quarter-day over the main dashboard since the change is mechanical once Axis A is defined. Moderate UX risk remains (the specific palette chosen could still read wrong to some users).

## Open decisions

1. **Exact hex values within the blue→purple range.** The two-gradient proposal (cyan→purple for CPU, soft-blue→deep-purple for memory) is concrete enough to implement, but the specific endpoints still benefit from a design pass — particularly verifying the 50% mark doesn't fall into the "neither blue nor purple" zone that can read muddy on some monitors. Green→purple was considered and rejected (see Axis A rationale). A quick A/B between the proposed two-gradient approach and a single-gradient-for-both approach on the marketing site could help resolve whether the metric-differentiation benefit is worth the added visual complexity.

2. **Should we expose a "show me idle as alarming" mode?** Some cooperative-compute operators who've committed their fleet to earning genuinely want their dashboard to scream when utilization drops — that's lost revenue. Could be a settings toggle. Probably not worth it for v1; defer until someone asks.

3. **How does this interact with the Recommendations panel?** Currently the memory-pressure health check produces a card in the side panel. With Axis B firing visually on the node card too, we have two surfaces showing the same signal. That's probably fine (redundancy helps discovery) but the team should agree on which is canonical if they disagree (e.g. pressure resolves on one but not the other).

4. **Thermal signal data model.** Currently factored into scoring but not exposed as a node-level status. Need to decide whether to surface it as:
   - a bool `throttling_likely` (simple, binary)
   - a float `thermal_headroom_pct` (richer, matches CPU% style)
   - nothing new (derive client-side from sustained-high-CPU heuristic)

## Non-goals

- **This is not a wholesale dashboard redesign.** Layout, typography, and information density stay as-is. Only the color semantics for utilization bars change.
- **We are not removing the health check system.** The Recommendations panel, the 31+ checks, and severity-based sorting all stay. The new bar overlay is in *addition* to those surfaces, not a replacement.
- **We are not adding new operator actions.** No "dismiss" / "acknowledge" / "mute" UI. Those can come later if warning states prove noisy in practice.
- **We are not modifying charts or categorical palettes.** `/dashboard/trends` and `/dashboard/models` use Chart.js with a categorical color array (`modelColors` on dashboard.py:3141). Those differentiate N time series visually — they're not a utilization scale and stay as-is.
- **We are not modifying the disk usage bar.** Disk at 95% is a hard-ceiling warning (model downloads will fail, trace DB writes will stop). `barColor` stays on the disk bar; the change applies only to product-value utilization metrics (CPU, memory, model RAM).
- **This plan does not touch anything outside `dashboard.py`.** Audited: no other HTML templates, no separate rendering layer, no client app. `README.md` and `skills/*.md` are pure markdown. The marketing site (`ollamaherd.com`) lives in a separate repo and gets a coordinated screenshot refresh as a sibling change — mentioned in Phase 4 but tracked there, not here.

## Related

- `docs/observations.md` → 2026-04-24 observations about utilization as product value.
- `src/fleet_manager/server/routes/dashboard.py` — implementation.
- `src/fleet_manager/models/node.py::MemoryMetrics` — data model for pressure.
- `src/fleet_manager/server/health_engine.py` — existing memory / thermal health checks to cross-reference.

## Source conversation

Raised 2026-04-24 during review of dashboard screenshots being prepared for marketing. The product team noted that an earlier "pretty" marketing screenshot was more accurate to the product's thesis than the live dashboard — prompting a broader rethink of whether server-ops color vocabulary applies to a cooperative-compute product at all.
