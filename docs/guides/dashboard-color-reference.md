# Dashboard color reference

One-page cheat sheet for what each color means on the Ollama Herd dashboard and **why**. Keep this in mind when adding new UI so the semantics stay consistent.

## The core principle — two independent axes

The dashboard uses **two independent color channels**, not one combined scale:

- **Axis A — utilization intensity** (fill color on CPU / memory / RAM bars). Blue-to-purple gradient. Low utilization reads as *waiting*; high utilization reads as *engaged*. **NOT a warning scale.** A Mac Studio at 95% memory is the product working as designed, not a problem.
- **Axis B — warning state** (outline / glow on bar containers). Fires independently of utilization %, driven by OS-reported pressure signals (`psutil.virtual_memory().pressure`), sustained thermal conditions, or explicit health-check findings.

The upshot: at 95% memory with `pressure=normal`, a bar shows a vivid purple fill with no outline — "working hard, all good." At 95% memory with `pressure=critical`, the same purple fill gets a pulsing red outline — "working hard AND the OS is under compressor stress." Both utilization and health are visible at a glance without being conflated.

## What each color means

### Bar fills (`utilizationColor(pct, metric)`)

Used on: CPU bar, memory bar (`/dashboard`), RAM bar (`/dashboard/recommendations`).

| Utilization | CPU bar (cyan → purple) | Memory bar (soft blue → deep purple) | Reads as |
|---|---|---|---|
| 0–10%  | Faint cyan / soft blue       | Faint soft blue                | "idle, waiting for work" |
| 50%    | Mid blue-purple              | Mid blue-purple                | "actively working"       |
| 95%+   | Vivid purple                 | Vivid deep purple              | "fully engaged"          |

Three channels compound (hue shift + saturation + opacity) so the progression is visible even to viewers with red-green color deficiency.

### Bar outlines (`.bar-warning` / `.bar-critical` / `.bar-thermal`)

Overlay on top of bar container. Fires independently of fill.

| Class | Trigger | Visual | Reads as |
|---|---|---|---|
| `.bar-warning`  | `node.memory.pressure === "warning"`   | Yellow 1px outline + soft glow             | "OS memory compressor is starting to work" |
| `.bar-critical` | `node.memory.pressure === "critical"`  | Red 1px outline + pulsing glow (1.8s loop) | "OS memory compressor is under distress"   |
| `.bar-thermal`  | CPU utilization ≥ 95% sustained        | Orange 1px outline + soft glow             | "likely thermal throttling"                |

Yellow / orange / red for Axis B are chosen for maximum contrast against the cool-hue-family fill colors.

### Busy-is-bad (`barColor(pct)`) — use sparingly

Used on: **disk usage bar** on `/dashboard/recommendations`, **capacity availability score** (input inverted).

Green at low % → red at high %. Correct for metrics where "high" is a genuine warning:
- **Disk usage**: at 95% full, model downloads fail and the SQLite trace DB stops writing. High *is* bad.
- **Capacity availability**: the score is "fraction of time this node is unused by humans." Via the `100 - score*100` inversion, green-at-low-busy maps to green-at-high-availability. Correct.

Do NOT use `barColor()` for CPU / memory / RAM utilization. Those are product-value metrics and use `utilizationColor()` instead.

### Categorical colors (not a scale)

Used where items differ by *kind*, not by *amount*.

| Palette | What it codes |
|---|---|
| Status dots: green / yellow / red | Node liveness (`online` / `degraded` / `offline`) — NOT utilization |
| Health badges: info / warning / critical | Severity from the 18-check engine |
| Model chips: purple-ish / orange / green / blue | ollama / image / stt / embed (type-coding) |
| MLX server rows: green / amber / red / grey | `healthy` / `starting|memory_blocked` / `unhealthy` / `stopped` |
| Chart.js palettes on `/dashboard/trends` + `/dashboard/models` | Time-series line differentiation — categorical, no scale |

Rule: if the color has a meaning, it's nominal (this-vs-that) not ordinal (this-is-more-than-that).

## When adding new UI

Before picking a color, decide which category the new element falls into:

1. **Is it a utilization metric?** (CPU, memory, model cache, GPU usage, etc.) → use `utilizationColor(pct, 'cpu'|'mem')`. Default `'mem'` for anything that isn't compute-time-like.
2. **Is it a warning state?** (OS pressure, thermal, health alert) → overlay it as an outline/badge via `.bar-warning`/`.bar-critical`/`.bar-thermal` or a severity badge. Don't encode it in the fill.
3. **Is it a hard-ceiling capacity?** (disk, quota, rate limit where running out actually breaks things) → use `barColor(pct)`. High genuinely is bad.
4. **Is it categorical?** (status, type, kind) → pick a color from the palette, not from a scale.

If in doubt, ask: *"Does a user's dashboard lighting up this color make them feel the product is working, or feel something is wrong?"* The answer should match reality.

## Live reference — `/dashboard/color-states`

When `herd` is running, visit [`http://localhost:11435/dashboard/color-states`](http://localhost:11435/dashboard/color-states) to see every warning state and the full CPU / memory gradient sweep rendered side-by-side using the live CSS + JS. This is the screenshot source for the marketing site and the eyeball regression test for designers. Any change to `utilizationColor()` or the `.bar-*` CSS shows up here automatically — no separate demo to maintain.

## Cross-references

- [`docs/plans/dashboard-color-semantics.md`](../plans/dashboard-color-semantics.md) — the design rationale and audit that drove this system.
- [`docs/observations.md`](../observations.md) — 2026-04-24 observation on utilization as product value.
- [`docs/handoffs/ollamaherd-com-color-refresh.md`](../handoffs/ollamaherd-com-color-refresh.md) — hand-off brief for the marketing-site agent.
- `src/fleet_manager/server/routes/dashboard.py` — `utilizationColor()`, `barColor()`, and the `.bar-*` CSS rules live in one file.
