# Brand Migration — `ollama-herd` → **Inference Herd**

**Status**: Proposed
**Created**: 2026-07-13 (as "Silicon Fleet")
**Superseded**: 2026-07-17 — pivoted to **Inference Herd** after keyword research invalidated the Silicon Fleet recommendation. The earlier reasoning is kept in **Decision history** on purpose: *how* it was wrong is the reusable part.
**Related**: `CLAUDE.md` (release checklist, package identity), `docs/observations.md` (2026-07-11 GitHub-visibility entry)

---

## TL;DR

The project has outgrown its name. `ollama-herd` says "a helper that wrangles Ollama," but what we built is a **control plane for heterogeneous local AI compute** — it routes across Ollama, MLX, native embeddings, and vision backends; runs one model distributed across multiple Macs; scores 7 signals to pick the best machine; and runs a health engine + dashboard on top. The name undersells the vision and carries a third party's trademark.

**Recommendation: rename to `inference-herd` (display: "Inference Herd").** Swap the one bad word; keep everything that works.

It is the cheapest defensible rename available:

- **Drops the trademark** — the actual goal.
- **Upgrades the worst keyword in the name.** "ollama" welds us to one backend; **"inference" is the highest-intent search term for what we do**, and it's backend-neutral.
- **Keeps `herd`** — the CLI (`herd`, `herd-node`), the recognition, and a word **nobody else in AI infra uses**. Zero CLI churn, no muscle-memory reset, no shims for the command surface.
- **Verified clean** on PyPI, npm, GitHub org (both spellings), and `.ai`/`.dev`/`.io` — and **no project owns the phrase "inference herd."**

Net change: **one word in the package/repo name, plus branding.** Everything expensive stays put.

---

## Why now (the strategic window)

1. **The name-reality gap is actively misleading.** Ollama is one backend of several. A newcomer reading `ollama-herd` mis-models what this is. Every `mlx:` chip and distributed-inference feature widens the gap.
2. **Adoption is near-zero externally** — the 2026-07-11 review found ~17 page views / 14 visitors in two weeks, no external forks, issues, or mentions. **The cost of a rename is proportional to who depends on the old name.** Almost nobody does yet.
3. **"Ollama" is a third-party trademark.** Carrying it signals "plugin for X" and is a mild legal/brand liability.

Counter-force: we just fixed the "looks abandoned" problem (backfilled GitHub releases). Mitigations are below — redirects, an install alias, a loud dated announcement. Keeping `herd` retains most recognition anyway.

---

## Decision history — three candidates, and why the first two died

Recorded because the *method* matters more than the answer, and because two confident recommendations (both mine) failed on contact with data.

### ❌ Silicon Fleet (original recommendation, 2026-07-13)

Researched as available everywhere and recommended. **Two later findings killed it:**

1. **"Fleet + AI" already means vehicle telematics.** [fleetai.com](https://www.fleetai.com/), [The AI Fleet Inc](https://www.crunchbase.com/organization/the-ai-fleet-inc) (AI trucking), [CoRun.ai](https://www.businesswire.com/news/home/20251112094813/en/German-Efficiency-Meets-Silicon-Valley-AI-CoRun.ai-Raises-$3.5-Million-to-Power-Intelligent-Profitable-Fleets) ("Silicon Valley AI … Profitable Fleets", $3.5M), [Fleetio](https://www.fleetio.com/). The original mitigation — *"the fix isn't a different name, it's a tagline that plants the flag on AI"* — **is backwards**: the collision is specifically at *fleet + AI*, so leading with "AI" walks you **into** it.
2. **"Silicon" carries zero search intent.** Nobody looking for this types "silicon." The word belongs to semiconductor firms (Cisco Silicon One, Arm, Synopsys "silicon to systems", Silicon Labs) — it reads as *chip vendor*, not software you run.

### ❌ Flotilla / Gaucho / Distributed Silicon (rejected 2026-07-17)

- **Flotilla** — recommended three times before checking. [pimoroni/flotilla](https://github.com/pimoroni/flotilla) is a **Python library + daemon + hardware ecosystem** in the adjacent Raspberry Pi/maker space. Direct namespace + semantic collision. *Recommending before verifying is the recurring failure mode in this doc.*
- **Distributed Silicon** — available, but generic (unownable) and reads as a chip company. Describes the substrate, not the product. Nobody searches "distributed silicon"; they search "distributed inference."
- **Gaucho** — charming lineage (a gaucho herds llamas → drops the trademark, keeps the wink), but npm, GitHub org, and `.ai`/`.dev`/`.io` are **all taken**; it has zero keywords; and it **re-anchors the identity to the llama/Ollama era** we're leaving. Compounds (`gaucho-fleet`, `distributed-gaucho`) are free but mix metaphors (a gaucho herds on land; a fleet sails) — the same flaw this doc already rejected in "Silicon Herd."

### ✅ Inference Herd (2026-07-17)

Wins both axes, which nothing else did.

---

## The research that decided it

### The trap: the biggest keywords are already owned

Naming yourself the highest-volume descriptor doesn't win search — it buries you under whoever already ranks for it. GitHub and Google both rank on authority (stars, links).

| Phrase | Incumbent |
|---|---|
| "distributed inference" | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo) **7,511★**, [evilsocket/cake](https://github.com/evilsocket/cake) **3,097★** |
| "local inference" | [antirez/ds4](https://github.com/antirez/ds4) **18,692★**, signerlabs/Klee **1,747★** |
| "inference cluster" | K2/olol **39★** — *"Ollama⇄Ollama Inference Cluster"* (our exact niche) |
| "inference mesh" | redbco/infermesh **35★** — *"GPU-aware inference mesh"* |

**So `Distributed Inference` and `Local Inference` — the two most obvious descriptive picks — are the worst available choices.** You don't outrank an 18.7k★ repo by naming yourself its description.

### Availability sweep (verified 2026-07-17)

| Candidate | PyPI | npm | GitHub org | Domains | Phrase owned? |
|---|---|---|---|---|---|
| **inference-herd** | ✅ free | ✅ free | ✅ free (both spellings) | ✅ `.ai` `.dev` `.io` free | ✅ **nobody** |
| llm-herd | ✅ | ✅ | ✅ | ✅ | ~ (one 10★ unrelated) |
| local-herd / ai-herd / model-herd / gpu-herd / silicon-herd | ✅ | ✅ | ✅ | — | ✅ |
| inference-fleet | ✅ | ✅ | ✅ | ✅ | ✅ nobody |
| inference-router / llm-router / llm-cluster / local-llm-router | ❌ **taken** | ❌/✅ | — | — | — |
| gaucho | ✅ | ❌ taken | ❌ taken | ❌ all taken | — |

**The entire `*-herd` family is free** — nobody names AI infrastructure "herd." We effectively coined it, and that equity is ours to keep.

### Why "inference" over the alternatives

- **inference** ✅ — highest search intent, describes the function, backend-neutral, survives every future backend.
- `llm` — narrows us (we also route embeddings, vision, image gen).
- `gpu` — factually wrong (Metal / unified memory, not GPU-centric).
- `ai` / `model` — noise.
- `silicon` — not a search term; reads as chip vendor.

### Why this resolves the brand-vs-discovery tension

Descriptive names are findable but unownable; brandable names are memorable but invisible to search. The successful hybrids here are **keyword-core + short distinctive modifier** (vLLM, LiteLLM) — *not* brand-first-keyword-suffix (which is why `Gaucho Inference` fails: "gaucho" is a cold-start word we'd have to teach).

**Inference Herd is keyword-first with a modifier we already own.** "Herd" needs no teaching — it's the CLI, it's the existing identity, and it's unclaimed in AI infra. That's why it gets both axes when nothing else did.

---

## Positioning & taglines

Primary (README h1, GitHub description, social):

> **Inference Herd — run local AI across every machine you own.**

Alternates by emphasis:
- Distributed / scale: *"Herd every Mac you own into one local-AI supercluster."*
- Routing brain: *"One endpoint. Every local model. The best machine, every time."*
- Sovereignty / cost: *"Your hardware, your models, your herd — no cloud bill."*

One-line GitHub description (keyword-dense — this field carries more search weight than the repo name):
> *Distributed local LLM inference — route and scale across Ollama, MLX, and native backends on every machine you own.*

Elevator (README opening):
> Inference Herd turns the Macs and PCs you already own into one local-AI cluster. It routes each request to the best machine across Ollama, MLX, and native embedding backends; pools memory across nodes to run models no single machine can hold; and keeps the whole herd healthy from one dashboard. Two commands, zero config files.

**Keyword surfaces matter more than the name** — GitHub ranks mostly on topics + description + stars; PyPI indexes `summary`/`keywords`. Set topics: `local-ai`, `llm`, `inference`, `distributed-inference`, `apple-silicon`, `mlx`, `ollama`, `llm-router`. (`ollama` stays as a *topic* — an accurate keyword for what we integrate, just not our identity.)

> **Metaphor note:** the naval vocabulary drafted for Silicon Fleet (flagship / vessels / convoy / dry dock) does **not** survive this pivot — "herd" is the metaphor now. A small, deliberate loss. Herding has its own light vocabulary (herd, stray, wrangle, roundup, brand) if flavor is ever wanted, but keyword clarity beats metaphor theater. `ranks` stays correct for distributed MLX — it's MLX's own term, not ours.

---

## Naming architecture — what changes, what stays

**The whole point: almost nothing changes.** A one-word swap plus branding.

| Layer | Today | Under Inference Herd | Change cost | Recommendation |
|---|---|---|---|---|
| Display / brand | "ollama-herd" | **Inference Herd** | trivial | **Now** |
| Tagline / positioning | (implicit) | see above | trivial | **Now** |
| GitHub repo | `ollama-herd` | `inference-herd` | low (redirects) | **Now/soon** |
| GitHub topics/description | thin | keyword-dense (above) | trivial | **Now** — biggest SEO lever |
| Domain | existing | existing + optional `inferenceherd.ai` | low | reserve `.ai`/`.dev` |
| PyPI package | `ollama-herd` | `inference-herd` (+ alias) | **high** (no in-place rename) | **defer to v1.0** |
| Homebrew tap | `homebrew-ollama-herd` | `homebrew-inference-herd` | medium (separate repo) | with the PyPI cut |
| **CLI** | `herd` / `herd-node` | **unchanged** | **none** | **keep** — now *more* on-brand |
| Env vars | `FLEET_*` / `FLEET_NODE_*` | **unchanged** | none | **keep** |
| Config dir | `~/.fleet-manager/` | **unchanged** | none | **keep** |
| Python package | `fleet_manager` | **unchanged** | none | **keep** |

The only genuinely expensive move is the PyPI package name (PyPI **cannot rename in place**). Everything else is free or a redirect.

> **Known cosmetic inconsistency:** brand says "herd," internals say `fleet` (`fleet_manager`, `FLEET_*`, `~/.fleet-manager/`). **Invisible to users**, and not worth a refactor that would break every deploy. Accepted deliberately.

---

## Migration plan (phased by layer)

### Phase 0 — Reserve the names (do immediately, ~an hour)

Cheap insurance against squatting the moment we announce; independent of full commitment.

- PyPI: publish a `0.0.0` placeholder for `inference-herd` (README = "reserved for the project formerly known as ollama-herd").
- Reserve npm `inference-herd` and the `inference-herd` GitHub org.
- Register `inferenceherd.ai` + `inferenceherd.dev`.

### Phase 1 — Surface rebrand (no install breakage)

- README: new h1, tagline, elevator, badges.
- **GitHub repo description + topics** — the highest-leverage SEO change in this whole doc.
- Docs: retitle; add "Inference Herd is the project formerly known as ollama-herd."
- Dashboard title / header wordmark.
- Announcement (README banner + dated `CHANGELOG` note): *"We're now Inference Herd. Same project, same `herd` command, no Ollama lock-in. `pip install ollama-herd` keeps working; `inference-herd` arrives at v1.0."*

### Phase 2 — Repo rename

- Rename GitHub repo `ollama-herd` → `inference-herd`. GitHub issues permanent redirects, so old links and `git remote`s keep working.
- Update in-repo absolute links that assume the old slug.

### Phase 3 — Package migration (bundle into the next major version, e.g. v1.0)

- Publish `inference-herd` on PyPI as primary (`[project] name = "inference-herd"`).
- Turn `ollama-herd` into a **transitional alias** — a final release whose description says "renamed to inference-herd," optionally a meta-package depending on `inference-herd` so `pip install ollama-herd` still lands the code for a deprecation window. (PyPI has no first-class rename; the alias is the standard workaround.)
- New tap `geeks-accelerator/homebrew-inference-herd`; deprecation note on the old tap.
- **Update the release checklist in `CLAUDE.md`** for the new package + tap names — the checklist is the only thing every releaser follows; if it isn't updated, the rename half-happens (see the 2026-07-11 observation on checklist blind spots).

### Phase 4 — Internals (never)

`FLEET_*`, `~/.fleet-manager/`, `fleet_manager`, `herd`/`herd-node` all stay. **Do not rename them.**

---

## Gotchas & mechanics

- **PyPI can't rename** — new name + deprecate old; download history splits. Plan the alias so `pip install ollama-herd` doesn't dead-end mid-window.
- **Homebrew tap is a separate repo** — its own commit + the non-negotiable end-to-end install test (uninstall + untap + retap + **trust** + install + import). The `brew trust` step is required on Homebrew 6.x for any third-party tap; without it a fresh install stops at "untrusted tap". A renamed tap starts untrusted even if the old one was trusted.
- **SEO reset is partial, not total** — we keep `herd`, so herd-based recognition survives. Mitigate the rest with the GitHub redirect, PyPI alias, dated announcement, and "formerly ollama-herd" in the README for a few releases.
- **Skills reference the old name** — the 37 ClawHub skills need a sweep: `grep -rn "ollama-herd" skills/` (mirror the existing `grep -rn "1077 tests"` maintenance pattern).
- **Don't silently cut over** — a quiet rename reads as a *different, abandoned* project. Announce it, date it, cross-link old↔new.
- **`ollama` stays where it's accurate** — the `mlx:` prefix, wire formats, env vars, and the `ollama` GitHub *topic* are API surface / true keywords, not brand. Only the identity drops it.

---

## Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| "Herd" reads as generic / unserious | Low-Med | "Inference" carries the meaning; herd is *ours* and unclaimed in AI infra — that distinctiveness is the asset |
| Descriptive name is unownable (no trademark moat) | Med | Accepted deliberately — discovery was chosen over defensibility. `herd` supplies what distinctiveness exists |
| We don't outrank incumbents for "inference" | Med | We're not trying to — we own the **unclaimed** phrase "inference herd" and win the long tail via topics/description |
| Split download stats confuse trend-watching | Low | Track both names during the deprecation window; note the cutover date in soak checks |
| Half-migrated state (package renamed, docs/tap not) | Med | Gate everything on updating the `CLAUDE.md` release checklist first; each phase ships independently |
| Someone squats `inference-herd` before we announce | Low | Phase 0 reserves every handle immediately |

---

## Open questions (decide before Phase 1)

1. **Commit or reserve-only?** Phase 0 is worth doing regardless. Phases 1+ need a "yes, we're Inference Herd."
2. **PyPI cut timing** — pair with the next major version, or make the rename *itself* the reason to cut v1.0? (Note 0.8.2 is unpublished; the accumulated 0.8.x could ship first under the old name.)
3. **Keep `ollama-herd` installable forever, or sunset after N releases?**
4. **Reserve `inference-fleet` defensively?** It also came back clean and is the nearest neighbor.

---

## Non-goals

- Renaming `herd`/`herd-node`, `FLEET_*`, `~/.fleet-manager/`, or `fleet_manager` — they work, and churning them breaks deploys for zero gain.
- A big-bang cutover — every phase must leave the project shippable and installable.
- A full visual-identity project (logo/wordmark) — worth doing eventually, out of scope here.
- Changing wire-format identifiers (`mlx:` prefix, env var names, API routes).
- Chasing a trademark moat — descriptive was chosen knowingly.
