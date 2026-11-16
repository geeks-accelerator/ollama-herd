# A live, visual Overview that fills the screen

**Status:** Implemented (2026-11-16); see "As built" at the end
**Supersedes, for the Overview only:** the "figures, not charts" outcome of
[`dashboard-calm-redesign.md`](dashboard-calm-redesign.md). The rest of that plan
stands: colour only for state, dot plus word, tokens, both appearances.

## Why

Feedback on the shipped redesign: the Overview is "lots of cards with text and
no charts". Fair. The calm redesign took out decoration and some signal went with it.
A fleet monitor is read for **change over time**: is throughput falling, is
latency climbing, is one node hotter than the others? A number with no
history can't answer "compared with what?". Right now the only way to see a
trend is the Models page, hourly, after a click.

The page is also capped at 1240 px. Someone watching a fleet on a 27-inch
display gets a centred column with empty space either side.

"Calm" was never meant to rule out charts. It means no decoration: every mark
carries data, colour means state, and nothing moves without a reason. A
sparkline is the calmest way to show a trend. Tufte's description is
"data-intense, design-simple, word-sized".

## What the research says

- **Headline number on top of its own recent history.** This is Grafana's stat
  panel: a large value with a faint sparkline behind it. Grafana's
  best-practice guide puts key metrics in large stat panels with supporting
  detail beneath. ([Grafana][grafana], [Grafana blog][grafana-blog])
- **Symptoms first, causes second.** RED (rate, errors, duration) is what users
  feel and is where alerting belongs. USE (utilisation, saturation, errors) is
  per machine and explains the RED numbers. ([Grafana][grafana]) That maps
  directly onto this page: fleet RED at the top, per-node USE below.
- **Small multiples for many entities.** The same small chart repeated per node
  or model, on a **shared y-axis and a fixed time window**. Then an outlier
  stands out, and a 1% wobble doesn't look like a 40% swing.
  ([D3 sparkcharts][d3], [UXmatters][uxm])
- **The window fits the screen.** Netdata sizes its live window to the browser
  width, about one point per few pixels, and keeps one-second ticks at the
  default zoom. ([Netdata][netdata]) A wider window shows more history at the
  same resolution, which answers the "use the whole screen" request directly.
- **Normal recedes, exceptions stand out.** "Healthy infrastructure should
  almost disappear." ([Lumics][lumics]) That fits colour-only-for-state: series
  draw in neutral and accent tones, and only a threshold crossing turns amber
  or red.
- **Every chart answers "compared with what?"** ([Five Nines][fivenines]) A
  sparkline gives each figure its own recent baseline.

## What we have to work with

| Signal | Source today | History available? |
|---|---|---|
| Requests, tokens, TTFT, latency, errors per request | `request_traces` (indexed on `timestamp`, `node_id, model`) | **Yes**: a per-minute bucket query over the last N hours is cheap |
| Queue depth, in-flight per `node:model` | SSE `/dashboard/events`, every 2 s | No, live only |
| Node CPU, memory, pressure | heartbeat every 5 s, pushed over SSE | **No**: only the latest heartbeat is kept |

So the request charts can show history from the first page load. Node charts
would start empty and fill over minutes unless the router keeps a short
history. That gap is the only new server state this plan adds.

## Plan

### 1. Full-width, fluid layout (Overview only)

- Remove the 1240 px cap on the Overview and set a 24 px gutter, 16 px on phones.
  Health, Settings and the prose blocks keep their reading width (`max-width:
  72–80ch`); text lines that run 300 characters are unreadable.
- Every row becomes a fluid grid that adds columns as the window widens:
  - figures: `repeat(auto-fit, minmax(200px, 1fr))`, so five across on a
    laptop and still five, wider, on a large display;
  - node cards: `repeat(auto-fill, minmax(340px, 1fr))`, so 1 per row on a
    phone, 3 on a laptop and 5 or 6 on a 27-inch display;
  - **at ≥ 1600 px, the hero chart and the node grid sit side by side** so
    everything fits on one screen without scrolling.
- The live chart window grows with width: about 4 px per minute-point, so
  roughly 60 minutes on a laptop and up to 3 hours on a large display, with a
  1h / 6h / 24h override.

### 2. Figures become stat panels with sparklines

The five existing figures (requests/min, tokens/s, TTFT, error rate, queue)
keep their large value, with a faint area sparkline behind it covering the same
window as the hero chart. The line uses `--text-3` at low alpha and switches to
`--warn`/`--fail` only when the figure crosses its threshold.

### 3. A hero chart: fleet activity

One wide chart directly under the status line:

- **tokens/s generated, stacked by model** (area), the fleet's real output;
- **requests per minute** as a thin line on a second axis;
- **failures as red ticks on the time axis**, so a bad minute is visible at a glance and
  attributable.

Data comes from a new `GET /dashboard/api/timeseries?window=3600&step=60`: one
`GROUP BY` over `request_traces` bucketed by `step`, returning per-bucket and
per-model counts, completion tokens, p50 TTFT and failures. The page re-fetches it
**every 60 s** (one cheap indexed query) and also nudges the current bucket
from the 2 s SSE stream, so the right edge stays live between fetches.

### 4. Node cards with live small multiples

Each node card gets three sparklines on **shared y-axes across all cards**
(CPU %, memory %, tokens/s served), plus the current value. Pressure and
thermal stay as dot plus word. With one node this is a single card; with five
it's a row you can scan for the odd one out.

To make these useful from the first load, `NodeRegistry` keeps a **ring buffer
of the last 60 minutes of heartbeat metrics per node** (5 s cadence gives 720
points at a few hundred bytes each, under 1 MB for ten nodes). A
`GET /dashboard/api/node-history` endpoint seeds the sparklines on load, and
each SSE tick appends to them. Memory only: nothing new on disk, and the buffer
resets on a router restart, which is honest about what it is.

### 5. Models at work: one sparkline per row

Each active model row gets a tokens/s sparkline (shared y-axis) beside its
in-flight and queue figures. Idle models stay folded.

### Rendering

- **Sparklines are inline SVG from one shared helper** (`sparkline(el, points,
  {min, max, state})`): one `<path>`, no library, no per-chart instance. Five
  nodes × three metrics plus models would be 25+ Chart.js instances redrawing
  every 2 s.
- **The hero chart uses Chart.js**, already loaded on Models, Usage and
  Benchmarks, through the existing `chartTheme()`. On append it calls
  `update('none')` so the line shifts without animating.
- Reduced motion: no transitions on append (already how `REDUCED_MOTION`
  works). Background tabs: skip redraws while `document.hidden` and catch up
  on return.
- All colours come from tokens, so both appearances work for free and the
  contrast sweep keeps passing.

## Not in this plan

- **Long-range history for node metrics.** Persisting heartbeats to SQLite is
  a real cost (17K rows/day/node). The Models page already covers long-range
  request history. Revisit if someone asks "what was memory doing yesterday".
- **A wallboard / TV mode.** The fluid layout gets most of the way there; a
  dedicated kiosk view can follow if it's wanted.
- **The fast-mode decode share figure.** Still the right statistic, still
  needs its own endpoint; it slots into the figure row later.

## Verification

- Unit tests: the timeseries query (bucketing, per-model split, empty window,
  failures counted), the ring buffer (bounded, ordered, per node), and both
  endpoints.
- Browser, against the live fleet: 375 px, 1280 px, 1920 px and 2560 px,
  dark and light, checking column counts and that nothing scrolls sideways. Leave the page
  open 10 minutes and confirm the charts advance, the 60 s refetch lands, and
  memory stays flat. Run `scripts/dashboard-contrast-sweep.py`, which covers
  reduced motion too.
- CPU cost: profile the page for 60 s with five fake nodes; the SSE tick
  plus sparkline redraw should stay well under a frame.

[grafana]: https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/
[grafana-blog]: https://grafana.com/blog/2024/07/03/getting-started-with-grafana-best-practices-to-design-your-first-dashboard/
[d3]: https://syntagmatic.github.io/d3-power-tools/skills/sparkcharts/SKILL.md
[uxm]: https://www.uxmatters.com/mt/archives/2005/12/small-multiples-within-a-user-interface.php
[netdata]: https://learn.netdata.cloud/docs/dashboards-and-charts/netdata-charts
[lumics]: https://www.lumics.io/why-most-network-dashboards-fail-the-30-second-test/
[fivenines]: https://fivenines.io/blog/metrics-and-dashboards/

## As built (2026-11-16)

Shipped as planned, with these departures:

- **The request series refetches every 15 s** for windows up to 3 h (60 s for
  6 h and 24 h) instead of every 60 s plus a client-side nudge. One indexed range
  scan over at most a few thousand rows is cheap, and a second code path that
  guesses the current bucket from queue counters would have been debt. Node and
  queue sparklines still move every 2 s, by appending each SSE tick to the
  fetched history.
- **The sampler records the queue too.** `FleetHistory` samples registry and
  queues on one clock, so "Running now" and "Waiting in queue" get real history
  alongside node CPU and memory.
- **Six figures, not five.** "Requests, last 24 h" became requests/min, and
  tokens/s was added. Rates show the last five *complete* minutes (the current
  bucket is still filling and would read low); error rate covers the window and
  says which.
- **Node cards show memory as a percentage**, with GB and swap/compressed on a
  line beneath: "237 GB / 512 GB" collided with its neighbour at card width.
- **The latency sparkline is a line without fill.** Filling to zero implies a
  quantity, and gaps between sparse buckets drew misleading slivers.
- **Node history starts empty after a restart, and the page says so** ("CPU and
  memory since 6:52 AM") rather than leaving a half-drawn sparkline looking broken.

**What the chart showed on its first run:** red failure marks that the old
cards had folded into a single 24 h percentage. The 11 `rejected` requests sat
exactly on this session's router restarts (a request arriving before the node
re-registers, the documented restart artifact), and 14 `client_disconnected`
requests clustered between 04:41 and 05:06, a client giving up while queued.
Both were already in the error rate; now there is a "when" beside the "how many".

**Verified** in the browser pane against the live fleet at 375, 1440 and 2560 px,
dark and light: no horizontal scroll, side by side from 1600 px, Fit choosing
1 h on a phone and 3 h on a large display. Left open, the series refetched every
15 s, node history grew with each SSE tick, the chart updated in place (one
Chart.js instance throughout) and the JS heap did not grow.
`scripts/dashboard-contrast-sweep.py` passes on every page in both appearances,
reduced motion included. **Not run:** the 60-second CPU profile with five fake
nodes. This fleet has one node online, so the multi-node small multiples are
tested but not yet seen with real peers.

