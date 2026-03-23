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

## Competitive Landscape

As of March 2026, **zero skills on ClawHub** occupy the local inference infrastructure category. The entire space — inference routing, load balancing, fleet management, GPU clustering — is empty. Our 7 skills cover 35+ unique search keywords across an uncontested category. See [Skill Marketplace Analysis](../docs/skill-marketplace-analysis.md) for the full competitive research, keyword coverage matrix, and gap analysis.

## Publishing

### ClawHub

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

## Updating

1. Edit the relevant `SKILL.md` file
2. Bump the `version` in the YAML frontmatter
3. Publish with the new version number
4. When API endpoints change, update **all seven** skills to stay in sync — the core skill (`ollama-herd`) is the source of truth
