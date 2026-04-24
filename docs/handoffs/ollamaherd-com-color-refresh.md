# Hand-off to the agent maintaining `ollamaherd.com`

**From:** the team / agent maintaining the [`ollama-herd`](https://github.com/geeks-accelerator/ollama-herd) main repo.
**Date:** 2026-04-24
**Scope:** marketing-site repo (the public `ollamaherd.com` landing page + any static docs/screenshots it ships).
**Not scope:** the main `ollama-herd` repo itself — all necessary changes there have already shipped in v0.6.x.

---

## TL;DR (paste-ready for the first message to the site agent)

> We just shipped a dashboard color overhaul in the `ollama-herd` main repo (merged to `main`, included in PyPI 0.6.0+). The dashboard's CPU / memory / RAM utilization bars changed from a green→red "busy is bad" scale to a **blue→purple gradient** that treats high utilization as "the product working." Warning state now lives in a **separate visual overlay** (yellow / red / orange outlines on the bar container) driven by OS-reported memory pressure and thermal signals — not the raw %.
>
> The `ollamaherd.com` landing-page screenshots still show the **old** green-busy-is-bad palette. That's now actively inconsistent with the product's own messaging ("your spare Mac is wasting compute") — the old screenshots visually argue that an idle Mac is healthy and a busy one is alarming, which is the exact opposite of the product's thesis.
>
> Please refresh the screenshots (and any hand-drawn illustrations that mimic the dashboard) to match the new palette. Copy/messaging doesn't need to change, but a couple of small tweaks are worth considering (see "Optional copy touch-ups" below).
>
> Details, specs, and a live reference page are below.

---

## Why this matters (the narrative)

The product's whole value proposition is that **idle Apple Silicon is waste**. The landing page literally says things like "your spare Mac is wasting compute" and "pool all your devices into one fleet." Every piece of site copy reinforces: utilization = the product working; idle = what you're trying to fix.

But the **dashboard screenshots** on the site show the opposite semantic:
- CPU / memory bars get GREENER as they go to 0% (rewarding visual for idle)
- CPU / memory bars get REDDER as they approach 100% (alarming visual for busy)

This is inherited from server-ops tools like Datadog and Grafana, where high utilization actually IS a warning (it threatens the next traffic spike's uptime). But that's a SaaS-infra framing, not a cooperative-compute framing. Visitors reading the product copy and then looking at the dashboard get a subtly contradictory message: the words say "idle = bad," and the pictures say "idle = good."

The dashboard now fixes this. The marketing site should match.

---

## What changed in the dashboard (the spec)

Two independent color axes, not one combined scale:

### Axis A — utilization intensity (CPU / memory / RAM bars)

A cool-hue-family gradient, two sibling scales so CPU and memory are visually distinguishable side-by-side.

| Metric | Low utilization | High utilization | Feel |
|---|---|---|---|
| **CPU** | cyan (`hsl(195, 78%, 62%)`) | purple (`hsl(255, 92%, 52%)`) | technical, calm |
| **Memory** | soft blue (`hsl(225, 78%, 62%)`) | deep purple (`hsl(270, 92%, 52%)`) | same family, shifted |

Three channels compound (hue + saturation + opacity grow together from low → high) so the gradient is legible even under red-green color deficiency. Reference JS:

```javascript
function utilizationColor(pct, metric) {
  var hueStart = metric === "cpu" ? 195 : 225;
  var hueEnd   = metric === "cpu" ? 255 : 270;
  var hue        = hueStart + (pct / 100) * (hueEnd - hueStart);
  var saturation = 78 + (pct / 100) * 14;       // 78% -> 92%
  var lightness  = 62 - (pct / 100) * 10;       // 62% -> 52%
  var alpha      = 0.72 + (pct / 100) * 0.28;   // 0.72 -> 1.00
  return `hsla(${hue}, ${saturation}%, ${lightness}%, ${alpha})`;
}
```

Key invariant: **low utilization reads as "waiting," high utilization reads as "engaged."** Never as "warning." The product is working when the bar is purple.

### Axis B — warning state (outline on the bar container, independent of fill)

Only fires on real signals, never on raw utilization %:

| Class | Triggered by | Visual | Means |
|---|---|---|---|
| `.bar-warning`  | `psutil.virtual_memory()` reports pressure = `"warning"` | Yellow 1px outline + soft glow | OS memory compressor starting to work |
| `.bar-critical` | Same, pressure = `"critical"` | Red 1px outline + **pulsing** glow (1.8s) | OS memory compressor under distress |
| `.bar-thermal`  | Linux: `psutil.sensors_temperatures()` peak ≥ 85°C; macOS/Windows: sustained CPU ≥ 95% fallback | Orange 1px outline + soft glow | Likely thermal throttling |

The important part: a bar at 95% memory with **normal** pressure shows a vivid purple fill and **no outline**. Same bar at 95% memory with **critical** pressure shows the same purple fill **plus** a pulsing red outline. Utilization and health are independently readable at a glance.

### What stayed the same

- **Status dots**: green (`online`) / yellow (`degraded`) / red (`offline`) — liveness, NOT utilization.
- **Health badges** on recommendations: info / warning / critical — severity from the 18-check health engine.
- **Model chips** (Ollama / MLX / image / STT / embed) — categorical type-coding, unchanged.
- **Disk usage bar** on the recommendations page — still uses the old green→red scale because disk actually IS a hard ceiling (full disk breaks things).
- **Capacity availability score** — still green-at-high-availability, because that genuinely means "low risk of interrupting the human on that machine."

---

## Live reference you can pull screenshots from

The upstream repo ships a dev-only route specifically for this:

```
http://localhost:11435/dashboard/color-states
```

It renders every warning state side-by-side using the live CSS + JS. To use it:

1. Run `uv run herd` from a local clone of `ollama-herd` (or `pip install ollama-herd>=0.6.1 && herd`)
2. Open `http://localhost:11435/dashboard/color-states` in a browser
3. Screenshot what you need

That page has three sections:
- **Axis B states** — memory bar at 75%, rendered in all 4 warning states (normal / memory warning / memory critical / CPU thermal)
- **CPU gradient sweep** — 8 utilization levels (5%, 15%, 30%, 50%, 70%, 85%, 95%, 100%) showing the cyan→purple progression
- **Memory gradient sweep** — same sweep for the soft-blue→deep-purple memory scale

For "hero shot" screenshots of a realistic fleet, use the main `/dashboard` page on a running node — the user's live screenshots from earlier in the day showed a Mac Studio at ~37% memory with vivid blue-purple fill, which reads exactly as intended.

---

## What the marketing site needs to update

### 1. Screenshots (required)

Audit every image on `ollamaherd.com` (and any docs it embeds) that shows:

- A node card with CPU or memory bars
- The full dashboard at any route
- Any hand-drawn illustration mimicking the dashboard UI

Replace with fresh captures using the new palette. The `/dashboard/color-states` reference page is the easiest way to get clean, repeatable shots of edge cases.

**Don't miss:**
- OG / Twitter card preview images
- Favicon if it incorporates bar colors
- Any animated GIFs / Lottie / video previews
- Dark-mode and light-mode variants (dashboard is dark-mode-only today, but any light-mode marketing artwork should also shift)

### 2. Copy (optional touch-ups)

The core messaging ("your spare Mac is wasting compute") was already aligned with the new direction — it just wasn't supported by the visuals. No rewrites needed.

If you want to reinforce the new semantic, consider:

- **Near a hero screenshot**: a short caption like "Every bar lit up = every machine earning its keep."
- **Near a feature callout about fleet utilization**: "Vivid purple = fully engaged. Faint cyan = waiting for work. Yellow outline = the OS is under pressure — the only time the dashboard raises its voice."
- **In any "why" section**: a sentence framing the product thesis explicitly — "Idle hardware is waste. Ollama Herd pools your fleet so the expensive Macs you already own actually do work."

None of these are required. The image refresh alone closes the visual/textual contradiction.

### 3. Documentation on the marketing site (if any)

If `ollamaherd.com` hosts any docs that describe the dashboard (getting-started guides, architecture overview), scan for:

- Phrases like "green means healthy, red means problem" — rephrase to describe the new two-axis semantic.
- Screenshots embedded in those docs — refresh same as the landing-page ones.
- Links back to the main repo's docs, which already describe the new system. Canonical references in the main repo:
  - `docs/plans/dashboard-color-semantics.md` — full design rationale and audit.
  - `docs/guides/dashboard-color-reference.md` — one-page cheat sheet for future UI additions.

---

## What NOT to change

- **Don't invent color semantics that aren't in the main dashboard.** If you're tempted to add a new color for something the dashboard doesn't have a visual for, skip it — the point is alignment, not creative expansion.
- **Don't change the product-tier framing** (pro / cooperative / etc.) based on color. The palette change is about utilization vs. warning, not about marketing positioning.
- **Don't soften the warning outlines.** The `.bar-critical` pulsing red is deliberate — operators need to see distress instantly, even when they're skimming a multi-node view. If a marketing illustration shows a critical state, keep the pulsing red visible.

---

## Questions? Triggers to escalate back to the main-repo team

- **If you find a marketing asset that contradicts the new semantic in a way this handoff doesn't cover** — e.g. an explainer video that walks through the dashboard using old colors — flag it and we'll coordinate a re-record rather than you doing a partial fix.
- **If the `/dashboard/color-states` route doesn't render for you** — check you're on `ollama-herd >= 0.6.1` (route was added post-0.6.0). If on 0.6.0 exactly, upgrade or pull from `main`.
- **If a stakeholder pushes back on blue→purple specifically** — the full rationale for the exact hue choice (including why not green→purple) lives in `docs/plans/dashboard-color-semantics.md` → "Why blue→purple" section. Cite that; don't relitigate from scratch.

---

## Related commits in the main repo (for context, not action)

- `f1d0c13` — Dashboard colors: utilization isn't a warning, pressure is (main two-axis implementation)
- `40c8e0e` — Brighten utilization bars to match chip/badge visual energy (intensity tune-up)
- `9422f88` — Plan doc: sync utilizationColor coefficients with shipped code
- Current HEAD — adds `ThermalMetrics` data model, real Linux thermal sensors, `/dashboard/color-states` dev route

Good luck!
