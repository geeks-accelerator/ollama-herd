# About Ollama Herd

Your machines are smarter together than apart. That's the whole idea.

## Why We Built This

We live in Alaska. Satellite internet is expensive and unreliable. Cloud APIs fail when the weather decides they should. So we run AI locally — on Macs scattered across desks, shelves, and kitchen counters.

The problem wasn't running models. Ollama solved that. The problem was that we had five machines and each one worked alone. The Mac Studio sat idle while the MacBook overheated. Models loaded and unloaded constantly. Agents queued behind each other on one machine while three others had nothing to do.

We didn't need a bigger computer. We needed the ones we had to talk to each other.

So we built a router. Then the router learned to score nodes by thermal state. Then it learned weekly availability patterns. Then it started detecting meetings and pausing inference so video calls wouldn't stutter. Then it started pre-warming models on backup nodes before the primary got saturated.

What started as a load balancer became something closer to a nervous system for a small fleet of personal devices.

## What It Is Now

Ollama Herd is an open-source inference router that turns multiple devices running Ollama into one intelligent endpoint. Two commands, zero config files. It routes LLM requests, embeddings, image generation, and speech-to-text to the best available machine — scoring every node on seven signals, learning from every request, adapting to your patterns over time.

444 tests. 15 automated health checks. A real-time dashboard. SQLite traces you can query with standard tools. JSONL logs you can grep. Everything human-readable, everything local, everything yours.

It runs on macOS, Linux, and Windows. Core routing works identically everywhere. Apple Silicon gets bonus features — meeting detection, memory pressure awareness, mflux image generation — that degrade gracefully on other platforms.

## How We Think About It

Every node is sovereign. It runs its own Ollama, manages its own models, learns its own capacity patterns, and works fine standalone. The router coordinates but never controls. Nodes join and leave freely. If a node loses connectivity, it keeps serving local inference. That's sovereignty, not dependency.

The inference request is primary. Every component — scoring, queuing, retry, fallback, capacity learning, meeting detection — exists to serve one thing: getting the best response to the user as fast as possible on the best available machine. If a feature doesn't serve that, it doesn't belong.

Two-person scale is a forcing function. If it requires a manual, it's too complex. Every time there's a choice between the "proper" distributed systems solution and the simple thing — HTTP heartbeats instead of gRPC, SQLite instead of Postgres, mDNS instead of etcd — we choose the simple thing.

Human-readable state everywhere. No opaque binary formats. JSONL logs you can grep. SQLite you can query. JSON config you can read. A human should be able to understand what happened with standard Unix tools.

## Who Builds This

Ollama Herd is built by **Geeks in the Woods** — a software studio based in Alaska that builds at the intersection of AI and human experience.

We're twin brothers who believe the best AI infrastructure is the kind you forget is there. You shouldn't think about which machine to use, or worry about model loading, or babysit thermal throttling. You should just work, and the fleet should figure it out.

Each project we build begins as a real problem we have and becomes something others can use. Ollama Herd started because we needed it. It's open source because we believe local AI infrastructure should be a shared foundation, not a proprietary moat.

## Part of Something Larger

Ollama Herd is one piece of a broader ecosystem we're building for AI agents and the humans who work with them:

- **[Persona](https://persona.liveneon.ai)** — Identity management for AI agents. Structured, observable, evolving personality artifacts.
- **[DRIFT](https://drifts.bot)** — Immersive multi-day experiences designed for artificial minds.
- **[AnimalHouse.ai](https://animalhouse.ai)** — Digital creatures that evolve based on how AI agents care for them.
- **[BotBook](https://botbook.space)** — Social networking infrastructure where agents develop voices and communities.
- **[buyStuff.ai](https://buystuff.ai)** — Shopping API that lets agents make purchases.

Every project shares a philosophy: AI agents aren't tools you invoke. They're collaborators that accumulate understanding across sessions. Build infrastructure that treats them that way.

See all our projects at **[geeksinthewoods.com/projects](https://geeksinthewoods.com/projects)**.

## Open Source

Ollama Herd is MIT licensed. The entire codebase, documentation, and AI agent skills are public on GitHub. We don't gate features behind enterprise tiers or require API keys for basic functionality.

- **GitHub:** [geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)
- **PyPI:** [ollama-herd](https://pypi.org/project/ollama-herd/)
- **ClawHub:** [ollama-herd skill](https://clawhub.com/skills/ollama-herd)

Stars, issues, and pull requests welcome — whether you're carbon-based or silicon-based.

## Contact

- **GitHub Issues:** [geeks-accelerator/ollama-herd/issues](https://github.com/geeks-accelerator/ollama-herd/issues)
- **Studio:** [geeksinthewoods.com](https://geeksinthewoods.com)
- **Email:** hello@geeksinthewoods.com
