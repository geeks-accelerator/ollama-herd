# Skill Marketplace Analysis

Competitive landscape research for Ollama Herd skills on ClawHub and other skill registries. Last updated: March 2026.

## Platform Overview

ClawHub (clawhub.ai) is the official skill registry for OpenClaw — think "npm for AI agents." ~3,300 verified skills after a major security purge in February 2026 that removed malicious entries. Skills are SKILL.md files with YAML frontmatter (instructions, not code). Discovery uses vector (semantic) search on name, description, and tags.

## Key Finding: The Space Is Completely Empty

**Zero skills on ClawHub do what Ollama Herd does.** No local inference routing, no multi-node fleet management, no Ollama load balancing, no GPU cluster management. The entire category of "local inference infrastructure" is unoccupied.

Every one of these searches returns zero results:

| Search Term | Results |
|---|---|
| `inference routing` | 0 |
| `llm routing` | 0 |
| `load balancer` | 0 |
| `ollama load balancer` | 0 |
| `gpu cluster` | 0 (local sense) |
| `model router` | 0 |
| `fleet management` | 0 |
| `distributed inference` | 0 |
| `multi-node` | 0 |
| `apple silicon` | 0 |
| `home lab` | 0 |
| `local llm` (infrastructure) | 0 |
| `model management` | 0 |
| `self-hosted llm` | 0 |
| `ai infrastructure` | 0 |
| `gpu inference` | 0 |

The `ollama` search returns 3 skills, but none manage Ollama fleets — they use Ollama as a dependency for other tasks.

## Closest Competitors (None Are Direct)

### Token Optimizer (`smartpeopleconnected/token-optimizer`)
- **Installs:** 63 | **Downloads:** 6.1k | **Stars:** 23
- **Tags:** cost-savings, model-routing, token-optimization
- **What it does:** Routes OpenClaw tasks to cheaper LLM providers (Haiku vs Opus). Uses Ollama for free heartbeat checks.
- **Relevance:** "Model routing" at the API provider tier (which model to call), not infrastructure tier (which machine handles the request). Not a competitor.

### OpenClaw Token Optimizer (`Asif2BD/openclaw-token-optimizer`)
- **Installs:** 54
- **Tags:** cost-savings, lazy-loading, model-routing, productivity
- **What it does:** Similar to above — routes simple tasks to cheap models, optimizes heartbeat intervals.
- **Relevance:** Same category. No infrastructure routing.

### GPU CLI (`angusbezzina/gpu-cli`)
- **Installs:** 1 | **Downloads:** 353
- **What it does:** Wrapper for remote GPU pod management (cloud rentals, training jobs, budget caps).
- **Relevance:** GPU-adjacent but for cloud GPU rentals, not local fleet management.

### OpenClaw Command Center (`jontsai/command-center`)
- **Installs:** 55 | **Downloads:** 7.5k | **Stars:** 52
- **What it does:** Self-hosted Node.js dashboard for monitoring OpenClaw sessions, LLM usage, system vitals.
- **Relevance:** Monitoring dashboard but for OpenClaw agent sessions, not Ollama infrastructure.

### Agent Team Orchestration (`arminnaimi/agent-team-orchestration`)
- **Installs:** 163 | **Downloads:** 12.8k | **Stars:** 41
- **What it does:** Multi-agent task routing with role definitions and handoff protocols.
- **Relevance:** "Routing" in the agent-task sense, not compute infrastructure sense.

### Chromadb Memory (`msensintaffar/chromadb-memory`)
- **Installs:** 22 | **Downloads:** 4.2k
- **Tags:** memory, chromadb, ollama, vector-search, local, self-hosted, auto-recall
- **What it does:** Long-term agent memory using ChromaDB with Ollama embeddings.
- **Relevance:** Uses Ollama for embeddings. Nothing to do with fleet management.

### ClawAPI Manager (`2233admin/clawapi-manager`)
- **Installs:** 2 | **Downloads:** 345 | **Status:** Flagged suspicious
- **What it does:** Multi-provider API key management, round-robin key rotation, failover.
- **Relevance:** Closest conceptually to "routing" but operates at the API key level.

### LobeHub Ollama Manager (not on ClawHub)
- **Platform:** LobeHub marketplace (separate from ClawHub)
- **What it does:** Single-node Ollama model lifecycle management (pull, delete, list).
- **Relevance:** Model management but single-node only. No fleet orchestration.

## Category Map

The existing skills on ClawHub fall into clear categories. Ollama Herd sits in an empty one:

```
OCCUPIED CATEGORIES:
├── LLM API Routing        → Token Optimizer, ClawAPI Manager (choose which provider)
├── Agent Orchestration     → Agent Team Orchestration (choose which agent)
├── Session Monitoring      → Command Center (watch OpenClaw sessions)
├── Memory/RAG             → Chromadb Memory (local vector stores)
└── Cloud GPU Management   → GPU CLI (rent cloud GPUs)

EMPTY CATEGORY (Ollama Herd):
└── Local Inference Infrastructure
    ├── Multi-node fleet management     ← ollama-herd
    ├── Inference routing/scoring       ← local-llm-router
    ├── Load balancing/failover         ← ollama-load-balancer
    └── Home lab GPU clustering         ← gpu-cluster-manager
```

## Tag Strategy

### Principles

1. **Own the empty keywords** — every zero-result search term above should appear in at least one of our skills' tags
2. **Borrow successful tags** — `cost-savings` (Token Optimizer's best tag), `local`, `self-hosted` (Chromadb Memory's tags) have proven search volume
3. **Differentiate per skill** — minimal tag overlap between our own skills maximizes total search surface

### Recommended Tags Per Skill

| Skill | Tags (ordered by priority) |
|---|---|
| **ollama-herd** | `ollama, fleet-management, inference-routing, model-management, monitoring, dashboard, local-llm, self-hosted` |
| **local-llm-router** | `local-llm, inference-routing, model-router, llm-routing, ollama, load-balancing, latency, scoring, multi-node` |
| **ollama-load-balancer** | `load-balancer, ollama, health-check, auto-discovery, high-availability, failover, monitoring, self-hosted, distributed-inference` |
| **gpu-cluster-manager** | `gpu-cluster, apple-silicon, homelab, local-ai, self-hosted, zero-config, gpu-inference, home-lab, cost-savings` |

### Keyword Coverage Matrix

Shows which of our skills covers each target keyword:

| Keyword | ollama-herd | local-llm-router | ollama-load-balancer | gpu-cluster-manager |
|---|---|---|---|---|
| ollama | x | x | x | |
| fleet-management | x | | | |
| inference-routing | x | x | | |
| model-management | x | | | |
| load-balancer | | x | x | |
| gpu-cluster | | | | x |
| apple-silicon | | | | x |
| homelab / home-lab | | | | x |
| self-hosted | x | | x | x |
| local-llm | x | x | | |
| model-router | | x | | |
| llm-routing | | x | | |
| health-check | | | x | |
| auto-discovery | | | x | |
| high-availability | | | x | |
| failover | | | x | |
| multi-node | | x | | |
| distributed-inference | | | x | |
| gpu-inference | | | | x |
| zero-config | | | | x |
| cost-savings | | | | x |
| monitoring | x | | x | |
| dashboard | x | | | |
| scoring | | x | | |
| latency | | x | | |
| local-ai | | | | x |

**Total unique keywords covered: 25** across 4 skills.

## Gap Analysis: Additional Skill Opportunities

### Strong candidates

1. **`ollama-manager`** — LobeHub has one (single-node), ClawHub has none. Focus on model lifecycle: pull, delete, recommend, disk usage, per-model performance stats. Different from fleet management — this is "manage what's on your machines."
   - Tags: `ollama, model-management, model-lifecycle, pull, delete, disk-usage, recommendations`
   - Audience: Individual Ollama users who want better model management

2. **`ai-devops-toolkit`** — DevOps/SRE audience searching for AI infrastructure tooling. Frame Herd as an operational tool alongside Prometheus, Grafana, etc.
   - Tags: `devops, ai-infrastructure, observability, traces, metrics, sre, operations`
   - Audience: Platform engineers building AI infrastructure

### Weaker candidates (diminishing returns)

3. **`distributed-inference`** — Academic/research framing. Likely low search volume on ClawHub.
4. **`model-router`** — Already covered by `local-llm-router`. Dedicated skill would overlap too much.

### Recommendation

Our 4 skills cover the space well. The `ollama-manager` variant is the strongest gap — it targets a different user journey (single-node model management) vs fleet operations. Consider adding it later once the core 4 are published and we can see actual search analytics.

## First-Mover Advantage

OpenClaw's ecosystem emphasizes local/self-hosted operation (Ollama integration is first-class in OpenClaw). There is natural demand for infrastructure tooling. By publishing 4 skills covering 25 unique keywords in an empty category, Ollama Herd would:

- **Own the category** — first results for every infrastructure search
- **Set the standard** — future infrastructure skills will be compared against ours
- **Compound installs** — users who find one skill discover the others from the same publisher
- **Build authority** — 4 high-quality skills signals an active, invested publisher

The window is open now. Once the space fills, first-mover advantage compounds through install counts and star ratings that new entrants can't match.
