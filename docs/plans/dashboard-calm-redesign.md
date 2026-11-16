# Dashboard calm redesign: glanceable, quiet, Apple-restrained

**Status:** Proposed, not yet scheduled
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
3. **Number formatting helper.** One JS function, `fmt(n, {unit})`, using
   `Intl.NumberFormat`. Every rendered count goes through it, so nothing is
   formatted by hand in two places.
4. **Base typography.** Keep the system font stack (already correct). Add
   `font-variant-numeric: tabular-nums` on `body`; add a `.figure` / `.label`
   pair for value and caption.
5. **Reduced-motion and focus.** Add the global `prefers-reduced-motion` block and
   a visible `:focus-visible` ring using `--accent`.
6. **Radius scale.** `--radius-1/2/3` tokens, with the concentric rule written down
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
4. **Active models.** Models with in-flight or recent requests get a full row:
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

   - **Every old URL keeps working.** `/dashboard/recommendations`,
     `/dashboard/trends` and `/dashboard/benchmarks` redirect (or render) into
     their new home, exactly as `/dashboard/apps` was kept for `/dashboard/tags`.
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
- A favicon and tab title that reflect status (the alert favicon already exists
  in `_FAVICON_SVG_ALERT`).
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
