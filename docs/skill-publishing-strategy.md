# Skill Publishing Strategy: Multiple Entry Points, One API

A guide to maximizing discoverability on skill marketplaces (ClawHub, OpenClaw, etc.) by publishing multiple skill files that share the same underlying API but target different audiences, search terms, and use cases.

## The core idea

One product. Many doors.

Ollama Herd has a single API. But people search for it using different words depending on their role, problem, or mental model. An ML engineer searches "local LLM routing." A sysadmin searches "Ollama load balancer." A home lab enthusiast searches "GPU cluster manager." They all need the same tool.

Instead of publishing one skill and hoping everyone finds it, publish multiple skills. Each one is a complete, self-contained entry point with the same API calls but different:

- **Name and description** (what shows in search results)
- **Voice and tone** (who it feels written for)
- **Opening hook** (what problem it leads with)
- **Typical workflow** (which commands are highlighted first)
- **Tags** (what search terms it matches)

## How it works

```
skills/
  ollama-herd/              # The "core" skill — general purpose fleet management
    SKILL.md
  local-llm-router/         # Same API, ML engineer framing
    SKILL.md
  ollama-load-balancer/     # Same API, DevOps/sysadmin framing
    SKILL.md
  gpu-cluster-manager/      # Same API, home lab framing
    SKILL.md
```

Every SKILL.md is fully self-contained. Someone who installs only one skill gets the complete API reference. No dependencies between skills. No "see the core skill for details."

## What stays the same across all skills

- API endpoints and curl examples
- Router URL (`http://localhost:11435`)
- Dashboard sections and capabilities
- Guardrails (no unauthorized restarts, no deleting data, etc.)
- Failure handling steps
- SQLite trace database queries

## What changes per skill

| Element | Why it varies |
|---|---|
| Name | Different search terms match different audiences |
| Description (1-2 lines) | The hook that appears in marketplace listings |
| Emoji | Visual differentiation in skill lists |
| Tags | Search keyword coverage |
| "What This Solves" section | Different problems framed for different people |
| Voice and tone | DevOps vs ML engineer vs hobbyist |
| Feature ordering | Which capabilities are shown first and emphasized |
| Examples | Tailored scenarios that resonate with the target audience |

## Practical example: Ollama Herd

| Skill name | Audience | Hook |
|---|---|---|
| ollama-herd | Fleet operators | "Manage your Ollama Herd device fleet" |
| local-llm-router | ML engineers, AI developers | "Smart routing for local LLM inference — 7-signal scoring picks the optimal device for every request" |
| ollama-load-balancer | DevOps, sysadmins | "Load balance Ollama across machines with auto-discovery, health checks, and zero config" |
| gpu-cluster-manager | Home lab enthusiasts | "Turn your spare GPUs into one inference endpoint — pip install, two commands, done" |

Same API calls in every file. Different packaging.

## Writing guidelines

### Keep each skill self-contained

The number one rule. Every SKILL.md must include the complete API reference. Do not say "see the core skill for full documentation." Someone will install exactly one skill, and it needs to work.

### Match the voice to the audience

A DevOps load-balancer skill should sound operational — uptime, health checks, auto-recovery. An ML engineer routing skill should sound technical — scoring signals, latency percentiles, context fit. A home lab skill should sound enthusiastic — "turn spare machines into one brain."

### Lead with their problem, not your solution

Each "What This Solves" section should describe the pain point the target audience actually feels:

- ML engineer: "You have 3 machines with GPUs but your inference script only talks to one. Switching models between machines means editing configs and restarting."
- DevOps: "Ollama has no built-in load balancing. One machine goes down, your app gets errors. No health checks, no failover, no queue management."
- Home lab: "Your Mac Studio, MacBook, and old gaming PC all have GPUs sitting idle. You want one endpoint that uses all of them without Kubernetes or Docker."

### Use different tags

Overlap is fine, but each skill should have unique tags that expand your search surface:

```yaml
# Core skill
tags: [ollama, herd, fleet, inference, routing, management, monitoring, dashboard]

# Local LLM router
tags: [local-llm, inference, routing, load-balancing, ollama, model-selection, latency, scoring]

# Load balancer
tags: [load-balancer, ollama, health-check, monitoring, auto-discovery, high-availability, failover]

# GPU cluster manager
tags: [gpu, cluster, home-lab, apple-silicon, inference, local-ai, self-hosted, zero-config]
```

## How many skills is too many?

Diminishing returns kick in around 4-6 for a focused tool like Ollama Herd. Each skill should target a genuinely different audience or search pattern. If you can't write a distinct "What This Solves" section, the skill doesn't need to exist.

Signs you have the right number:
- Each skill would rank for different search queries
- You can describe each audience in one sentence
- The "What This Solves" sections don't overlap significantly
- Someone from each target audience would feel "this was written for me"

## Maintenance

The biggest risk is skills going out of sync when the API changes. Mitigate this by:

1. Keep the core skill (`ollama-herd`) as the source of truth
2. When API changes, update the core skill first
3. Propagate API endpoint/curl changes to all variant skills
4. Only the framing/voice sections need manual updates per skill

When new features are added (like context protection, VRAM fallback, settings dashboard), update all skills to include them — but frame each feature differently per audience:
- Core: "Context protection strips `num_ctx` to prevent reload hangs"
- ML engineer: "Context-size protection prevents Ollama from reloading models when `num_ctx` changes"
- DevOps: "Auto-strips dangerous `num_ctx` parameters that would trigger multi-minute model reloads"
- Home lab: "Automatically prevents your 89GB model from reloading when apps send different context sizes"

## Results to expect

On ClawHub and similar marketplaces:
- Each skill appears as a separate listing in search results
- Different search queries surface different entry points
- Total install count is the sum across all skills
- Users who find one skill may discover others from the same publisher

This is not gaming the system. Each skill genuinely serves a different audience with different framing. The value delivered is identical, the packaging is tailored.
