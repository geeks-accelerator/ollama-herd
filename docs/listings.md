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

## Package registries and other channels

| Channel | Where |
|---------|-------|
| PyPI | [ollama-herd](https://pypi.org/project/ollama-herd/) — `pip install ollama-herd`; download history in [`pypi-download-stats.md`](pypi-download-stats.md) |
| Homebrew | [geeks-accelerator/homebrew-ollama-herd](https://github.com/geeks-accelerator/homebrew-ollama-herd) — `brew tap`, `brew trust`, `brew install ollama-herd` |
| Website | [ollamaherd.com](https://ollamaherd.com) |
| ClawHub | 37 agent skills published from [`skills/`](../skills/) — strategy in [`skill-publishing-strategy.md`](skill-publishing-strategy.md) |

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
