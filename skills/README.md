# Ollama Herd Skills

This directory contains 13 skills that let AI agents discover and manage Ollama Herd fleets. Multiple skills target different audiences and keywords with the same underlying API — see [Skill Publishing Strategy](../docs/skill-publishing-strategy.md) for the rationale and [Optimizing Skills for ClawHub](../docs/guides/optimizing-skills-for-clawhub.md) for the search ranking playbook.

## Directory Structure

```
skills/
├── README.md                  # This file
│
│   # Core skills (7) — same fleet, different audience
├── ollama-herd/               # Core skill — general fleet management (all 4 model types)
│   └── SKILL.md
├── local-llm-router/          # ML engineer framing — routing & latency
│   └── SKILL.md
├── ollama-load-balancer/      # DevOps framing — health & failover
│   └── SKILL.md
├── gpu-cluster-manager/       # Home lab framing — zero-config cluster
│   └── SKILL.md
├── ollama-manager/            # Model lifecycle — pull, delete, recommend
│   └── SKILL.md
├── ai-devops-toolkit/         # Ops/SRE framing — traces, analytics, health
│   └── SKILL.md
├── distributed-inference/     # Systems framing — architecture, scoring, fault tolerance
│   └── SKILL.md
│
│   # Modality skills (3) — one per non-LLM model type
├── local-transcription/       # STT — Qwen ASR fleet-routed transcription
│   └── SKILL.md
├── mflux-image-router/        # Image gen — Apple Silicon mflux routing
│   └── SKILL.md
├── fleet-embeddings/          # Embeddings — batch embed across fleet
│   └── SKILL.md
│
│   # Keyword-targeted skills (4) — specific search term as primary target
├── ollama-ollama-herd/        # Targets "ollama" keyword — all 4 model types
│   └── SKILL.md
├── deepseek-deepseek-coder/   # Targets "deepseek" keyword — DeepSeek model family
│   └── SKILL.md
├── qwen-qwen3/                # Targets "qwen" keyword — Qwen model family
│   └── SKILL.md
└── apple-silicon-ai/          # Targets "apple silicon", "mac studio", "mac mini" keywords
    └── SKILL.md
```

## Current Skills (13)

### Core skills — same fleet, different audience

| Slug | Version | ClawHub Display Name | Audience |
|------|---------|---------------------|----------|
| `ollama-herd` | 1.2.0 | Ollama Fleet Router — Multimodal AI Routing for Llama, Qwen, DeepSeek, mflux, Qwen ASR | Fleet operators |
| `local-llm-router` | 1.1.0 | Local LLM Router — Inference Routing for Llama, Qwen, DeepSeek, Phi, Mistral | ML engineers |
| `ollama-load-balancer` | 1.1.0 | Ollama Load Balancer — Inference Routing with Failover for Llama, Qwen, DeepSeek | DevOps, sysadmins |
| `gpu-cluster-manager` | 1.1.0 | GPU Cluster Manager — Apple Silicon AI Fleet for Llama, Qwen, DeepSeek | Home lab enthusiasts |
| `ollama-manager` | 1.1.0 | Ollama Manager — Model Lifecycle for Llama, Qwen, DeepSeek, Phi, Mistral | Individual Ollama users |
| `ai-devops-toolkit` | 1.0.0 | AI DevOps Toolkit — Traces, Analytics & Health for LLM Infrastructure | Platform engineers, SRE |
| `distributed-inference` | 1.0.2 | Distributed Inference — Llama, Qwen, DeepSeek Across Heterogeneous Hardware | Systems engineers |

### Modality skills — one per non-LLM model type

| Slug | Version | ClawHub Display Name | Model Type |
|------|---------|---------------------|------------|
| `local-transcription` | 1.0.0 | Qwen ASR Transcription — Local Speech-to-Text Routed Across Your Fleet | STT |
| `mflux-image-router` | 1.0.0 | mflux Image Router — Apple Silicon Flux Image Generation Across Your Fleet | Image |
| `fleet-embeddings` | 1.0.0 | Fleet Embeddings — nomic-embed-text, mxbai-embed Distributed for RAG | Embeddings |

### Keyword-targeted skills — specific search term as primary target

| Slug | Version | ClawHub Display Name | Target Keyword |
|------|---------|---------------------|----------------|
| `ollama-ollama-herd` | 1.0.0 | Ollama — Herd Your Ollama LLMs Into One Smart Endpoint | "ollama" |
| `deepseek-deepseek-coder` | 1.0.1 | DeepSeek — DeepSeek-Coder, V3, R1 on Your Local Fleet | "deepseek" |
| `qwen-qwen3` | 1.0.1 | Qwen — Qwen3, Qwen3-Coder, Qwen3-ASR on Your Local Fleet | "qwen" |
| `apple-silicon-ai` | 1.0.0 | Apple Silicon AI — Mac Studio, Mac Mini, MacBook Pro Local AI Fleet | "apple silicon", "mac studio", "mac mini" |

All fourteen skills share the same fleet router. Each is fully self-contained — installing any one skill gives the primary API reference plus links to the other 3 model types (LLM, image, STT, embeddings). The difference is framing, voice, and which capability leads.

## ClawHub Search Rankings (as of 2026-03-30)

### #1 rankings (we own these keywords)

| Keyword | Our Skill | Score |
|---------|-----------|-------|
| "load balancer" | `ollama-load-balancer` | 3.251 |
| "gpu cluster" | `gpu-cluster-manager` | 3.266 |
| "inference routing" | `local-llm-router` | 1.711 |
| "distributed inference" | `distributed-inference` | 3.132 |
| "ollama herd" | `ollama-ollama-herd` | 2.875 |
| "deepseek coder" | `deepseek-deepseek-coder` | 2.894 |

### Top 5 rankings

| Keyword | Our Skill | Rank | Score |
|---------|-----------|------|-------|
| "mflux" | `mflux-image-router` | #2 | 2.869 |
| "qwen" | `qwen-qwen3` | #3 | 2.862 |
| "embeddings" | `fleet-embeddings` | #3 | 2.833 |
| "qwen3" | `qwen-qwen3` | #4 | 2.867 |
| "qwen asr" | `qwen-qwen3` | #5 | 1.452 |
| "llama" | `ollama-load-balancer` | #4 | 1.696 |
| "phi" | `ollama-manager` | #1 (niche) | 1.415 |
| "mistral" | `ollama-manager` | #1 (niche) | 1.415 |
| "fleet" | `fleet-embeddings` | #3 | 2.833 |

### Keywords we don't crack top 8

| Keyword | Leader (score) | Why we don't rank | Opportunity |
|---------|---------------|-------------------|-------------|
| "ollama" | ollama-local (3.556) | 20+ skills with "ollama" in slug — saturated | Low — would need exact slug match |
| "deepseek" | deepseek-reasoner-lite (3.492) | 8+ skills with "deepseek" slug prefix | Low — saturated |
| "speech to text" | text-to-speech-heygen (3.480) | Our skill is named "transcription" not "speech-to-text" | Medium — rename or add skill |
| "transcription" | azure-ai-transcription (3.434) | Azure skill has exact slug match | Low — saturated |
| "image generation" | best-image-generation (3.680) | Cloud-focused competitors dominate | Low — different category |
| "whisper" | openai-whisper (3.908) | 8+ whisper-specific skills, all 3.5+ | Low — saturated |
| "apple silicon ai" | No clear leader | Nobody ranks — but neither do we | High — unclaimed |
| "multimodal router" | model-router-premium (1.075) | We're the only multimodal router but don't rank | High — needs dedicated skill |

### Ranking insights

Three fields are indexed on ClawHub: **Display name** (highest weight), **Description** (medium), **Tags** (lower). The body content of SKILL.md is NOT indexed for search.

Key lessons learned:
1. **Lead with the keyword** — "Ollama Load Balancer" ranks for "load balancer" because the keyword is in the display name
2. **Double the keyword in the slug** — `deepseek-deepseek-coder` scores higher than `deepseek-fleet` for "deepseek coder"
3. **Model names in descriptions** — Adding "Llama, Qwen, DeepSeek" to descriptions moved us into top 5 for those terms
4. **Tags alone don't rank** — A tag for "llama" only helps if "llama" also appears in the display name or description
5. **Exact slug match wins** — Skills with the exact keyword as their slug (e.g., `ollama-local`) always outscore compound slugs

See [Optimizing Skills for ClawHub](../docs/guides/optimizing-skills-for-clawhub.md) for the full playbook.

## Prerequisites

Every skill assumes the user has Ollama Herd installed and running. Each SKILL.md includes a setup section, but the core requirement is:

```bash
pip install ollama-herd    # install from PyPI
herd                       # start the router (port 11435)
herd-node                  # start node agent on each device
```

**Dependencies:**
- [Ollama](https://ollama.ai) must be installed on each device (the node agent auto-starts it if needed)
- Python 3.10+
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

#### Core skills

```bash
clawhub --workdir skills --registry https://clawhub.ai publish ollama-herd \
  --slug ollama-herd \
  --name "Ollama Fleet Router — Multimodal AI Routing for Llama, Qwen, DeepSeek, mflux, Qwen ASR" \
  --version 1.2.0 \
  --tags "ollama,fleet-management,inference-routing,model-management,monitoring,dashboard,local-llm,self-hosted,llama,qwen,deepseek,phi,mistral,multimodal,speech-to-text,embeddings,image-generation"

clawhub --workdir skills --registry https://clawhub.ai publish local-llm-router \
  --slug local-llm-router \
  --name "Local LLM Router — Inference Routing for Llama, Qwen, DeepSeek, Phi, Mistral" \
  --version 1.1.0 \
  --tags "local-llm,inference-routing,model-router,llm-routing,ollama,load-balancing,latency,scoring,multi-node,llama,qwen,deepseek,phi,mistral,gemma,codestral"

clawhub --workdir skills --registry https://clawhub.ai publish ollama-load-balancer \
  --slug ollama-load-balancer \
  --name "Ollama Load Balancer — Inference Routing with Failover for Llama, Qwen, DeepSeek" \
  --version 1.1.0 \
  --tags "load-balancer,ollama,health-check,auto-discovery,high-availability,failover,monitoring,self-hosted,distributed-inference,llama,qwen,deepseek"

clawhub --workdir skills --registry https://clawhub.ai publish gpu-cluster-manager \
  --slug gpu-cluster-manager \
  --name "GPU Cluster Manager — Apple Silicon AI Fleet for Llama, Qwen, DeepSeek" \
  --version 1.1.0 \
  --tags "gpu-cluster,apple-silicon,homelab,local-ai,self-hosted,zero-config,gpu-inference,home-lab,cost-savings,llama,qwen,deepseek,phi"

clawhub --workdir skills --registry https://clawhub.ai publish ollama-manager \
  --slug ollama-manager \
  --name "Ollama Manager — Model Lifecycle for Llama, Qwen, DeepSeek, Phi, Mistral" \
  --version 1.1.0 \
  --tags "ollama,model-management,model-lifecycle,pull,delete,disk-usage,recommendations,cleanup,llama,qwen,deepseek,phi,mistral"

clawhub --workdir skills --registry https://clawhub.ai publish ai-devops-toolkit \
  --slug ai-devops-toolkit \
  --name "AI DevOps Toolkit — Traces, Analytics & Health for LLM Infrastructure" \
  --version 1.0.0 \
  --tags "devops,ai-infrastructure,observability,traces,metrics,sre,operations,analytics,monitoring"

clawhub --workdir skills --registry https://clawhub.ai publish distributed-inference \
  --slug distributed-inference \
  --name "Distributed Inference — Llama, Qwen, DeepSeek Across Heterogeneous Hardware" \
  --version 1.0.2 \
  --tags "distributed-inference,scheduling,fault-tolerance,multi-node,heterogeneous,scoring,coordination,llm-infrastructure,llama,qwen,deepseek"
```

#### Modality skills

```bash
clawhub --workdir skills --registry https://clawhub.ai publish local-transcription \
  --slug local-transcription \
  --name "Qwen ASR Transcription — Local Speech-to-Text Routed Across Your Fleet" \
  --version 1.0.0 \
  --tags "transcription,speech-to-text,whisper,qwen-asr,local-stt,audio-transcription,fleet-routing,apple-silicon,offline-transcription,mlx"

clawhub --workdir skills --registry https://clawhub.ai publish mflux-image-router \
  --slug mflux-image-router \
  --name "mflux Image Router — Apple Silicon Flux Image Generation Across Your Fleet" \
  --version 1.0.0 \
  --tags "mflux,flux,image-generation,apple-silicon,mlx,local-image,stable-diffusion,z-image-turbo,gpu-cluster"

clawhub --workdir skills --registry https://clawhub.ai publish fleet-embeddings \
  --slug fleet-embeddings \
  --name "Fleet Embeddings — nomic-embed-text, mxbai-embed Distributed for RAG" \
  --version 1.0.0 \
  --tags "embeddings,ollama-embeddings,vector-search,rag,semantic-search,fleet-routing,batch-embeddings,distributed-inference,nomic-embed-text,mxbai-embed,snowflake-arctic-embed"
```

#### Keyword-targeted skills

```bash
clawhub --workdir skills --registry https://clawhub.ai publish ollama-ollama-herd \
  --slug ollama-ollama-herd \
  --name "Ollama — Herd Your Ollama LLMs Into One Smart Endpoint" \
  --version 1.0.0 \
  --tags "ollama,ollama-herd,llm-router,fleet-management,llama,qwen,deepseek,phi,mistral,image-generation,speech-to-text,embeddings,apple-silicon,local-ai"

clawhub --workdir skills --registry https://clawhub.ai publish deepseek-deepseek-coder \
  --slug deepseek-deepseek-coder \
  --name "DeepSeek — DeepSeek-Coder, V3, R1 on Your Local Fleet" \
  --version 1.0.1 \
  --tags "deepseek,deepseek-coder,deepseek-v3,deepseek-r1,local-llm,ollama,fleet-routing,apple-silicon,coding,reasoning"

clawhub --workdir skills --registry https://clawhub.ai publish qwen-qwen3 \
  --slug qwen-qwen3 \
  --name "Qwen — Qwen3, Qwen3-Coder, Qwen3-ASR on Your Local Fleet" \
  --version 1.0.1 \
  --tags "qwen,qwen3,qwen3-coder,qwen-asr,qwen2.5,local-llm,ollama,fleet-routing,apple-silicon,speech-to-text,coding"

clawhub --workdir skills --registry https://clawhub.ai publish apple-silicon-ai \
  --slug apple-silicon-ai \
  --name "Apple Silicon AI — Mac Studio, Mac Mini, MacBook Pro Local AI Fleet" \
  --version 1.0.0 \
  --tags "apple-silicon,mac-studio,mac-mini,macbook-pro,mac-pro,m4-max,m4-ultra,m3-max,m2-ultra,local-ai,self-hosted,ollama,llm,image-generation,speech-to-text,embeddings"
```

### Other Registries

| Registry | How |
|----------|-----|
| **ClawHub** | `clawhub publish` (see above) |
| **Skills.sh** | `npx skills add geeks-accelerator/ollama-herd` |
| **SkillsMP** | Auto-indexed from GitHub (needs 2+ stars) |

### Ghost slugs

When renaming skills on ClawHub via the web UI, the old slug remains as a redirect. These ghost slugs still appear in search results with lower scores. Known ghosts:

| Ghost Slug | Redirects To | Notes |
|------------|-------------|-------|
| `ollama-fleet-router` | `ollama-ollama-herd` | Renamed 2026-03-30 |
| `deepseek-fleet` | `deepseek-deepseek-coder` | Renamed 2026-03-30 |
| `qwen-fleet` | `qwen-qwen3` | Renamed 2026-03-30 |

## Security Scan Status

ClawHub runs automated security scans via VirusTotal and OpenClaw on every published skill version. Here's the current status and our rationale for each finding.

### Current Ratings

| Skill | VirusTotal | OpenClaw | Confidence | Notes |
|-------|-----------|----------|------------|-------|
| `ollama-herd` | ✅ Benign | ✅ Benign | Medium | Clean — all requirements declared |
| `ollama-manager` | ✅ Benign | ✅ Benign | High | Highest confidence across all skills |
| `gpu-cluster-manager` | ✅ Benign | ✅ Benign | Medium | Fixed in v1.0.1 |
| `ai-devops-toolkit` | ✅ Benign | ✅ Benign | Medium | Fixed in v1.0.1 |
| `local-llm-router` | ✅ Benign | ⚠️ Suspicious | Medium | Awaiting rescan after v1.0.2 metadata fix |
| `ollama-load-balancer` | ✅ Benign | ⚠️ Suspicious | Medium | Auto-pull side effects (legitimate, guarded) |
| `distributed-inference` | ✅ Benign | ⚠️ Suspicious | Medium | Awaiting rescan after v1.0.2 privacy fix |
| `local-transcription` | — | — | — | Not yet scanned (v1.0.0) |
| `mflux-image-router` | — | — | — | Not yet scanned (v1.0.0) |
| `fleet-embeddings` | — | — | — | Not yet scanned (v1.0.0) |
| `ollama-ollama-herd` | — | — | — | Not yet scanned (v1.0.0) |
| `deepseek-deepseek-coder` | — | — | — | Not yet scanned (v1.0.1) |
| `qwen-qwen3` | — | — | — | Not yet scanned (v1.0.1) |

### What we fixed

**v1.0.0 → v1.0.1:** All skills only declared `curl`/`wget`/`sqlite3` as required binaries but instructions also used `python3`, `pip`, and `~/.fleet-manager/` files. Added `optionalBins` and `configPaths` to metadata. Resolved 2 of 5 Suspicious ratings.

**v1.0.1 → v1.0.2:** Three targeted fixes for the remaining Suspicious skills:
- **`local-llm-router`**: Moved `configPaths` from inside `requires` to top-level `openclaw` metadata (scanner couldn't find it nested under `requires`)
- **`ollama-load-balancer`**: Clarified auto-pull is opt-in ("disabled by default, toggle via settings API"), not automatic
- **`distributed-inference`**: Removed meeting detection and app fingerprinting references from the SKILL.md — these are node agent features that don't belong in a skill about distributed inference coordination. The skill now focuses on scheduling, scoring, and fault tolerance.

### How the scanner works

OpenClaw's security scanner evaluates 5 dimensions:

1. **Purpose & Capability** — Does the skill name/description match what the code actually does?
2. **Instruction Scope** — Does the skill request access proportional to its stated purpose?
3. **Install Mechanism** — Is the install method transparent and verifiable?
4. **Credentials** — Does it request secrets or tokens?
5. **Persistence & Privilege** — Does it run persistently or request elevated access?

The "pip install without checksums" concern appears on ALL skills as info-level. This is inherent to the PyPI distribution model — the scanner can't verify the package contents at registry time. Users should review the [source code](https://github.com/geeks-accelerator/ollama-herd) before installing.

### Our security posture

- **All requests go to localhost** — Skills never contact external APIs (except PyPI for initial install)
- **No credentials required** — No API keys, tokens, or passwords
- **No persistent privileges** — Skills don't request always-on or elevated access
- **Explicit confirmation for destructive actions** — All SKILL.md guardrails require user approval before pull/delete operations
- **Trace data is non-sensitive** — SQLite traces store model names and latencies, not prompt content

## Updating

1. Edit the relevant `SKILL.md` file
2. Bump the `version` in the YAML frontmatter
3. Publish with the new version number using the commands above
4. When API endpoints change, update **all thirteen** skills to stay in sync — the core skill (`ollama-herd`) is the source of truth
5. After publishing, verify rankings with `clawhub search "<keyword>"` for key terms
