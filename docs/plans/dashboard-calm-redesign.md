# Dashboard calm redesign: glanceable, quiet, Apple-restrained

**Status:** Implemented on `main` 2026-11-15, all five phases (unreleased). See § *As built* at the end for where the build departed from this plan.
**Filed:** 2026-11-15
**Scope:** the router's dashboard at `/dashboard/*`, all rendered from
`src/fleet_manager/server/routes/dashboard.py`. **Not** the ollamaherd.com site.
**Revised:** 2026-11-15, adding four ideas from a review of a design note for
another project (the same Liquid Glass reasoning, applied to a different app):
the solid fallback for reduced transparency and high contrast, dot-plus-word
status, concentric corners, and the worst-case contrast check for translucent
bars.
**Builds on:** [`dashboard-color-semantics.md`](dashboard-color-semantics.md).
Its two-axis rule (utilization is the product, so busy is never red) is already
partly shipped as `utilizationColor()`; this plan keeps that rule and extends it
to the whole page. That plan's `Status: Proposed` line is stale and should be
corrected when this work starts.

**Constraints** (CLAUDE.md § Design Principles):
- Greenfield, so **no `FLEET_*` flag and no second "classic" dashboard**. The new
  design replaces the old one page by page.
- **Two-person scale.** Everything stays inside the Python package as it is now:
  no bundler, no npm, no framework, no web font download. The dashboard has to
  work offline on a LAN.
- **Reuse before build.** `_SHARED_CSS`, `_dashboard_page()`, the SSE stream at
  `/dashboard/events` and the existing JSON endpoints stay; this is a
  presentation change, not a data change.

---

## Why

The dashboard answers "what does herd know?" well and "is my fleet OK?" badly.
Observed on the live fleet on 2026-11-15:

| What a viewer sees | What is wrong with it |
|---|---|
| The top of `/dashboard` is a "Fleet Intelligence" panel reading *Generating fleet briefing…* | The most valuable space on the page is a loading state, waiting on an LLM call |
| The health score (85) lives on `/dashboard/health` | The one-number answer to "is it OK?" is not on the home page |
| 11 model cards of equal size; one (`gpt-oss:120b`) is busy, ten show rows of `0` | Activity is buried in inventory. The busy card has the same weight as idle ones |
| `0` failed printed in red on every idle card | Red stops meaning "trouble" when it is everywhere |
| Coloured chips on every card: TEXT, OLLAMA, EMBED, NATIVE, VISION, VISION… | Colour used as decoration; five accent colours compete with the status colours |
| Header reads `96995 completed` | No thousands separator |
| Footer reads **`Ollama Herd v0.1.0`** | Hard-coded at `dashboard.py:2196`; wrong on every release ever shipped |
| 8 nav tabs, including both Health and Recommendations | Two tabs answer the same question |

Measured in the code:
- 24 distinct hard-coded hex colours;
- 169 inline `style="…"` attributes;
- 7 page-local `<style>` blocks beside `_SHARED_CSS`;
- `tabular-nums` used in only 15 places, so live counters jitter as digits change width;
- **no `prefers-color-scheme`** (dark only);
- **no `prefers-reduced-motion`**.

## Design direction

Current practice for developer and operations tools is converging on calm
surfaces: neutral palette, one accent, quiet chrome, dark mode as a real design
target, and density carried by typography rather than boxes and colour
([AdminLTE 2026 examples](https://adminlte.io/blog/25-best-dashboard-design-examples-for-2026-real-products-principles/),
[daisyUI, data-dense utilitarian UI](https://trends.daisyui.com/trend/data-dense-utilitarian-ui/)).

Apple's Liquid Glass ([overview](https://en.wikipedia.org/wiki/Liquid_Glass)) is
the other half of the request. Its known weakness is legibility: content blends
into a translucent background
([AppleMagazine](https://applemagazine.com/liquid-glass-design/)). An operations
dashboard is read at a glance, often from across a desk.

**So herd takes Apple's restraint, not its glass.** Translucency appears in one
place, the navigation chrome, and never behind a number. Those sources are design
commentary, not measurement; the rules below are justified by this dashboard's
own problems, listed above, not by trend.

### Rules

1. **Answer first.** Every page opens with the sentence a reader came for.
   Explanation, history and AI commentary follow it.
2. **Colour is a signal, never decoration.**
   - Neutral greys for structure.
   - One accent (the existing `#6c63ff`) for interaction: links, the active tab, focus rings.
   - **The accent is never green**, and must not converge with ollamaherd.com's
     green (`--neon`). On the site green means "you can press this"; here green
     means "online / OK", so a green link would read as a status. The two
     products differ on purpose, decided 2026-11-16 alongside the site's own
     minimal redesign: do not "unify the brand" by changing either.
   - **Destructive buttons are red words on a neutral button**, never a filled
     red block: white on `--fail` is 2.8:1 in dark, and a red block shouts on a
     page where red means "failing now".
   - **Status colours only for state:** green means online, amber means degraded, red means failing *now*.
   - A zero is grey. "0 failed" is never red.
   - Status is shown as a small coloured dot plus a word ("● Online",
     "● Degraded"), never colour alone and never a filled pill. That stays
     readable for colour-blind viewers and is quieter than a row of chips.
   - Utilization keeps the cyan-to-purple scale from `dashboard-color-semantics.md`, so busy is not alarming.
3. **Activity over inventory.** What is doing work gets space; idle things
   collapse to a single line that can be expanded.
4. **Numbers are the interface.**
   - Large, quiet figures with small sentence-case labels, instead of today's all-caps headings.
   - `tabular-nums` everywhere.
   - Locale thousands separators (`Intl.NumberFormat`).
   - Units in a lighter weight than the value.
5. **One material per job.**
   - Solid surfaces for content.
   - The sticky header alone gets `backdrop-filter: blur()` over a translucent background.
   - Elevation comes from a 1px border and spacing, not shadows.
   - **Concentric corners:** an inner element's radius is its container's radius
     minus the padding between them, so nested shapes sit parallel. One radius
     scale in the tokens, not per-component values.
   - **A solid fallback for the glass.** When the viewer asks for less
     transparency or more contrast, the header becomes solid. Browser support for
     `prefers-reduced-transparency` is uneven, so it is paired with
     `prefers-contrast: more` and with the manual appearance override in
     Settings, so the fallback is reachable in every browser.
6. **Motion explains change.**
   - Numbers ease to a new value (~200 ms).
   - A status change gets one soft highlight.
   - Nothing loops or decorates.
   - All of it is disabled under `prefers-reduced-motion`.
7. **Both appearances.** Light and dark from one token set, following
   `prefers-color-scheme`, with a manual override in Settings stored in
   `localStorage` (a per-viewer convenience, so browser storage is right here).

## Audit: what already exists, and what this plan reuses

Audited 2026-11-15 against `dashboard.py` (5,779 lines). The redesign is mostly
**consolidation**: the patterns it needs exist, often more than once, and the
debt is in the copies. Every phase below names the existing code it extends.
**Greenfield rule:** each step replaces the old version in the same change.
There is no flag, no "v2" helper beside the old one, and no period where two
designs coexist on one page.

### Already right, keep as is

| Pattern | Where | Use in this plan |
|---|---|---|
| One page shell for every tab | `_dashboard_page()` + `_SHARED_CSS` | Home of the tokens, shared JS helpers, header and footer |
| Shared JS in the page head | `barColor()`, `utilizationColor()` in `_dashboard_page` | Where every helper consolidated below moves to |
| Charts read colours from CSS variables | `getComputedStyle(...)` → `const C` on the chart pages | Tokens reach Chart.js with no new mechanism |
| Status dot classes | `.status-dot.online / .degraded / .offline` | The dot in dot-plus-word status |
| Status in the favicon and tab title | `setTabAlert()` + `_FAVICON_SVG_ALERT` (offline only today) | Phase 4 extends it to degraded/critical; nothing new is built |
| Health score and severities, cached | `/dashboard/api/health` (30 s cache), `scoreClass()` (80 / 50) | Source of the home-page status line; no new endpoint |
| AI briefing as its own endpoint and poll | `/dashboard/api/briefing`, `fetchBriefing()` (30 s) | Moving it below the fold is markup only |
| Old URL kept by a second route decorator | `/dashboard/apps` on the Tags handler | The pattern for every merged tab (render, not redirect) |
| Toast | `showToast()` | Confirmation feedback, unchanged |
| Colour reference page | `/dashboard/color-states`, already rendering `utilizationColor` live | Becomes the token reference in both appearances |
| Version string | `fleet_manager.__version__`, used by `app.py` and `health_engine.py` | Footer fix |
| Settings that persist | `toggleSetting()` → `common/env_writer.py` | **Not** for the theme: see below |

### Duplicated, consolidate (the main debt)

| What | Copies today | Consolidation |
|---|---|---|
| `fmtNum()` | **3**, with two behaviours: two abbreviate (`96.9K`), one uses `toLocaleString` (`96,995`) | One `fmtNum` (full, separators) and one `fmtCompact` (K/M, for axes and tight cells), in the shared head. Delete the page copies |
| `fmtMs()` | **2**, different output: `340ms`/`1.2s` vs always seconds | One, in the shared head |
| Other formatters | `fmtTime`, `fmtDate`, `fmtDay`, `fmtShortDate`, `fmtDuration`, `fmtElapsed`, `formatGB`, `timeAgo`, each page-local | Move to the shared head when the page that owns them is redesigned |
| Chart colour object `const C` + `chartDefaults` | **4** copies, already drifting (`textDim` vs `dim`, `border` vs `grid`, a hex fallback in one) | One `chartTheme()` in the shared head that reads the tokens. It must be re-run on an appearance change, because `getComputedStyle` is read once at load today |
| `MODEL_COLORS` | Model Insights page, five hard-coded hexes beside tokens | Series colours become tokens |
| Colour scales | `barColor` (warning), `utilizationColor` (utilization), `barGradient` (purple→green, benchmark progress only), plus inline `>= 80 / >= 40` green/yellow/red thresholds on the overview | Two scales: utilization and warning. Progress bars use the accent. Inline thresholds go through one helper |
| Health thresholds | `scoreClass()` uses 80 / 50; overview cards use 80 / 40 | One helper, one set of thresholds |
| Status-dot markup | `statusDotHtml()` (Settings page, with inline styles and a broken `border-radius:50` with no unit) beside hand-written `<span class="status-dot">` | One `statusDot(status, label)` in the shared head, no inline style, returning dot plus word |
| Disclosure | `toggleDetail()` + an `.open` class, used once (Benchmarks) | Native `<details>/<summary>` for every collapse (idle models, INFO findings, node details). No JS, keyboard-accessible. Convert the Benchmarks use when that page is redesigned, so one pattern remains |
| Page-local `<style>` blocks | 6, beside `_SHARED_CSS` | Each page's block folds into token classes in that page's redesign PR |
| Inline `style="…"` | 169 | Same, per page |
| Radii | 7 values: 3, 4, 6, 8, 10, 12, 20 px | `--radius-1/2/3` with the concentric rule |
| Font sizes | 13 values from 9 to 28 px | `--fs-display/title/body/label` |
| Glow on status dots | `box-shadow: 0 0 8px` on online and degraded | Removed: a quiet dot, per rule 2 |

### Genuinely new (small, and justified)

- **`prefers-color-scheme` light tokens, `prefers-reduced-motion`,
  `prefers-reduced-transparency` / `prefers-contrast`.** None exist today.
- **`localStorage` for the manual appearance override.** Nothing in the dashboard
  uses it today, and Settings persists through `env_writer` to
  `~/.fleet-manager/env`. That is right for fleet settings and **wrong for
  appearance**: one person choosing light mode would switch every viewer's
  dashboard. Appearance is per viewer, so it is the one thing stored in the
  browser. Wrapped in try/catch, defaulting to the system setting.
- **Number easing on SSE updates (Phase 4).** No existing equivalent.

## Plan

Each phase ships on its own, keeps every existing URL working, and leaves the
dashboard fully usable in between.

### Phase 0: Foundations (no visible redesign yet)

The groundwork every later phase needs, plus one bug.

1. **Fix the footer version.** Replace the hard-coded `v0.1.0` with
   `fleet_manager.__version__` (already used by `app.py:270` and
   `health_engine.py:768`). Test: the rendered footer contains the installed version.
2. **Token layer in `_SHARED_CSS`.** Replace the 11 colour variables with semantic tokens:
   - surfaces: `--surface-0/1/2`, `--hairline`;
   - text: `--text-1/2/3`;
   - interaction: `--accent`;
   - status: `--ok`, `--warn`, `--fail`;
   - utilization: `--util-*`;
   - type scale: `--fs-display`, `--fs-title`, `--fs-body`, `--fs-label`.

   Define every token for dark under `:root`, and for light under
   `@media (prefers-color-scheme: light)`, guarded by
   `:root:not([data-theme="dark"])` so the manual override wins.
3. **Consolidate the number formatters.** Move `fmtNum` (full, with separators)
   and `fmtMs` into the shared head of `_dashboard_page`, beside `barColor`; add
   `fmtCompact` for K/M. Delete the 3 + 2 page-local copies in the same change.
   Not a new `fmt()`: the existing names stay, so call sites barely move.
4. **Base typography.** Keep the system font stack (already correct). Add
   `font-variant-numeric: tabular-nums` on `body`; add a `.figure` / `.label`
   pair for value and caption.
5. **Reduced-motion and focus.** Add the global `prefers-reduced-motion` block and
   a visible `:focus-visible` ring using `--accent`.
6. **One chart theme.** Replace the four `const C` + `chartDefaults` copies with a
   single `chartTheme()` in the shared head that reads the tokens, so the light
   tokens reach every chart.
7. **One status dot.** Replace `statusDotHtml()` and the hand-written spans with
   `statusDot(status, label)`; drop the inline styles (including the broken
   `border-radius:50`) and the glow.
8. **Radius scale.** `--radius-1/2/3` tokens, with the concentric rule written down
   beside them, replacing the per-component radii.

**Exit:** visually the same in dark mode, but every colour is a token, light mode
renders legibly (not yet polished), the footer is correct, and counts have
separators.

### Phase 1: The home page (`/dashboard`)

The page most people look at most often; this is where the redesign earns its keep.

1. **Status line, first.** One sentence plus the health score:
   *"Healthy · 1 node online · 6 models loaded · 5 queued"*. Its colour is driven
   by the worst open health finding: green, amber, or red for CRITICAL.
   - The data already exists: `/dashboard/api/health` gives the score and the
     findings, and the SSE stream gives the counts.
   - A test asserts the status picks the worst severity, and that INFO findings
     never turn it amber.
2. **Key figures row.** Four or five large numbers: requests in the last 24 h,
   time to first token, error rate, queued, decode speed.
   - **Decode speed is shown as the fast-mode share, not the median.** CLAUDE.md
     explains why: the decode distribution is bimodal, and the median sits
     between the two modes.
   - Each figure links to the page that explains it.
3. **Nodes.** One calm row per node: status dot, name, chip, CPU and memory bars
   in utilization colours, and models resident. Details sit behind a disclosure.
   The current node card's information is kept; only its visual weight changes.
4. **Active models.** Collapse uses native `<details>`, the one disclosure pattern
   (see the audit). Models with in-flight or recent requests get a full row:
   in-flight versus capacity, queued, completed, failures (red only if > 0), time
   to first token.
   - Idle models collapse into one line, *"8 idle models"*, expandable to a
     compact table.
   - Embedding, vision and rerank models are grouped under one heading instead of
     each having its own coloured chips.
5. **Fleet Intelligence moves below the fold.** It stays, collapsed by default to
   its first sentence once generated. It is never a loading state at the top of
   the page.

**Exit:** a first-time viewer can tell from the first screen alone whether the
fleet is healthy, what is busy, and whether anything is failing. Check it
against the 2026-11-15 screenshots.

### Phase 2: Navigation and chrome

1. **Translucent sticky header:** `position: sticky`, a translucent `--surface-0`,
   `backdrop-filter: saturate(180%) blur(20px)`, and a hairline bottom border. This
   is the only glass in the product.
   - It falls back to solid where `backdrop-filter` is unsupported, and under the
     reduced-transparency, high-contrast and manual fallbacks from rule 5.
   - **The tint is strong, close to solid, not just a blur.** Page content scrolls
     under the header, so the nav labels sit over moving text; a light tint would
     put glass over content after all.
   - **Contrast is checked at the worst case.** A translucent bar has no single
     colour, so blend the header tint at its real transparency over the lightest
     and the darkest content that can scroll under it, and check the nav text
     against both results. A `--header-worst-case` pair of tokens records them, so
     the check is repeatable rather than eyeballed.
2. **Merge 8 tabs into 5:**

   | New tab | Pages it holds |
   |---|---|
   | **Overview** | the home page |
   | **Health** | Health + Recommendations, as one page with findings first and the capacity recommendations below |
   | **Models** | Model Insights + Trends: per-model figures with their trend charts |
   | **Usage** | Tags |
   | **Settings** | Settings, with Benchmarks as a section (a tool, rarely used) |

   - **Every old URL keeps working**, with the existing pattern: a second route
     decorator on the new handler, exactly as `/dashboard/apps` is kept for
     `/dashboard/tags`. Render, don't redirect, so there is one pattern.
   - Update `test_routes.py::…navigation links to all tabs` to the new set, and
     add a test per old URL.
3. **Footer** shrinks to one quiet line: version, the live-connection dot, and
   the alerts toggle.

### Phase 3: The remaining pages

Apply the same rules page by page, one PR each, in this order:
1. **Health:** the score ring stays; findings are sorted by severity; INFO is
   collapsed by default, because a standing list of no-action items is how a
   board stops being read (see `context_waste` in CLAUDE.md).
2. **Models and Trends:** Chart.js themed from the tokens, gridlines at hairline
   contrast, one series colour per metric, tooltips with formatted numbers.
3. **Usage (Tags):** tables with aligned numerals and sticky headers.
4. **Settings and Benchmarks.**
5. **`/dashboard/color-states`:** update it to show every token in both
   appearances, so it doubles as the visual reference for this plan.

While touching each page, move its inline `style="…"` attributes and local
`<style>` block into token-based classes. Don't do it as a separate sweep: each
page's cleanup ships with that page's redesign, so no PR is pure churn.

### Phase 4: Polish

- Number transitions on SSE updates, as an ease from old value to new; skipped
  under reduced motion.
- A favicon and tab title that reflect status: extend the existing
  `setTabAlert()`, which only knows "offline" today, to degraded and critical.
- A phone-width pass. Operators check fleets from a phone: single column, at
  least 16px side gutters, no horizontal scroll.

## Verification, per phase

- **Unit tests** wherever a phase adds logic: the status-line severity rule, the
  number formatter, the old-URL redirects, and the footer version.
- **Visual check in the browser pane at three sizes:** desktop, 768 px and 375 px,
  in both appearances, each against the live fleet. Screenshots before and after
  go in the PR.
- **Console:** zero errors on every page, and the SSE stream still updating
  (watch a counter move).
- **Contrast:** body text at least 4.5:1 and figures at least 3:1, in both
  appearances, checked on the tokens rather than per page. For the translucent
  header, check against the two worst-case blends, not its nominal colour.
- **Fallbacks:** with reduced transparency and with high contrast emulated, the
  header renders solid.
- **Reduced motion:** with `prefers-reduced-motion: reduce` emulated, nothing animates.
- **All of the rendered checks are one script:**
  `uv run --with playwright python scripts/dashboard-contrast-sweep.py` against a
  running router. It measures each text node against the background composited
  from every layer beneath it, on every page in both appearances, then checks
  reduced motion at load and when it is switched mid-session. Exit status is the
  failure count. Run it on any styling change; see "Follow-up" under As built.

## Risks

- **A pretty but wrong palette.** `dashboard-color-semantics.md` exists because
  the obvious palette (busy = red) contradicts the product. Its two-axis rule is
  a constraint here, not a suggestion. Review every colour change against it.
- **Hiding a real problem behind calm.** Collapsing idle models and INFO findings
  must never hide a WARNING or CRITICAL; those always render expanded. A test
  should pin this.
- **Breaking bookmarks.** Covered by keeping every old URL.
- **One 5,779-line file.** Splitting `dashboard.py` into per-page modules is
  tempting but out of scope. It is a refactor with no user-visible change; do it
  separately if at all, before or after, never mixed into a redesign PR.

## Out of scope

- The ollamaherd.com site (separate repo, separate design system).
- New data or new endpoints. If a phase seems to need one, stop and file it separately.
- A frontend framework or build step.

## As built (2026-11-15)

All five phases shipped in one change rather than one PR per page, because merging
pages (Health + Recommendations, Models + Trends, Settings + Benchmarks) made
the per-page bodies depend on the shared helpers at the same moment; a page
half-converted would have redefined `fmtNum` beside the shared one, which is
the debt this plan exists to remove. Measured after: inline `style=` attributes
**169 to 6**, all data-driven (bar widths, per-series swatch colours); hard-coded
hex colours outside the token table **24 to 0**; `fmtNum` copies **3 to 1**, chart
colour objects **4 to 1**. `tests/test_server/test_dashboard_design.py` pins all
of it, so a page that reintroduces a copy or a hex colour fails the build.

**Where the build departed from the plan, and why:**

- **The status verdict is computed server-side** (`health_engine.fleet_status`,
  returned as `status` by `/dashboard/api/health`), not in the page. The plan
  wanted it unit-tested; Python can test it, page JavaScript cannot be tested in
  this repo's suite. The page adds one thing the server cannot know quickly: a
  node going offline in the 2 s live stream lifts the verdict before the 30 s
  health cache turns over.
- **Decode speed is not on the home page.** The plan asked for the fast-mode
  share; no endpoint reports it, and the plan's own rule was to stop and file
  rather than add data mid-redesign. The figure row uses what the health vitals
  already carry (requests, time to first token, error rate) plus live queue
  depth. Open follow-up: add the fast-mode share to the vitals.
- **The header's worst-case contrast is computed by the test, not stored as
  tokens.** Tokens nothing renders are debt; the test blends the header tint
  (`_HEADER_TINT_PCT`, asserted to match the CSS) over white and black and checks
  both. Measuring it moved the tint **82% to 88%**: at 82, light mode's nav text
  over dark content was 4.0:1.
- **`--text-3` was darkened** so it clears 4.5:1 on every surface it sits on,
  because it ended up carrying small de-emphasised text, not only large figures.
- **The colour tokens are one Python dict** (`_TOKENS`) that generates the CSS
  for both appearances, so light values are written once, and the contrast test
  reads the same source the page renders.
- **Benchmark runs became a list of `<details>`** rather than table rows with a
  hidden detail row, because a disclosure cannot span table rows; this removed
  `toggleDetail`, the last hand-rolled collapse.
- **The Models summary now honours the time range** (a pre-existing bug found by
  the redesign's own range control): `latency_store.get_model_summary` takes
  `start_ts`/`end_ts`.
- **Old URLs render and then re-align** on the section a few times while the
  page settles (content above loads asynchronously and pushed the target down),
  stopping the moment the viewer scrolls.

**Verified** in the browser pane and in Chrome on the live fleet: every page in
dark and light, the four theme combinations (system light/dark, manual
override each way), the transparency fallback (blur to solid), 375 px width with
no horizontal scroll on any page, every old URL landing on its section, node and
idle disclosures surviving the 2 s live updates, and a real WARNING (a transient
connection failure after a router restart) turning the status line amber, the
tab title prefixed, and the finding expanded on Health with suggestions folded.

### Follow-up (2026-11-16): the token test was not enough

Reviewing ollamaherd.com's minimal-redesign plan, whose rendered contrast sweep
found failures Lighthouse never reported, the same method run here found two
that `test_dashboard_design.py` had passed, both in dark mode on Health:

- **The danger button** was white on `--fail`, **2.82:1**. The token test checks
  text tokens on the surfaces text sits on; a button sits on `--surface-2` with
  its own ink, a pair nobody had listed. Now red words on a neutral button
  (5.9:1 dark, 4.7:1 light), and the button pairs are tested.
- **"never" in an unused-model row** was **4.43:1**, because the row carried a
  6% warn tint. That tint also broke the colour rule: an unused model is not a
  fault, and its "never used" tag already says so. Removed.

A third finding was not contrast: `REDUCED_MOTION` was read once at load, so a
viewer who turned on Reduce Motion mid-session kept the animations until a
reload. It now follows the media query and rebuilds the charts on change.
Reduced motion had also never been *emulated* during the original verification,
only asserted in CSS; the sweep now does both.

Run against the pre-fix code, the script reports exactly these three; against
the fix it reports none. **Lesson: a pair test proves the pairs you thought of.
Measure what the browser paints.** One more number worth knowing: light `--ok`
on `--surface-2` is 4.47:1, so green text must not go on a button or other
`--surface-2` background without darkening the token first; nothing does today.

