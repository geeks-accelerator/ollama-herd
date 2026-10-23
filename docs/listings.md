# Where Ollama Herd is listed

Ollama Herd is an open-source (MIT) router for local AI inference: it routes LLM,
image generation, speech-to-text and embedding requests across a fleet of machines
you own — Macs, Linux and Windows boxes running Ollama or `mlx_lm.server` — and
serves OpenAI-, Ollama- and Anthropic-compatible APIs. This page tracks the curated
lists and directories that include it, what each entry says, and what we learned
submitting to them.

Last verified: **2026-10-23** — each merged entry below was checked on the list's
default branch, not only on the merged pull request.

## Curated lists

| List | Section | Status | Pull request |
|------|---------|--------|--------------|
| [awesome-mlx](https://github.com/antranapp/awesome-mlx) | Libraries and Tools | **Listed** (merged 2026-10-23) | [antranapp/awesome-mlx#20](https://github.com/antranapp/awesome-mlx/pull/20) |
| [Awesome-LLMOps](https://github.com/InftyAI/Awesome-LLMOps) | LLM Router | **Listed** (merged 2026-09-03) | [InftyAI/Awesome-LLMOps#522](https://github.com/InftyAI/Awesome-LLMOps/pull/522) |
| [awesome-ai-gateway](https://github.com/cuihuan/awesome-ai-gateway) | Self-hosted open source (English and Chinese READMEs) | **Listed** (merged 2026-08-28) | [cuihuan/awesome-ai-gateway#56](https://github.com/cuihuan/awesome-ai-gateway/pull/56) |
| [awesome-selfhosted](https://github.com/awesome-selfhosted/awesome-selfhosted) | — | **Not listed** — closed without comment 2026-08-16 | [awesome-selfhosted/awesome-selfhosted-data#2915](https://github.com/awesome-selfhosted/awesome-selfhosted-data/pull/2915) |

All four were submitted on 2026-08-16.

### What each entry says

The wording differs per list on purpose — each one leads with what that list's
readers are looking for.

- **awesome-mlx:** Routes LLM, image, speech-to-text and embedding requests across a
  fleet of Macs. Runs `mlx_lm.server` as a first-class backend alongside Ollama,
  auto-discovers nodes over mDNS, and picks a node by memory fit, thermal state,
  queue depth and which models are already loaded.
- **Awesome-LLMOps:** Routes LLM, image, speech-to-text and embedding requests across
  a fleet of machines you own. Auto-discovers nodes over mDNS and scores each on
  memory fit, thermal state, queue depth and already-loaded models. OpenAI-, Ollama-
  and Anthropic-compatible.
- **awesome-ai-gateway:** A self-hosted router that routes to machines you own rather
  than to hosted providers — LAN discovery over mDNS with no config file, per-request
  scoring that avoids cold-loading a model already warm elsewhere, and no provider
  keys to manage because there is no provider.

### awesome-selfhosted

The pull request was closed by `kokomo123` 31 minutes after it was opened,
with no comment, so the reason is not recorded. It was not the list's age rule —
"first released more than 4 months ago" — because `0.1.0` reached PyPI on
2026-03-11, five months before submission.

**Do not resubmit with an agent.** Since then the list's `CONTRIBUTING.md` has
formalized two rules: the submitter must attest that *the submission was done by a
human, not a machine/LLM*, and machine- or LLM-generated contributions *will result
in a ban*. A resubmission has to be written and filed by a person, and the
attestation is theirs to make. Their data repo takes one YAML file per project
under `software/`; the closed PR has the one we used.

## Candidates — not yet submitted

Researched 2026-10-23. Found mostly by searching GitHub for READMEs that already
list comparable projects (LiteLLM, GPUStack, exo, llama-swap, claude-code-router,
Ollama proxies) — a list that takes those has a section herd belongs in. Herd is
absent from every list below as of that date. Stars and activity are as of the same
day; re-check rules before submitting, because they change (see awesome-selfhosted).

**Human-submission only.** These forbid agent- or LLM-made submissions, so a person
writes and files them:

| List | Stars | Section | How to submit |
|------|------:|---------|---------------|
| [awesome-claude-code](https://github.com/hesreallyhim/awesome-claude-code) | 56k | Providers, Runtime & Integration Infrastructure | Web-UI issue form only, never a PR; "resource recommendations must be created by human beings", and misuse risks a temporary restriction. Fits because Claude Code runs against herd via `ANTHROPIC_BASE_URL` — see [`guides/claude-code-integration.md`](guides/claude-code-integration.md) |
| [awesome-selfhosted](https://github.com/awesome-selfhosted/awesome-selfhosted) | — | — | Resubmission; see above |

**Open to PRs, strong fit:**

| List | Stars | Section | Notes |
|------|------:|---------|-------|
| [ollama/ollama](https://github.com/ollama/ollama) README, Community Integrations | 184k | Infrastructure & Deployment | The README invites PRs, but the backlog is large: 15+ integration PRs opened in the week to 2026-10-23, and the last one merged was 2026-10-07. High value, low odds; a one-line entry costs little |
| [awesome-local-llm](https://github.com/rafska/awesome-local-llm) | 3.0k | Inference engines | Already lists exo, GPUStack, llama-swap, LocalAI. Plain PR, no special rules |
| [Awesome-LLMOps (tensorchord)](https://github.com/tensorchord/Awesome-LLMOps) | 6.0k | Frameworks/Servers for Serving | Lists GPUStack and vLLM. A different list from the InftyAI one herd is already on |
| [awesome-opensource-ai](https://github.com/alvinreal/awesome-opensource-ai) | 4.9k | High-performance Serving & API Servers | No star minimum. Format: one factual sentence plus a stars badge |
| [awesome-codex-cli](https://github.com/RoggeOhta/awesome-codex-cli) | 555 | Model Providers & Proxies | Fits because Codex runs against herd's `/v1/responses` — see [`guides/codex-integration.md`](guides/codex-integration.md). Requires a stars badge in their format |
| [awesome-cli-coding-agents](https://github.com/bradAGI/awesome-cli-coding-agents) | 1.4k | Agent infrastructure | Lists claude-code-router |

**Possible, weaker fit or extra bar:**

| List | Stars | Why weaker |
|------|------:|------------|
| [awesome-ai-tokenomics](https://github.com/QuesmaOrg/awesome-ai-tokenomics) | 206 | Has "Gateways and Proxies" and "Routing Model Selection", but requires verifiable cost/performance evidence and adoption beyond the author's own use. The measurements in `observations.md` may qualify |
| [Awesome-Ollama](https://github.com/EndoTheDev/Awesome-Ollama) | 492 | No infrastructure or routing section; would need "Libraries" or a new one |
| [Awesome-AITools](https://github.com/ikaijua/Awesome-AITools) | 6.2k | Mostly end-user apps; every entry needs both English and Chinese READMEs |
| [awesome-local-llms](https://github.com/vince-lam/awesome-local-llms) | 811 | Not hand-curated: generated daily from [llmrepos.com](https://llmrepos.com), which tracks 11k repos by star growth. Getting into that index is the path, not a PR |

**Not eligible yet:**

- **[awesome-production-machine-learning](https://github.com/EthicalML/awesome-production-machine-learning)** (21k) — requires 500 GitHub stars. Herd has 27.
- **homebrew/core** — the [acceptance policy](https://docs.brew.sh/Package-Acceptance-Policy) asks a self-submitted project for 225 stars, 90 forks or 90 watchers. The tap stays the install path.

**Outside GitHub:**

- **Hacker News (Show HN)** — never posted; a search of HN finds no story about herd.
  Show HN wants something people can try, and `pip install ollama-herd` qualifies.
  Post as a person, and be there to answer comments.
- **Reddit** — r/LocalLLaMA, r/ollama, r/selfhosted and r/MacStudio are where the
  users are. Each has self-promotion rules; read them first, and post as a person.
- **Directories** — [OpenAlternative](https://openalternative.co/submit) and
  [selfhostyourself.com](https://selfhostyourself.com) both have submit forms and
  list self-hosted AI tools. Their criteria aren't public before you submit.
  AlternativeTo lists gateways under LiteLLM's alternatives, so herd could appear
  there too.

## Package registries and other channels

| Channel | Where |
|---------|-------|
| PyPI | [ollama-herd](https://pypi.org/project/ollama-herd/) — `pip install ollama-herd`; download history in [`pypi-download-stats.md`](pypi-download-stats.md) |
| Homebrew | [geeks-accelerator/homebrew-ollama-herd](https://github.com/geeks-accelerator/homebrew-ollama-herd) — `brew tap`, `brew trust`, `brew install ollama-herd` |
| Website | [ollamaherd.com](https://ollamaherd.com) |
| ClawHub | 37 agent skills published from [`skills/`](../skills/), e.g. [ollama-ollama-herd](https://clawhub.ai/twinsgeeks/ollama-ollama-herd) — strategy in [`skill-publishing-strategy.md`](skill-publishing-strategy.md) |
| Skywork SkillHub | A third-party mirror of the ClawHub skills, e.g. [ollama-ollama-herd](https://skywork.ai/skillhub/ollama-ollama-herd/) |

## Submitting to a new list

What the four submissions above taught us:

- **The diff is one entry and nothing else.** The awesome-mlx maintainer asked twice
  — in August and again in September — for unrelated changes to be removed before
  merging. A branch cut from a stale fork carries other people's commits; branch
  from the list's current default branch.
- **Read `CONTRIBUTING.md` before writing anything.** Lists set age floors, required
  formats (awesome-selfhosted takes YAML, not a README line), alphabetical order,
  badge conventions, and increasingly a human-authorship requirement. A
  human-only list is not one to automate.
- **Match the section, not just the list.** "LLM Router" on Awesome-LLMOps and
  "Self-hosted open source" on awesome-ai-gateway are where a reader looking for
  this kind of tool already is.
- **Describe, don't sell.** Every merged entry states mechanisms — mDNS discovery,
  the scoring signals, the API surfaces — rather than adjectives.
- **Verify after merge, on the default branch.** Merged is not the same as present:
  lists get reorganized and entries pruned (awesome-selfhosted removes projects with
  no activity for 6–12 months).

Then add a row to the table above.

## Re-checking

```bash
# every Ollama Herd listing PR, any state
gh search prs "Ollama Herd" --limit 50 \
  --json repository,state,url -q '.[] | "\(.state)\t\(.url)"'

# is the entry still on a list's default branch?
curl -s https://raw.githubusercontent.com/antranapp/awesome-mlx/main/README.md \
  | grep -c geeks-accelerator/ollama-herd
```
