# Ollama Herd Skills

This directory contains 28 skills that let AI agents discover and manage Ollama Herd fleets. Multiple skills target different audiences, keywords, and devices with the same underlying API — see [Skill Publishing Strategy](../docs/skill-publishing-strategy.md) for the rationale and [Optimizing Skills for ClawHub](../docs/guides/optimizing-skills-for-clawhub.md) for the search ranking playbook.

## Skill Expansion Strategy

We publish many skills for one product. Each skill is a **different door** into the same fleet router — targeting a different audience, keyword, model family, device, or use case. This is intentional:

### The four tiers

| Tier | Purpose | How it works | Examples |
|------|---------|-------------|---------|
| **Core skills** (7) | Different audience framing | Same API, different voice (DevOps vs ML engineer vs home lab) | `ollama-load-balancer`, `gpu-cluster-manager` |
| **Modality skills** (3) | One per non-LLM model type | Primary capability leads, other 3 mentioned as "also available" | `local-transcription`, `mflux-image-router` |
| **Model-family skills** (9) | One per open-source LLM provider/version | Specific models, hardware recommendations, code examples | `llama-llama3`, `deepseek-deepseek-coder`, `qwen-qwen3-5` |
| **Device/use-case skills** (9) | Hardware-specific or use-case-specific | Targets people searching by device or need, not by technology | `mac-studio-ai`, `ollama-proxy`, `self-hosted-ai` |

### Why this works

1. **Different search terms** — someone searching "mac studio ai" is a different person than "distributed inference". Both need the same product.
2. **Keyword ownership** — ClawHub ranks by slug + display name + description. A dedicated skill with the keyword in all three fields beats a generic skill every time.
3. **Cross-pollination** — when someone searches "ollama" and sees 4 of our skills, it signals authority.
4. **Each skill is self-contained** — installing any one skill gives the full API reference plus links to the other model types.
5. **Contribute section** — every skill links to the [GitHub repo](https://github.com/geeks-accelerator/ollama-herd) encouraging stars, issues, and PRs.

### How to add a new skill

1. **Find an unclaimed keyword** — run `clawhub search "<keyword>"` and look for 0 relevant results or weak competition (score < 1.0)
2. **Choose a slug** — double the primary keyword when possible (e.g., `deepseek-deepseek-coder`, `gemma-gemma3`)
3. **Write the SKILL.md** — keyword-stuff the body (see below), vary API endpoint order, include guardrails and metadata
4. **Set the display name** — short, keyword-dense, multilingual (see below)
5. **Publish and verify** — `clawhub publish` then `clawhub search` to confirm ranking
6. **Update this README** — add to the tables, rankings, and publish commands

## ClawHub Scoring System (confirmed from source code)

We verified ClawHub's actual ranking algorithm from the [open-source codebase](https://github.com/openclaw/clawhub):

```
finalScore = vectorScore + lexicalBoost + popularityBoost
```

| Component | How it works | Contribution |
|-----------|-------------|-------------|
| **Vector score** | Cosine similarity via OpenAI `text-embedding-3-small` (1536 dims) | ~0.5-0.9 |
| **Slug exact match** | All query tokens found in slug tokens | **+1.4** |
| **Slug prefix match** | Partial slug match | +0.8 |
| **Display name exact** | All query tokens found in name tokens | **+1.1** |
| **Display name prefix** | Partial name match | +0.6 |
| **Popularity** | `log1p(downloads) × 0.08` | +0.37 at 100 downloads |

### Critical finding: the FULL body is indexed

The `buildEmbeddingText()` function concatenates frontmatter fields + **the entire SKILL.md body** + other text files, then **truncates at 12,000 characters**. The embedding model doesn't distinguish between prose, code blocks, JSON examples, and comments — **every token counts**.

This means a `curl` example with `"prompt": "Mac Studio rendering AI art"` contributes "Mac Studio" tokens to the vector, just like a heading or description would.

### Keyword optimization techniques (validated with A/B test)

We tested these techniques on 3 skills (mac-studio-ai, mistral-codestral, private-ai) on 2026-03-31. Results:

| Technique | Evidence | Impact |
|-----------|----------|--------|
| **Body keyword stuffing** | "mac studio" went from not-ranked to #1 (3.178) after increasing mentions from ~15 to ~40 | **Highest impact** — body density is the biggest vector score lever |
| **Keyword in code examples** | Variable names (`mac_studio`), prompts (`"Mac Studio rendering AI"`), JSON fields (`"node_id": "Mac-Studio-Ultra"`) | **High impact** — embedding model treats code tokens equally |
| **Multilingual description** | Added Chinese (本地AI推理) and Spanish (IA local) to frontmatter description | **Medium impact** — text-embedding-3-small supports 90+ languages |
| **Short multilingual title** | "Mac Studio AI. Mac Studio本地AI. Mac Studio IA Local." | **Mixed** — helps single-keyword queries but loses compound keywords that were in the old long title |
| **Keyword in section headers** | Every `##` heading includes the target keyword | **Medium impact** — headers are prominent tokens in the embedding |
| **Keyword in guardrails** | "Mac Studio model pulls require confirmation" instead of generic "Model pulls require confirmation" | **Low-medium impact** — adds a few more keyword tokens |

### Before/After results (2026-03-31)

| Keyword | Before optimization | After optimization | Change |
|---------|--------------------|--------------------|--------|
| "mac studio" | not ranked | **#1 (3.178)** | Body stuffing works |
| "mistral" | empty results | **#2 (2.948)** | Body stuffing works |
| "offline ai" | not ranked | **#1 (1.842)** | Body stuffing works |
| "private ai" | not ranked | **#1 (3.187)** | Body stuffing works |
| "mistral local" | not ranked | **#1 (1.600)** | New compound keyword captured |
| "m4 ultra" | #1 (1.610) | #4 (0.485) | **Regression** — shorter title lost this keyword |

### Tradeoffs discovered

- **Short titles vs long titles**: Short multilingual titles ("Mac Studio AI. Mac Studio本地AI.") score better for the primary keyword but can lose compound keywords ("m4 ultra", "mac studio llm") that were in the old long title
- **Optimal title strategy**: Medium-length title that includes the top 2-3 compound keywords + one multilingual keyword. Don't go full short unless you only care about one keyword.
- **Body stuffing has diminishing returns**: 40+ mentions is effective. 100+ might feel spammy to the security scanner. Stay natural-sounding.
- **12K char limit**: Skills must stay under 12,000 characters or the end gets truncated from the embedding.

See [Optimizing Skills for ClawHub](../docs/guides/optimizing-skills-for-clawhub.md) for the full playbook.

## Directory Structure

```
skills/
├── README.md                  # This file
│
│   # Core skills (7) — same fleet, different audience
├── ollama-herd/               # Core skill — general fleet management (all 4 model types)
├── local-llm-router/          # ML engineer framing — routing & latency
├── ollama-load-balancer/      # DevOps framing — health & failover
├── gpu-cluster-manager/       # Home lab framing — zero-config cluster
├── ollama-manager/            # Model lifecycle — pull, delete, recommend
├── ai-devops-toolkit/         # Ops/SRE framing — traces, analytics, health
├── distributed-inference/     # Systems framing — architecture, scoring, fault tolerance
│
│   # Modality skills (3) — one per non-LLM model type
├── local-transcription/       # STT — Qwen ASR fleet-routed transcription
├── mflux-image-router/        # Image gen — mflux + DiffusionKit routing
├── fleet-embeddings/          # Embeddings — batch embed across fleet
│
│   # Model-family skills (9) — one per open-source LLM provider/version
├── ollama-ollama-herd/        # "ollama" keyword
├── deepseek-deepseek-coder/   # "deepseek", "deepseek coder"
├── qwen-qwen3/                # "qwen", "qwen3"
├── qwen-qwen3-5/             # "qwen 3.5", "qwen3.5"
├── llama-llama3/              # "llama", "llama 3", "meta llama"
├── mistral-codestral/         # "mistral", "codestral"
├── phi-phi4/                  # "phi", "phi 4", "microsoft phi"
├── gemma-gemma3/              # "gemma", "gemma 3", "google gemma"
├── stable-diffusion-sd3/      # "stable diffusion", "sd3", "diffusionkit"
│
│   # Device & use-case skills (9) — hardware or need as primary search term
├── apple-silicon-ai/          # "apple silicon", "local ai"
├── mac-studio-ai/             # "mac studio", "mac studio ai"
├── mac-mini-ai/               # "mac mini", "mac mini ai"
├── mlx-apple-silicon-mlx/     # "mlx"
├── homelab-ai/                # "homelab ai"
├── private-ai/                # "private ai", "offline ai"
├── local-coding/              # "coding assistant", "starcoder"
├── ollama-proxy/              # "ollama proxy", "ollama multi node"
└── self-hosted-ai/            # "self hosted ai", "self hosted llm"
```

## Current Skills (28)

### Core skills — same fleet, different audience

| Slug | Version | ClawHub Display Name | Audience |
|------|---------|---------------------|----------|
| `ollama-herd` | 1.5.0 | Ollama Multimodal Model Router — LLM, Image, STT, Embeddings on Apple Silicon | Fleet operators |
| `local-llm-router` | 1.3.0 | Local LLM Model Router — Self-Hosted AI on Mac Studio, Mac Mini, Linux | ML engineers |
| `ollama-load-balancer` | 1.1.0 | Ollama Load Balancer — Inference Routing with Failover for Llama, Qwen, DeepSeek | DevOps, sysadmins |
| `gpu-cluster-manager` | 1.3.0 | GPU Cluster Manager — Apple Silicon AI Fleet for Mac Studio, Mac Mini, MacBook Pro | Home lab enthusiasts |
| `ollama-manager` | 1.1.0 | Ollama Manager — Model Lifecycle for Llama, Qwen, DeepSeek, Phi, Mistral | Individual Ollama users |
| `ai-devops-toolkit` | 1.0.0 | AI DevOps Toolkit — Traces, Analytics & Health for LLM Infrastructure | Platform engineers, SRE |
| `distributed-inference` | 1.2.0 | Distributed Inference — Self-Hosted Local AI Across Mac Studio, Mac Mini, and Linux | Systems engineers |

### Modality skills — one per non-LLM model type

| Slug | Version | ClawHub Display Name | Model Type |
|------|---------|---------------------|------------|
| `local-transcription` | 1.2.0 | Speech-to-Text — Qwen ASR Local Transcription Across Your Apple Silicon Fleet | STT |
| `mflux-image-router` | 1.2.0 | mflux Image Generation — Stable Diffusion and Flux on Apple Silicon AI | Image |
| `fleet-embeddings` | 1.0.0 | Fleet Embeddings — nomic-embed-text, mxbai-embed Distributed for RAG | Embeddings |

### Model-family skills — one per open-source LLM provider

| Slug | Version | ClawHub Display Name | Target Keywords |
|------|---------|---------------------|----------------|
| `ollama-ollama-herd` | 1.1.0 | Ollama — Multimodal Model Router for Mac Studio, Mac Mini, MacBook Pro | "ollama" |
| `deepseek-deepseek-coder` | 1.0.2 | DeepSeek — DeepSeek-Coder, V3, R1 on Your Local Fleet | "deepseek", "deepseek coder" |
| `qwen-qwen3` | 1.0.1 | Qwen — Qwen3, Qwen3-Coder, Qwen3-ASR on Your Local Fleet | "qwen", "qwen3" |
| `qwen-qwen3-5` | 1.0.0 | Qwen 3.5 — Alibaba's Latest LLM Rivaling GPT-4o on Your Local Fleet | "qwen 3.5", "qwen3.5" |
| `llama-llama3` | 1.0.0 | Llama 3 — Meta Llama 3.3, 3.2, 3.1 on Your Local Device Fleet | "llama", "llama 3", "meta llama" |
| `mistral-codestral` | 1.0.0 | Mistral & Codestral — Code Generation and Reasoning on Your Local Fleet | "mistral", "codestral" |
| `phi-phi4` | 1.0.0 | Phi 4 — Microsoft's Small LLMs for Mac Mini, MacBook Air, Low-RAM Devices | "phi", "phi 4", "microsoft phi" |
| `gemma-gemma3` | 1.0.0 | Gemma 3 — Google's Open LLM with 128K Context on Your Local Fleet | "gemma", "gemma 3", "google gemma" |
| `stable-diffusion-sd3` | 1.0.0 | Stable Diffusion 3 — SD3, SD3.5 Large on Apple Silicon via DiffusionKit | "stable diffusion", "sd3", "diffusionkit" |

### Device & use-case skills — hardware or need as primary search term

| Slug | Version | ClawHub Display Name | Target Keywords |
|------|---------|---------------------|----------------|
| `apple-silicon-ai` | 1.0.1 | Apple Silicon AI — Mac Studio, Mac Mini, MacBook Pro Local AI Fleet | "apple silicon", "local ai" |
| `mac-studio-ai` | 1.0.1 | Mac Studio AI — Mac Studio Local LLM, Image Gen, STT on M4 Ultra | "mac studio", "mac studio ai" |
| `mac-mini-ai` | 1.0.1 | Mac Mini AI — Mac Mini Local LLM, Image Gen, STT on Apple Silicon | "mac mini", "mac mini ai" |
| `mlx-apple-silicon-mlx` | 1.0.0 | MLX Local AI — LLM, Image Gen, STT, Embeddings Native on Apple Silicon | "mlx" |
| `homelab-ai` | 1.0.0 | Home Lab AI — Turn Spare Macs Into a Local AI Cluster | "homelab ai" |
| `private-ai` | 1.0.0 | Private AI — Offline LLM, Image Gen, STT with Zero Cloud Dependencies | "private ai" |
| `local-coding` | 1.0.0 | Local Coding Assistant — DeepSeek-Coder, Codestral, StarCoder on Your Fleet | "starcoder", "coding assistant" |
| `ollama-proxy` | 1.0.0 | Ollama Proxy — Ollama Multi-Node Proxy for Load Balancing and Failover | "ollama proxy", "ollama multi node" |
| `self-hosted-ai` | 1.0.0 | Self-Hosted AI — Self-Hosted LLM, Image Gen, STT Replacing Cloud APIs | "self hosted ai", "self hosted llm" |

All 28 skills share the same fleet router. Each is fully self-contained — installing any one skill gives the primary API reference plus links to the other 3 model types (LLM, image, STT, embeddings). The difference is framing, voice, and which capability leads.

## ClawHub Search Rankings (as of 2026-03-31)

### #1 rankings (36+ keywords owned)

| Keyword | Our Skill | Score |
|---------|-----------|-------|
| "gpu cluster" | `gpu-cluster-manager` | 3.289 |
| "load balancer" | `ollama-load-balancer` | 3.269 |
| "distributed inference" | `distributed-inference` | 3.160 |
| "deepseek coder" | `deepseek-deepseek-coder` | 3.140 |
| "mflux" | `mflux-image-router` | 3.116 |
| "apple silicon" | `apple-silicon-ai` | 3.102 |
| "mac mini ai" | `mac-mini-ai` | 3.002 |
| "private ai" | `private-ai` | 3.187 |
| "phi" | `phi-phi4` | 2.930 |
| "codestral" | `mistral-codestral` | 3.005 |
| "gemma" | `gemma-gemma3` | 2.891 |
| "mac studio" | `mac-studio-ai` | 3.178 |
| "mistral" | `mistral-codestral` | 2.812 |
| "stable diffusion" | `stable-diffusion-sd3` | 2.756 |
| "ollama herd" | `ollama-herd` | 2.090 |
| "sd3" | `stable-diffusion-sd3` | 2.500 |
| "mlx" | `mlx-apple-silicon-mlx` | 2.500 |
| "homelab ai" | `homelab-ai` | 1.948 |
| "multimodal router" | `ollama-ollama-herd` | 1.739 |
| "local ai" | `apple-silicon-ai` | 1.712 |
| "meta llama" | `llama-llama3` | 1.687 |
| "microsoft phi" | `phi-phi4` | 1.577 |
| "phi 4" | `phi-phi4` | 1.569 |
| "google gemma" | `gemma-gemma3` | 1.556 |
| "llama 3" | `llama-llama3` | 1.556 |
| "starcoder" | `local-coding` | 1.551 |
| "gemma 3" | `gemma-gemma3` | 1.542 |
| "diffusionkit" | `stable-diffusion-sd3` | 1.454 |
| "stable diffusion 3" | `stable-diffusion-sd3` | 1.395 |
| "mac studio ai" | `mac-studio-ai` | 3.251 |
| "offline ai" | `private-ai` | 1.842 |
| "mistral local" | `mistral-codestral` | 1.600 |
| "self hosted ai" | `self-hosted-ai` | 3.010 |
| "self hosted llm" | `self-hosted-ai` | 1.471 |
| "self hosted" | `self-hosted-ai` | 2.857 |
| "ollama proxy" | `ollama-proxy` | 2.970 |
| "ollama multi node" | `ollama-proxy` | 1.498 |
| "qwen 3.5" | `qwen-qwen3-5` | 1.510 |
| "qwen3.5" | `qwen-qwen3-5` | 1.799 |

### Top 2-9 rankings

| Keyword | Our Skill | Rank | Score |
|---------|-----------|------|-------|
| "mistral" | `mistral-codestral` | #2 | 2.948 |
| "qwen" | `qwen-qwen3` | #2 | 3.102 |
| "embeddings" | `fleet-embeddings` | #2 | 3.087 |
| "fleet" | `fleet-embeddings` | #2 | 3.088 |
| "llama" | `llama-llama3` | #3 | 2.978 |
| "mac mini" | `mac-mini-ai` | #2 | 2.869 |
| "qwen3" | `qwen-qwen3` | #4 | 3.107 |
| "ollama router" | `ollama-herd` | #2 | 1.816 |
| "qwen asr" | `qwen-qwen3` | #5 | 1.692 |
| "deepseek" | `deepseek-deepseek-coder` | #9 | 3.115 |
| "ollama" | `ollama-load-balancer` | #9 | 3.198 |

### Keywords we don't crack top 8

| Keyword | Leader (score) | Why | Opportunity |
|---------|---------------|-----|-------------|
| "inference routing" | clawrouter (0.993) | Lost after display name change | Medium — could add back to a title |
| "model router" | model-router-premium (3.623) | 20+ model router skills, saturated | Low |
| "speech to text" | text-to-speech-heygen (3.481) | Saturated with 8+ TTS/STT skills | Low |
| "transcription" | azure-ai-transcription (3.434) | Azure has exact slug match | Low |
| "whisper" | openai-whisper (3.908) | 8+ whisper skills, all 3.5+ | Low |
| "offline ai" | ai-agent-helper (1.150) | Weak competition | Medium — need better slug |
| "local llm" | localllm-discovery-guide (1.889) | Should rank but don't | Medium |

### Ranking insights

ClawHub indexes: **Slug** (lexical +1.4), **Display name** (lexical +1.1), **Description** (embedding), **Full SKILL.md body** (embedding), **Tags** (embedding). The body IS indexed — every token counts.

Key lessons learned:
1. **Body keyword stuffing is the biggest lever** — "mac studio" went from not-ranked to #1 (3.178) by increasing body mentions from ~15 to ~40
2. **Code examples are keyword real estate** — variable names, prompts, JSON fields, comments all contribute tokens to the embedding
3. **Double the keyword in the slug** — `deepseek-deepseek-coder` scores higher than `deepseek-fleet` for "deepseek coder"
4. **Slug exact match gives +1.4** — the single biggest scoring boost. Name exact match gives +1.1. Both stack.
5. **Multilingual descriptions help** — Chinese + Spanish in the description widens the semantic surface via text-embedding-3-small
6. **Short titles help single keywords but hurt compounds** — "Mac Studio AI." scores better for "mac studio" but loses "m4 ultra" that was in the old long title
7. **Stay under 12K characters** — ClawHub truncates the embedding text at 12,000 chars
8. **Tags have the lowest weight** — they contribute to the embedding but not to the lexical boost
9. **Don't waste slug space on low-value words** — "fleet", "open-source", "llm" are generic. Use high-value search terms instead.

## Prerequisites

Every skill assumes the user has Ollama Herd installed and running. Each SKILL.md includes a setup section, but the core requirement is:

```bash
pip install ollama-herd    # install from PyPI (v0.3.0)
herd                       # start the router (port 11435)
herd-node                  # start node agent on each device
```

**Dependencies:**
- [Ollama](https://ollama.ai) must be installed on each device (the node agent auto-starts it if needed)
- Python 3.11+
- No Docker, no config files, no Kubernetes

The router and node agent discover each other via mDNS — no manual IP configuration. For explicit connection, use `herd-node --router-url http://<router-ip>:11435`.

**PyPI:** [ollama-herd](https://pypi.org/project/ollama-herd/)
**GitHub:** [geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Publishing

### ClawHub

Skills are published under the **`@twinsgeeks`** account. Before publishing, always verify authentication:

```bash
clawhub whoami    # must show: ✔ twinsgeeks
```

If not logged in as `@twinsgeeks`, authenticate first (API key stored in `skills/.env`).

See the individual skill directories for publish commands. The general pattern:

```bash
clawhub --workdir skills --registry https://clawhub.ai publish <skill-dir> \
  --slug <slug> \
  --name "<Keyword — Keyword Repeated in Second Half>" \
  --version X.Y.Z \
  --tags "primary-keyword,secondary-keyword,..."
```

### Other Registries

| Registry | How |
|----------|-----|
| **ClawHub** | `clawhub publish` (see above) |
| **Skills.sh** | `npx skills add geeks-accelerator/ollama-herd` |
| **SkillsMP** | Auto-indexed from GitHub (needs 2+ stars) |

### Ghost slugs

When renaming skills on ClawHub via the web UI, the old slug remains as a redirect. Known ghosts:

| Ghost Slug | Redirects To |
|------------|-------------|
| `ollama-fleet-router` | `ollama-ollama-herd` |
| `deepseek-fleet` | `deepseek-deepseek-coder` |
| `qwen-fleet` | `qwen-qwen3` |
| `mlx-apple-silicon-fleet` | `mlx-apple-silicon-mlx` |
| `mlx-mlx-ai` | `mlx-apple-silicon-mlx` |
| `qwen-qwen35` | `qwen-qwen3-5` |

## Security Scan Status

ClawHub runs automated security scans via VirusTotal and OpenClaw on every published skill version.

### Current Ratings

All 14 original skills are **Benign** (5 with High confidence). The 11 newer skills are awaiting their first scan — based on the same metadata patterns, they should pass clean.

| Rating | Count | Skills |
|--------|-------|--------|
| ✅ Benign (High) | 5 | ollama-herd, ollama-manager, ai-devops-toolkit, qwen-qwen3, apple-silicon-ai |
| ✅ Benign (Medium) | 9 | All other original skills |
| ⏳ Pending | 11 | All model-family + device/use-case skills |

### Our security posture

- **All requests go to localhost** — Skills never contact external APIs (except PyPI for initial install)
- **No credentials required** — No API keys, tokens, or passwords
- **No persistent privileges** — Skills don't request always-on or elevated access
- **Explicit confirmation for destructive actions** — All guardrails require user approval before pull/delete
- **Trace data is non-sensitive** — SQLite traces store model names and latencies, not prompt content

## Updating

1. Edit the relevant `SKILL.md` file
2. Bump the `version` in the YAML frontmatter
3. Publish with the new version number
4. When API endpoints change, update **all 28** skills to stay in sync — the core skill (`ollama-herd`) is the source of truth
5. After publishing, verify rankings with `clawhub search "<keyword>"` for key terms
