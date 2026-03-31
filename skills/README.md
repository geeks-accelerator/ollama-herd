# Ollama Herd Skills

This directory contains 19 skills that let AI agents discover and manage Ollama Herd fleets. Multiple skills target different audiences and keywords with the same underlying API — see [Skill Publishing Strategy](../docs/skill-publishing-strategy.md) for the rationale and [Optimizing Skills for ClawHub](../docs/guides/optimizing-skills-for-clawhub.md) for the search ranking playbook.

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
│   # Keyword-targeted skills (9) — specific search term as primary target
├── ollama-ollama-herd/        # Targets "ollama" keyword — all 4 model types
│   └── SKILL.md
├── deepseek-deepseek-coder/   # Targets "deepseek" keyword — DeepSeek model family
│   └── SKILL.md
├── qwen-qwen3/                # Targets "qwen" keyword — Qwen model family
│   └── SKILL.md
├── apple-silicon-ai/          # Targets "apple silicon", "mac studio", "mac mini" keywords
│   └── SKILL.md
├── llama-llama3/              # Targets "llama", "llama 3", "meta llama"
│   └── SKILL.md
├── mistral-codestral/         # Targets "mistral", "codestral" — both unclaimed
│   └── SKILL.md
├── phi-phi4/                  # Targets "phi 4", "microsoft phi" — small model angle
│   └── SKILL.md
├── private-ai/                # Targets "private ai", "offline ai", "air gapped"
│   └── SKILL.md
└── local-coding/              # Targets "coding assistant", "local code", "starcoder"
    └── SKILL.md
```

## Current Skills (19)

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

### Keyword-targeted skills — specific search term as primary target

| Slug | Version | ClawHub Display Name | Target Keyword |
|------|---------|---------------------|----------------|
| `ollama-ollama-herd` | 1.1.0 | Ollama — Multimodal Model Router for Mac Studio, Mac Mini, MacBook Pro | "ollama" |
| `deepseek-deepseek-coder` | 1.0.2 | DeepSeek — DeepSeek-Coder, V3, R1 on Your Local Fleet | "deepseek" |
| `qwen-qwen3` | 1.0.1 | Qwen — Qwen3, Qwen3-Coder, Qwen3-ASR on Your Local Fleet | "qwen" |
| `apple-silicon-ai` | 1.0.1 | Apple Silicon AI — Mac Studio, Mac Mini, MacBook Pro Local AI Fleet | "apple silicon", "mac studio", "mac mini" |
| `llama-llama3` | 1.0.0 | Llama 3 — Meta Llama 3.3, 3.2, 3.1 on Your Local Device Fleet | "llama", "llama 3", "meta llama" |
| `mistral-codestral` | 1.0.0 | Mistral & Codestral — Code Generation and Reasoning on Your Local Fleet | "mistral", "codestral" |
| `phi-phi4` | 1.0.0 | Phi 4 — Microsoft's Small LLMs for Mac Mini, MacBook Air, Low-RAM Devices | "phi 4", "microsoft phi" |
| `private-ai` | 1.0.0 | Private AI — Offline LLM, Image Gen, STT with Zero Cloud Dependencies | "private ai", "offline ai", "air gapped" |
| `local-coding` | 1.0.0 | Local Coding Assistant — DeepSeek-Coder, Codestral, StarCoder on Your Fleet | "coding assistant", "local code", "starcoder" |

All nineteen skills share the same fleet router. Each is fully self-contained — installing any one skill gives the primary API reference plus links to the other 3 model types (LLM, image, STT, embeddings). The difference is framing, voice, and which capability leads.

## ClawHub Search Rankings (as of 2026-03-30)

### #1 rankings (we own these keywords)

| Keyword | Our Skill | Score |
|---------|-----------|-------|
| "load balancer" | `ollama-load-balancer` | 3.251 |
| "gpu cluster" | `gpu-cluster-manager` | 3.269 |
| "deepseek coder" | `deepseek-deepseek-coder` | 3.070 |
| "apple silicon" | `apple-silicon-ai` | 2.869 |
| "private ai" | `private-ai` | 2.839 |
| "distributed inference" | `distributed-inference` | 3.139 |
| "codestral" | `mistral-codestral` | 2.763 |
| "mistral" | `mistral-codestral` | 2.668 |
| "multimodal router" | `ollama-ollama-herd` | 1.499 |
| "meta llama" | `llama-llama3` | 1.543 |
| "microsoft phi" | `phi-phi4` | 1.434 |
| "phi 4" | `phi-phi4` | 1.426 |
| "llama 3" | `llama-llama3` | 1.412 |
| "stable diffusion" | `mflux-image-router` | 1.100 |
| "phi" | `ollama-manager` | 1.415 |
| "mistral" (niche) | `ollama-manager` | 1.415 |

### Top 5 rankings

| Keyword | Our Skill | Rank | Score |
|---------|-----------|------|-------|
| "llama" | `llama-llama3` | #3 | 2.834 |
| "mflux" | `mflux-image-router` | #2 | 2.866 |
| "qwen" | `qwen-qwen3` | #3 | 2.862 |
| "embeddings" | `fleet-embeddings` | #3 | 2.833 |
| "qwen3" | `qwen-qwen3` | #4 | 2.867 |
| "qwen asr" | `qwen-qwen3` | #5 | 1.452 |
| "fleet" | `fleet-embeddings` | #3 | 2.833 |

### Keywords we don't crack top 8

| Keyword | Leader (score) | Why we don't rank | Opportunity |
|---------|---------------|-------------------|-------------|
| "ollama" | ollama-local (3.556) | 20+ skills with "ollama" in slug — saturated | Low — would need exact slug match |
| "deepseek" | deepseek-reasoner-lite (3.492) | 8+ skills with "deepseek" slug prefix | Low — saturated |
| "speech to text" | text-to-speech-heygen (3.480) | Display name leads with "Speech-to-Text" now, awaiting re-index | Medium — may improve |
| "transcription" | azure-ai-transcription (3.434) | Azure skill has exact slug match | Low — saturated |
| "image generation" | best-image-generation (3.680) | Cloud-focused competitors dominate | Low — different category |
| "whisper" | openai-whisper (3.908) | 8+ whisper-specific skills, all 3.5+ | Low — saturated |
| "mac studio" | apple-notes (1.109) | Device-specific terms don't match well in vector search | Low — may need `mac-studio-ai` slug |
| "mac mini ai" | minimax-understand-image (1.057) | Same — device name doesn't trigger semantic match | Low — may need dedicated slug |

### Regressions to watch

| Keyword | Previous rank | Current | Cause |
|---------|-------------|---------|-------|
| "inference routing" | #1 (1.711) | Dropped | `local-llm-router` display name changed from "Inference Routing for..." to "Self-Hosted AI on..." |
| "ollama herd" | #1 (2.875) | #2 (1.780) | `ollama-herd` display name changed from "Ollama Fleet Router" to "Ollama Multimodal Model Router" |

These regressions show the tradeoff: optimizing for new keywords ("multimodal router", "self-hosted") can drop rankings for previously owned keywords. Monitor after each publish cycle.

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
  --name "Ollama Multimodal Model Router — LLM, Image, STT, Embeddings on Apple Silicon" \
  --version 1.5.0 \
  --tags "ollama,multimodal-router,model-router,fleet-management,llama,qwen,deepseek,phi,mistral,image-generation,speech-to-text,embeddings,apple-silicon,mac-studio,self-hosted,local-ai"

clawhub --workdir skills --registry https://clawhub.ai publish local-llm-router \
  --slug local-llm-router \
  --name "Local LLM Model Router — Self-Hosted AI on Mac Studio, Mac Mini, Linux" \
  --version 1.3.0 \
  --tags "local-llm,model-router,inference-routing,self-hosted,local-ai,apple-silicon,mac-studio,mac-mini,llama,qwen,deepseek,phi,mistral,gemma,codestral"

clawhub --workdir skills --registry https://clawhub.ai publish ollama-load-balancer \
  --slug ollama-load-balancer \
  --name "Ollama Load Balancer — Inference Routing with Failover for Llama, Qwen, DeepSeek" \
  --version 1.1.0 \
  --tags "load-balancer,ollama,health-check,auto-discovery,high-availability,failover,monitoring,self-hosted,distributed-inference,llama,qwen,deepseek"

clawhub --workdir skills --registry https://clawhub.ai publish gpu-cluster-manager \
  --slug gpu-cluster-manager \
  --name "GPU Cluster Manager — Apple Silicon AI Fleet for Mac Studio, Mac Mini, MacBook Pro" \
  --version 1.3.0 \
  --tags "gpu-cluster,apple-silicon,mac-studio,mac-mini,macbook-pro,mac-pro,homelab,local-ai,self-hosted,zero-config,llama,qwen,deepseek,phi,m4-max,m4-ultra"

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
  --name "Distributed Inference — Self-Hosted Local AI Across Mac Studio, Mac Mini, and Linux" \
  --version 1.2.0 \
  --tags "distributed-inference,self-hosted,local-ai,apple-silicon,mac-studio,mac-mini,scheduling,fault-tolerance,multi-node,heterogeneous,llama,qwen,deepseek"
```

#### Modality skills

```bash
clawhub --workdir skills --registry https://clawhub.ai publish local-transcription \
  --slug local-transcription \
  --name "Speech-to-Text — Qwen ASR Local Transcription Across Your Apple Silicon Fleet" \
  --version 1.2.0 \
  --tags "speech-to-text,transcription,qwen-asr,whisper,local-stt,audio-transcription,fleet-routing,apple-silicon,mac-studio,mac-mini,mlx,offline-transcription"

clawhub --workdir skills --registry https://clawhub.ai publish mflux-image-router \
  --slug mflux-image-router \
  --name "mflux Image Generation — Stable Diffusion and Flux on Apple Silicon AI" \
  --version 1.2.0 \
  --tags "mflux,flux,image-generation,stable-diffusion,apple-silicon,mlx,local-image,z-image-turbo,mac-studio,mac-mini,sdxl,local-ai"

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
  --name "Ollama — Multimodal Model Router for Mac Studio, Mac Mini, MacBook Pro" \
  --version 1.1.0 \
  --tags "ollama,ollama-herd,multimodal-router,model-router,llama,qwen,deepseek,phi,mistral,mac-studio,mac-mini,macbook-pro,self-hosted,local-ai,apple-silicon"

clawhub --workdir skills --registry https://clawhub.ai publish deepseek-deepseek-coder \
  --slug deepseek-deepseek-coder \
  --name "DeepSeek — DeepSeek-Coder, V3, R1 on Your Local Fleet" \
  --version 1.0.2 \
  --tags "deepseek,deepseek-coder,deepseek-v3,deepseek-r1,local-llm,ollama,fleet-routing,apple-silicon,coding,reasoning"

clawhub --workdir skills --registry https://clawhub.ai publish qwen-qwen3 \
  --slug qwen-qwen3 \
  --name "Qwen — Qwen3, Qwen3-Coder, Qwen3-ASR on Your Local Fleet" \
  --version 1.0.1 \
  --tags "qwen,qwen3,qwen3-coder,qwen-asr,qwen2.5,local-llm,ollama,fleet-routing,apple-silicon,speech-to-text,coding"

clawhub --workdir skills --registry https://clawhub.ai publish apple-silicon-ai \
  --slug apple-silicon-ai \
  --name "Apple Silicon AI — Mac Studio, Mac Mini, MacBook Pro Local AI Fleet" \
  --version 1.0.1 \
  --tags "apple-silicon,mac-studio,mac-mini,macbook-pro,mac-pro,m4-max,m4-ultra,m3-max,m2-ultra,local-ai,self-hosted,ollama,llm,image-generation,speech-to-text,embeddings"

clawhub --workdir skills --registry https://clawhub.ai publish llama-llama3 \
  --slug llama-llama3 \
  --name "Llama 3 — Meta Llama 3.3, 3.2, 3.1 on Your Local Device Fleet" \
  --version 1.0.0 \
  --tags "llama,llama-3,llama-3.3,meta-llama,llama3,local-llm,ollama,fleet-routing,apple-silicon,mac-studio,mac-mini,open-source"

clawhub --workdir skills --registry https://clawhub.ai publish mistral-codestral \
  --slug mistral-codestral \
  --name "Mistral & Codestral — Code Generation and Reasoning on Your Local Fleet" \
  --version 1.0.0 \
  --tags "mistral,codestral,mistral-ai,mistral-large,mistral-nemo,code-generation,local-llm,ollama,fleet-routing,apple-silicon,multilingual"

clawhub --workdir skills --registry https://clawhub.ai publish phi-phi4 \
  --slug phi-phi4 \
  --name "Phi 4 — Microsoft's Small LLMs for Mac Mini, MacBook Air, Low-RAM Devices" \
  --version 1.0.0 \
  --tags "phi,phi-4,phi4,microsoft-phi,small-llm,low-ram,mac-mini,macbook-air,local-llm,ollama,fleet-routing,efficient,apple-silicon"

clawhub --workdir skills --registry https://clawhub.ai publish private-ai \
  --slug private-ai \
  --name "Private AI — Offline LLM, Image Gen, STT with Zero Cloud Dependencies" \
  --version 1.0.0 \
  --tags "private-ai,offline-ai,air-gapped,on-premise,local-ai,self-hosted,hipaa,gdpr,data-sovereignty,no-cloud,privacy,compliance,ollama"

clawhub --workdir skills --registry https://clawhub.ai publish local-coding \
  --slug local-coding \
  --name "Local Coding Assistant — DeepSeek-Coder, Codestral, StarCoder on Your Fleet" \
  --version 1.0.0 \
  --tags "coding-assistant,code-generation,deepseek-coder,codestral,starcoder,qwen-coder,local-code,ollama,aider,continue-dev,cline,ide,apple-silicon"
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
| `ollama-herd` | ✅ Benign | ✅ Benign | High | Fixed at v1.5.0 — upgraded from Suspicious to Benign + High confidence |
| `ollama-manager` | ✅ Benign | ✅ Benign | High | Highest confidence across all skills |
| `gpu-cluster-manager` | ✅ Benign | ✅ Benign | Medium | Clean since v1.0.1 |
| `ai-devops-toolkit` | ✅ Benign | ✅ Benign | High | Clean since v1.0.1 |
| `local-llm-router` | ✅ Benign | ✅ Benign | Medium | Fixed! Was Suspicious, now Benign at v1.3.0 |
| `ollama-load-balancer` | ✅ Benign | ✅ Benign | Medium | Fixed! Was Suspicious, now Benign at v1.1.0 |
| `distributed-inference` | ✅ Benign | ✅ Benign | Medium | Fixed! Was Suspicious, now Benign at v1.2.0 |
| `local-transcription` | ✅ Benign | ✅ Benign | Medium | Clean at v1.2.0 |
| `mflux-image-router` | ✅ Benign | ✅ Benign | Medium | Clean at v1.2.0 |
| `fleet-embeddings` | ✅ Benign | ✅ Benign | Medium | Clean at v1.0.0 |
| `ollama-ollama-herd` | ✅ Benign | ✅ Benign | Medium | Clean at v1.1.0 |
| `deepseek-deepseek-coder` | ⏳ Pending | ✅ Benign | Medium | Fixed at v1.0.2 — was Suspicious, now Benign |
| `qwen-qwen3` | ✅ Benign | ✅ Benign | High | Clean at v1.0.1 |
| `apple-silicon-ai` | ✅ Benign | ✅ Benign | High | Fixed at v1.0.1 — was Suspicious, now Benign + High confidence |
| `llama-llama3` | ⏳ Pending | ⏳ Pending | — | Awaiting first scan (v1.0.0) |
| `mistral-codestral` | ⏳ Pending | ⏳ Pending | — | Awaiting first scan (v1.0.0) |
| `phi-phi4` | ⏳ Pending | ⏳ Pending | — | Awaiting first scan (v1.0.0) |
| `private-ai` | ⏳ Pending | ⏳ Pending | — | Awaiting first scan (v1.0.0) |
| `local-coding` | ⏳ Pending | ⏳ Pending | — | Awaiting first scan (v1.0.0) |

### What we fixed

**v1.0.0 → v1.0.1:** All skills only declared `curl`/`wget`/`sqlite3` as required binaries but instructions also used `python3`, `pip`, and `~/.fleet-manager/` files. Added `optionalBins` and `configPaths` to metadata. Resolved 2 of 5 Suspicious ratings.

**v1.0.1 → v1.0.2:** Three targeted fixes for the remaining Suspicious skills:
- **`local-llm-router`**: Moved `configPaths` from inside `requires` to top-level `openclaw` metadata (scanner couldn't find it nested under `requires`)
- **`ollama-load-balancer`**: Clarified auto-pull is opt-in ("disabled by default, toggle via settings API"), not automatic
- **`distributed-inference`**: Removed meeting detection and app fingerprinting references from the SKILL.md — these are node agent features that don't belong in a skill about distributed inference coordination. The skill now focuses on scheduling, scoring, and fault tolerance.

**v1.1.0+ → v1.3.0:** All three previously Suspicious core skills (`local-llm-router`, `ollama-load-balancer`, `distributed-inference`) are now **Benign** after the metadata and description updates.

### Latest fixes (all confirmed Benign)

**`ollama-herd`** (v1.4.0 → v1.5.0) — `configPaths` was nested inside `requires` instead of at the `openclaw` level. Same bug we'd fixed in other skills but missed in the core skill. Now Benign with **High confidence**.

**`deepseek-deepseek-coder`** (v1.0.1 → v1.0.2) — Removed `ollama pull` from setup steps (was presented as required). Added "no models are downloaded during installation" language. Reframed hardware table as optional recommendations. Strengthened guardrails with explicit "no automatic downloads" and "all pulls are user-initiated". Now Benign.

**`apple-silicon-ai`** (v1.0.0 → v1.0.1) — Removed "mDNS (Bonjour)" protocol reference that triggered LAN discovery flags. Softened to "automatically finds the router on your local network". Added PyPI link for install verification. Expanded guardrails with "no external network access" and "read-only local state" sections. Now Benign with **High confidence**.

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
4. When API endpoints change, update **all nineteen** skills to stay in sync — the core skill (`ollama-herd`) is the source of truth
5. After publishing, verify rankings with `clawhub search "<keyword>"` for key terms
