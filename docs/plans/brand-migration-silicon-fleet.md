# Brand Migration — `ollama-herd` → Silicon Fleet

**Status**: Proposed
**Created**: 2026-07-13
**Owner**: TBD
**Related**: `CLAUDE.md` (release checklist, package identity), `docs/observations.md` (2026-07-11 GitHub-visibility entry)

---

## TL;DR

The project has outgrown its name. `ollama-herd` says "a helper that wrangles Ollama," but the thing we actually built is a **control plane for heterogeneous local AI compute** — it routes across Ollama, MLX, native embeddings, and vision backends; runs one model distributed across multiple Macs over Thunderbolt; scores 7 signals to pick the best machine; and runs a health engine + dashboard on top. The name undersells the vision and ties us to a third party's trademark.

**Recommendation:** rebrand to **Silicon Fleet**, migrated in layers — cheap/reversible surface changes now, package/CLI changes deferred to a clean major version. The name is available on every registry that matters. Do it now, while external adoption is near-zero and the switching cost is the lowest it will ever be.

This is not a coat of paint. It's a **repositioning**: from "Ollama add-on" to "the fleet command layer for local AI."

---

## Why now (the strategic window)

Three forces line up, and they won't stay aligned:

1. **The name-reality gap is now actively misleading.** Ollama is one backend of several. A newcomer reading `ollama-herd` mis-models what the project is and what it can do. Every doc, every chip prefix (`mlx:`), every distributed-inference feature widens the gap.
2. **Adoption is near-zero externally** — the 2026-07-11 review found ~17 page views / 14 visitors in two weeks, no external forks, issues, or mentions. **The cost of a rename is proportional to who depends on the old name.** Right now almost nobody does. In six months of growth, that won't be true. This is the cheapest rename we will ever get to make.
3. **"Ollama" is a third-party trademark.** Carrying it in our identity signals "plugin for X" and is a mild legal/brand liability as we position this as its own product. Dropping it is defensible and clarifying.

Counter-force to respect: we *just* fixed the "looks abandoned" problem (backfilled GitHub releases). A rename resets some hard-won recognition. Mitigation is baked into the phased plan below — redirects, an install-name alias, and a loud, dated announcement rather than a silent cutover.

---

## The creative opportunity

A rename is a rare chance to **claim a category** instead of describing a tool. Framing options and where they lead:

| Frame | Implicit ceiling | Example line |
|---|---|---|
| "Ollama manager" (today) | a helper for one backend | *"herd your Ollama instances"* |
| "MLX/Apple tool" | one hardware family | *"run MLX across your Macs"* |
| **"Fleet command for local AI"** (Silicon Fleet) | **every machine, every backend, one brain** | *"run local AI across every machine you own"* |

Silicon Fleet lets the project mean the biggest true thing about it: **you own silicon; this commands it.** The router is the brain; your machines are the fleet; the workload is inference. That story scales to more backends, more hardware, more nodes without another rename.

### The metaphor is a gift — lean into it

"Fleet" isn't just a word here; it's a **coherent naval metaphor system** that maps cleanly onto the architecture and even onto MLX's own vocabulary (MLX already calls distributed workers **ranks**, and rank 0 already "serves"). Used as a documentation + UX throughline, it makes the whole product legible and a little delightful:

| Concept in the system | Fleet term | Already true? |
|---|---|---|
| The whole set of your machines | **the fleet** | — |
| Router node (scoring + queues + dashboard) | **flagship** | it already commands |
| A worker node (`herd-node`) | **vessel / ship** | — |
| Distributed MLX ranks | **ranks** | ✅ MLX term already |
| A multi-Mac distributed cluster | **convoy** | — |
| Heartbeat / capability payload | **manifest** | it already reports capacity |
| Node joining via mDNS | **muster / enlist** | ✅ auto-discovery already |
| Drain / maintenance | **dry dock** | ✅ drain already exists |
| Health checks / status | **signal flags** | ✅ health engine already |
| Routing decision (pick a machine) | **set a bearing / chart** | ✅ scorer already |

This is an **optional flavor layer** — adopt as much as delights without forcing churn. The pragmatic recommendation (below) keeps the CLI as `herd`/`herd-node`, and "you *herd* your *fleet*" reads fine. The metaphor pays for itself in docs, the dashboard, error messages, and release notes, at zero engineering cost.

### Turn the one weakness into the positioning

The audit found "fleet" leans semantically toward **management** (device/vehicle fleets — Fleet Inc., Intel's "silicon-based fleet management," Samsara). That pull is real. The fix isn't a different name; it's a **tagline that plants the flag on AI** on every first-impression surface. Done right, the "command your machines" connotation becomes an asset, not a liability.

---

## Name decision: Silicon Fleet

### Why it wins

- **Says the vision**: "silicon" = the compute (Apple Silicon today, any silicon tomorrow), "fleet" = many machines, one command. Matches our internal `fleet_manager` package — the guts already speak this language.
- **Available everywhere** (see below) — we can actually own it.
- **Memorable, pronounceable, ownable** as a two-word mark; not a generic single word ("Fleet" alone collides with JetBrains Fleet and is unownable).
- **Drops the third-party trademark.**

### Alternatives considered

- **Herd** (keep the CLI equity, drop "ollama") — lowest churn, cross-platform-neutral, but generic and doesn't signal the vision; easy to collide with.
- **Silicon Herd** — keeps "herd" recognition + adds "silicon," but "herd" undercuts the fleet/command framing and mixes metaphors (herding vs. commanding).
- **Invented word** (e.g., a coined mark) — strongest legal distinctiveness, but zero descriptive pull and a cold start on meaning; more marketing lift than a two-person project wants.

Silicon Fleet is the best trade of *available* × *on-strategy* × *low-internal-churn*.

### Availability (researched 2026-07-13)

| Surface | Status |
|---|---|
| PyPI `silicon-fleet` / `siliconfleet` / `silicon-herd` | **all available** |
| npm `silicon-fleet` / `siliconfleet` | **available** |
| crates.io `silicon-fleet` | **available** |
| GitHub org/user `silicon-fleet` / `siliconfleet` | **available** |
| `siliconfleet.ai` / `.dev` / `.io` | **unregistered** |
| `siliconfleet.com` | parked / for-sale (GoDaddy) — no operating business |
| "Silicon Fleet" as a registered product / company / trademark | **none found** in software/AI |

### Known neighbors (no clash, but shape the tagline)

- **Fleet Inc. / fleetdm.com** — $52M open-source *device* management. Different product; same word.
- **JetBrains Fleet** — an IDE. Different space.
- **Intel vPro "silicon-based fleet management"** — closest semantic neighbor; it's *endpoint* management. This is exactly why the tagline must say "AI / inference."
- **`aulafy/mi`** (GitHub, 1★, mid-2026) — "Local-first distributed inference for Apple Silicon fleets." Our exact niche, named "mi" not Silicon Fleet. Validates the descriptor; not a collision.

**Verdict: available — yes, unambiguously. Good — yes, provided the tagline carries the AI meaning.**

---

## Positioning & taglines

Primary tagline (leads every first-impression surface — README h1, GitHub description, social):

> **Silicon Fleet — run local AI across every machine you own.**

Alternates by emphasis:
- Distributed / scale: *"Command a fleet of Macs as one local-AI supercluster."*
- Routing brain: *"One endpoint. Every local model. The best machine, every time."*
- Sovereignty / cost: *"Your hardware, your models, your fleet — no cloud bill."*

One-line GitHub description (replaces the current):
> *Fleet command for local AI — route, scale, and distribute inference across Ollama, MLX, and more, on every machine you own.*

Elevator (README opening paragraph):
> Silicon Fleet turns the Macs and PCs you already own into one local-AI cluster. It routes each request to the best machine across Ollama, MLX, and native embedding backends; pools memory across nodes to run models no single machine can hold; and keeps the whole fleet healthy from one dashboard. Two commands, zero config files.

---

## Naming architecture — what changes, what stays

The genius of our position: **the internals already say `fleet`.** So "Silicon Fleet" is mostly a *surface* rename.

| Layer | Today | Under Silicon Fleet | Change cost | Recommendation |
|---|---|---|---|---|
| Display / brand | "ollama-herd" | **Silicon Fleet** | trivial | **Now** |
| Tagline / positioning | (implicit) | see above | trivial | **Now** |
| GitHub repo | `ollama-herd` | `silicon-fleet` | low (redirects) | **Now/soon** |
| Domain | existing (kept) | existing + optional `siliconfleet.ai` redirect | low | keep; reserve `.ai` |
| PyPI package | `ollama-herd` | `silicon-fleet` (+ `ollama-herd` alias/shim) | **high** (no in-place rename) | **defer to v1.0** |
| Homebrew tap | `homebrew-ollama-herd` | `homebrew-silicon-fleet` | medium (separate repo) | with the PyPI cut |
| CLI | `herd` / `herd-node` | **keep** (`herd` = "herd your fleet") | — | **keep** — short, distinctive, on-metaphor |
| Env vars | `FLEET_*` / `FLEET_NODE_*` | **keep** — already fleet-flavored | — | **keep** — ages perfectly |
| Config dir | `~/.fleet-manager/` | **keep** | — | **keep** |
| Python package | `fleet_manager` | **keep** | — | **keep** |

The only genuinely expensive move is the PyPI package name, and PyPI **cannot rename in place** — you publish a new distribution and deprecate the old. Everything else is either free or a redirect. The CLI/env/internals **already speak "fleet"** and should not change — churning them would break every existing deploy for no brand gain.

---

## Migration plan (phased by layer)

### Phase 0 — Reserve the names (do immediately, ~an hour)

Cheap insurance; prevents squatting the moment we announce. Independent of the decision to fully commit.

- Register on PyPI: publish a `0.0.0` placeholder for `silicon-fleet` (README = "reserved for the project formerly known as ollama-herd").
- Reserve the npm name and the `silicon-fleet` GitHub org.
- Register `siliconfleet.ai` and `siliconfleet.dev`.
- Optionally price `siliconfleet.com` (parked) — not required since we keep our domain.

### Phase 1 — Surface rebrand (no install breakage)

The visible identity flips; nothing a user has installed breaks.

- README: new h1, tagline, elevator paragraph, badges.
- GitHub repo **description + topics** (`local-ai`, `apple-silicon`, `mlx`, `distributed-inference`, `llm-router`, `fleet`).
- Docs: retitle the landing docs; add a short "Silicon Fleet is the project formerly known as ollama-herd" note; weave the fleet metaphor where it adds clarity.
- Dashboard title / header wordmark.
- A one-time announcement (README banner + a dated `CHANGELOG` note): *"We're now Silicon Fleet. Same project, bigger vision. `pip install ollama-herd` keeps working; `silicon-fleet` is coming at v1.0."*

### Phase 2 — Repo rename

- Rename the GitHub repo `ollama-herd` → `silicon-fleet`. GitHub issues permanent redirects, so old links and `git remote`s keep working. Update the remote in local clones.
- Update all in-repo absolute links that assume the old repo slug.

### Phase 3 — Package migration (bundle into the next major version, e.g. v1.0)

One clean break, not a slow bleed:

- Publish `silicon-fleet` on PyPI as the primary package (same code, `[project] name = "silicon-fleet"`).
- Turn `ollama-herd` into a **transitional alias**: a final release whose description says "renamed to silicon-fleet," optionally a meta-package that depends on `silicon-fleet` so `pip install ollama-herd` still lands the code for a deprecation window. (Note: PyPI has no first-class "rename/redirect"; the alias is the standard workaround.)
- New Homebrew tap `geeks-accelerator/homebrew-silicon-fleet` with a `silicon-fleet` formula; leave a deprecation note on the old tap.
- **Update the release checklist in `CLAUDE.md`** for the new package + tap names (the checklist is the only thing every releaser follows; if it isn't updated, the rename half-happens — see the 2026-07-11 observation about checklist blind spots).

### Phase 4 — Internals (probably never)

`FLEET_*`, `~/.fleet-manager/`, `fleet_manager`, `herd`/`herd-node` already align with the new brand. **Do not rename them.** Reconsider only at a hypothetical v2 that already forces a breaking migration for other reasons.

---

## Gotchas & mechanics

- **PyPI can't rename** — new name + deprecate old is the only path; download history splits across the two names. Plan the alias/shim so `pip install ollama-herd` doesn't dead-end mid-window.
- **Homebrew tap is a separate repo** — its own commit + the non-negotiable end-to-end install test from the release checklist (uninstall + untap + retap + install + import).
- **SEO / recognition reset** — search equity built on "ollama-herd" doesn't transfer automatically. Mitigate with the GitHub redirect, the PyPI alias, a dated announcement, and keeping the old name discoverable ("formerly ollama-herd") in the README for a few releases.
- **Skills reference the old name** — the 37 ClawHub skills and any `grep -rn "ollama-herd"` hits need a sweep (mirror the existing `grep -rn "1006 tests"` maintenance pattern).
- **Don't silently cut over** — a quiet rename reads as a *different, abandoned* project. Announce it, date it, and cross-link old↔new everywhere for a window.
- **The `mlx:` model prefix and other wire-format identifiers stay** — they're API surface, not brand.

---

## Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| "Fleet" reads as device/vehicle management | Medium | Tagline leads with "local AI / inference" on every surface; the naval framing reinforces *command*, not *asset tracking* |
| Recognition reset after fixing the "abandoned" look | Low-Med | Do it now while tiny; redirects + alias + dated announcement; "formerly ollama-herd" for a few releases |
| Split download stats confuse trend-watching | Low | Track both names during the deprecation window; note the cutover date in soak checks |
| Half-migrated state (package renamed, docs/tap not) | Med | Everything gated on updating the `CLAUDE.md` release checklist first; phased order keeps each phase shippable |
| Someone squats `silicon-fleet` before we announce | Low | Phase 0 reserves every handle immediately |
| A future project actually named "Silicon Fleet" appears | Low | We'd hold first use across PyPI/npm/GitHub/domains; revisit trademark only if it becomes commercially material |

---

## Open questions (decide before Phase 1)

1. **Commit or reserve-only?** Phase 0 is worth doing regardless. Phases 1+ need a "yes, we're Silicon Fleet."
2. **How hard to lean on the naval metaphor** — light touch (docs flavor) vs. full throughline (dashboard, CLI help, error copy)?
3. **PyPI cut timing** — pair with the next planned major version, or make the rename *itself* the reason to cut v1.0?
4. **Keep `ollama-herd` installable forever, or sunset after N releases?**
5. **Grab `siliconfleet.com`** from the parker, or is `.ai`/`.dev` + the existing domain enough?

---

## Non-goals

- Renaming `herd`/`herd-node`, `FLEET_*`, `~/.fleet-manager/`, or `fleet_manager` — they already fit and churning them breaks deploys for zero brand gain.
- A big-bang cutover — every phase must leave the project shippable and installable.
- A full visual-identity project (logo/wordmark system) — worth doing eventually (a fleet wants an ensign), but out of scope for this migration.
- Changing wire-format identifiers (`mlx:` prefix, env var names, API routes).
