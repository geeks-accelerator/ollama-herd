# Ollama Herd Skills

This directory contains skills that let AI agents discover and manage Ollama Herd fleets. Multiple skills target different audiences with the same underlying API — see [Skill Publishing Strategy](../docs/skill-publishing-strategy.md) for the rationale.

## Directory Structure

```
skills/
├── README.md                  # This file
├── ollama-herd/               # Core skill — general fleet management
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
└── distributed-inference/     # Systems framing — architecture, scoring, fault tolerance
    └── SKILL.md
```

## Current Skills

| Slug | Version | Audience | Voice |
|------|---------|----------|-------|
| `ollama-herd` | 1.1.0 | Fleet operators | Operational — "manage your fleet" |
| `local-llm-router` | 1.0.0 | ML engineers | Technical — scoring signals, latency optimization |
| `ollama-load-balancer` | 1.0.0 | DevOps, sysadmins | Operational — health checks, failover, uptime |
| `gpu-cluster-manager` | 1.0.0 | Home lab enthusiasts | Enthusiastic — "turn spare GPUs into one brain" |
| `ollama-manager` | 1.0.0 | Individual Ollama users | Practical — "wrangle your models, clean up the mess" |
| `ai-devops-toolkit` | 1.0.0 | Platform engineers, SRE | Analytical — traces, percentiles, per-app analytics |
| `distributed-inference` | 1.0.0 | Systems engineers, researchers | Academic — architecture, scoring function, data model |

All seven skills share the same API endpoints. Each is fully self-contained — installing any one skill gives the complete API reference. The difference is framing, voice, and which features are highlighted first.

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

## Competitive Landscape

As of March 2026, **zero skills on ClawHub** occupy the local inference infrastructure category. The entire space — inference routing, load balancing, fleet management, GPU clustering — is empty. Our 7 skills cover 35+ unique search keywords across an uncontested category. See [Skill Marketplace Analysis](../docs/skill-marketplace-analysis.md) for the full competitive research, keyword coverage matrix, and gap analysis.

## Publishing

### ClawHub

Skills are published under the **`@twinsgeeks`** account. Before publishing, always verify authentication:

```bash
clawhub whoami    # must show: ✔ twinsgeeks
```

If not logged in as `@twinsgeeks`, authenticate first (API key stored in `skills/.env`).

```bash
# Core skill
clawhub --workdir skills --registry https://clawhub.ai publish ollama-herd \
  --slug ollama-herd \
  --name "Ollama Herd — Fleet Management for Local LLM Inference" \
  --version 1.1.0 \
  --tags "ollama,fleet-management,inference-routing,model-management,monitoring,dashboard,local-llm,self-hosted"

# ML engineer variant
clawhub --workdir skills --registry https://clawhub.ai publish local-llm-router \
  --slug local-llm-router \
  --name "Local LLM Router — Smart Inference Routing Across Devices" \
  --version 1.0.0 \
  --tags "local-llm,inference-routing,model-router,llm-routing,ollama,load-balancing,latency,scoring,multi-node"

# DevOps variant
clawhub --workdir skills --registry https://clawhub.ai publish ollama-load-balancer \
  --slug ollama-load-balancer \
  --name "Ollama Load Balancer — Auto-Discovery, Health Checks, Failover" \
  --version 1.0.0 \
  --tags "load-balancer,ollama,health-check,auto-discovery,high-availability,failover,monitoring,self-hosted,distributed-inference"

# Home lab variant
clawhub --workdir skills --registry https://clawhub.ai publish gpu-cluster-manager \
  --slug gpu-cluster-manager \
  --name "GPU Cluster Manager — Combine Spare GPUs Into One Endpoint" \
  --version 1.0.0 \
  --tags "gpu-cluster,apple-silicon,homelab,local-ai,self-hosted,zero-config,gpu-inference,home-lab,cost-savings"

# Model lifecycle variant
clawhub --workdir skills --registry https://clawhub.ai publish ollama-manager \
  --slug ollama-manager \
  --name "Ollama Manager — Model Lifecycle Across Machines" \
  --version 1.0.0 \
  --tags "ollama,model-management,model-lifecycle,pull,delete,disk-usage,recommendations,cleanup"

# DevOps/SRE variant
clawhub --workdir skills --registry https://clawhub.ai publish ai-devops-toolkit \
  --slug ai-devops-toolkit \
  --name "AI DevOps Toolkit — Traces, Analytics & Health for LLM Infrastructure" \
  --version 1.0.0 \
  --tags "devops,ai-infrastructure,observability,traces,metrics,sre,operations,analytics,monitoring"

# Systems/research variant
clawhub --workdir skills --registry https://clawhub.ai publish distributed-inference \
  --slug distributed-inference \
  --name "Distributed Inference — Multi-Node LLM Scheduling & Fault Tolerance" \
  --version 1.0.0 \
  --tags "distributed-inference,scheduling,fault-tolerance,multi-node,heterogeneous,scoring,coordination,llm-infrastructure"
```

### Other Registries

| Registry | How |
|----------|-----|
| **ClawHub** | `clawhub publish` (see above) |
| **Skills.sh** | `npx skills add geeks-accelerator/ollama-herd` |
| **SkillsMP** | Auto-indexed from GitHub (needs 2+ stars) |

## Security Scan Status

ClawHub runs automated security scans via VirusTotal and OpenClaw on every published skill version. Here's the current status and our rationale for each finding.

### Current Ratings (v1.0.2 / v1.1.0)

| Skill | VirusTotal | OpenClaw | Confidence | Notes |
|-------|-----------|----------|------------|-------|
| `ollama-herd` | ✅ Benign | ✅ Benign | Medium | Clean — all requirements declared |
| `ollama-manager` | ✅ Benign | ✅ Benign | High | Highest confidence across all skills |
| `gpu-cluster-manager` | ✅ Benign | ✅ Benign | Medium | Fixed in v1.0.1 |
| `ai-devops-toolkit` | ✅ Benign | ✅ Benign | Medium | Fixed in v1.0.1 |
| `local-llm-router` | ✅ Benign | ⚠️ Suspicious | Medium | Awaiting rescan after v1.0.2 metadata fix |
| `ollama-load-balancer` | ✅ Benign | ⚠️ Suspicious | Medium | Auto-pull side effects (legitimate, guarded) |
| `distributed-inference` | ✅ Benign | ⚠️ Suspicious | Medium | Awaiting rescan after v1.0.2 privacy fix |

### What we fixed

**v1.0.0 → v1.0.1:** All skills only declared `curl`/`wget`/`sqlite3` as required binaries but instructions also used `python3`, `pip`, and `~/.fleet-manager/` files. Added `optionalBins` and `configPaths` to metadata. Resolved 2 of 5 Suspicious ratings.

**v1.0.1 → v1.0.2:** Three targeted fixes for the remaining Suspicious skills:
- **`local-llm-router`**: Moved `configPaths` from inside `requires` to top-level `openclaw` metadata (scanner couldn't find it nested under `requires`)
- **`ollama-load-balancer`**: Clarified auto-pull is opt-in ("disabled by default, toggle via settings API"), not automatic
- **`distributed-inference`**: Removed meeting detection and app fingerprinting references from the SKILL.md — these are node agent features that don't belong in a skill about distributed inference coordination. The skill now focuses on scheduling, scoring, and fault tolerance.

### Remaining Suspicious ratings — rationale

**`local-llm-router`** — The `configPaths` metadata placement was fixed in v1.0.2. Awaiting scanner rescan. The metadata now correctly declares both `~/.fleet-manager/latency.db` and `~/.fleet-manager/logs/herd.jsonl` at the top-level `openclaw` scope.

**`ollama-load-balancer`** — Flagged for auto-pull side effects: the skill documents `POST /dashboard/api/pull` which can trigger large downloads. This is legitimate functionality with guardrails requiring explicit user confirmation. The scanner correctly identifies the side effect but our guardrails section addresses it.

**`distributed-inference`** — Was flagged for privacy concerns around meeting detection and app fingerprinting. These references were removed in v1.0.2 since they're node agent features, not coordinator features. Awaiting rescan.

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
3. Publish with the new version number
4. When API endpoints change, update **all seven** skills to stay in sync — the core skill (`ollama-herd`) is the source of truth
