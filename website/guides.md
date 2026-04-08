# Guides

Everything you need to go from install to production fleet. Start at the top and work down, or jump to what you need.

## Start Here

- **[Quickstart](guides/quickstart.md)** — Install to first routed request in 60 seconds. Create a fleet with two commands, send a request, and see it land on the right machine.

- **[Core Concepts](guides/concepts.md)** — The mental model behind Ollama Herd. Nodes, heartbeats, scoring signals, queues, capacity modes, and how they fit together.

## Learn the System

- **[Routing Engine](guides/routing-engine.md)** — How the 5-stage scoring pipeline eliminates bad candidates, scores survivors across 7 signals, and picks a winner for every request.

- **[Adaptive Capacity](guides/adaptive-capacity.md)** — How your fleet learns when each device has spare compute. Weekly behavioral models, meeting detection, app fingerprinting, and memory ceilings.

## Put It to Work

- **[Integrations](guides/integrations.md)** — Connect Ollama Herd to Open WebUI, LangChain, CrewAI, OpenClaw, Aider, Continue.dev, LlamaIndex, and any OpenAI-compatible client.

- **[Deployment](guides/deployment.md)** — Multi-node setup, monitoring, log analysis, health checks, graceful drain, and production tips.

- **[API Reference](guides/api-reference.md)** — Every endpoint with request/response schemas, headers, error codes, and curl examples.
