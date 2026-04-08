# Privacy Policy

**Last updated:** April 8, 2026
**Operator:** Geeks in the Woods, LLC (Alaska, USA)

---

## 1. What Ollama Herd Is

Ollama Herd is open-source, self-hosted software that runs entirely on your own devices. It routes AI inference requests across machines on your local network. There is no cloud component, no hosted service, and no account system.

When you run Ollama Herd, all data stays on your machines. We have no access to your network, your models, your requests, or your responses.

## 2. What the Software Collects (On Your Machines)

Ollama Herd stores the following data locally on the machine running the router. This data never leaves your network.

**Request traces** — Each inference request is logged to a local SQLite database (`~/.fleet-manager/latency.db`) with: model name, node selected, scoring breakdown, latency, token counts, retry/fallback status, and request tags. No prompt content or response content is stored.

**Structured logs** — JSONL logs written to `~/.fleet-manager/logs/` containing operational events: node heartbeats, queue state changes, errors, and routing decisions. No prompt or response content is logged.

**Capacity learner state** — JSON files tracking per-node availability patterns (168-slot weekly behavioral model). Contains only availability scores and timestamps.

**Node heartbeats** — System metrics sent from each node agent to the router over your local network: CPU usage, memory usage, thermal state, loaded models, disk space, Ollama version. No personal data, no browsing history, no application names.

**Meeting detection (macOS only)** — Checks whether a camera or microphone is active. Returns only a boolean (in-meeting or not). Does not record audio, video, or application names.

**App fingerprinting** — Classifies workload intensity (idle/light/moderate/heavy/intensive) using aggregate CPU, memory, and network metrics. Does not read application names, window titles, or file contents.

## 3. What We Collect

**From the software itself:** Nothing. Ollama Herd has no telemetry, no analytics, no phone-home behavior, and no crash reporting. We cannot see your data.

**From the website (ollamaherd.com):** If you visit our website, we use privacy-respecting analytics to understand traffic patterns. We collect no personally identifiable information from website visitors. We do not use advertising pixels or tracking cookies.

**From PyPI downloads:** PyPI provides aggregate download statistics. We can see total download counts but not who downloaded the package.

**From GitHub:** GitHub provides repository traffic statistics (views, clones). We can see aggregate counts but not individual users.

## 4. What We Do Not Collect

- Prompt content or AI responses
- Personal information, names, or email addresses (there is no account system)
- IP addresses (the software runs on your LAN — we have no access)
- Browsing history, application names, or window titles
- Model weights or fine-tuning data
- Any data from your local network

## 5. Third-Party Services

Ollama Herd itself connects to no third-party services. Your local Ollama instances may download models from ollama.com — that is governed by Ollama's own privacy policy.

The optional web dashboard loads Chart.js from a CDN (`cdn.jsdelivr.net`) for visualizations. This is the only external network request the dashboard makes. You can self-host Chart.js to eliminate this.

## 6. Data Retention

All data is stored locally on your machines. You control retention:

- **SQLite database:** Grows over time. Delete or truncate at will.
- **JSONL logs:** Rotated daily by filename. Delete old log files at will.
- **Capacity learner state:** JSON files. Delete to reset learned patterns.

There is no remote data to delete because no remote data exists.

## 7. Security

All communication between nodes and the router happens over HTTP on your local network. Ollama Herd does not implement authentication or encryption at the application layer — it is designed for trusted local networks.

If you need to expose Ollama Herd beyond your LAN, we recommend placing it behind a reverse proxy (nginx, Caddy, Traefik) with TLS and authentication.

## 8. Children's Privacy

Ollama Herd is developer tools with no account system and no data collection. There are no age-related concerns.

## 9. GDPR and CCPA

Since Ollama Herd collects no personal data and sends no data to us, GDPR and CCPA data subject rights (access, deletion, portability) are satisfied by default. All data is on your machines under your control.

If you interact with our website, you may contact us to exercise your rights regarding any website analytics data.

## 10. Changes to This Policy

We may update this policy to reflect changes in the software or legal requirements. Changes will be posted on this page with an updated date.

## 11. Contact

**Geeks in the Woods, LLC**
- Email: hello@geeksinthewoods.com
- GitHub: [geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)
- Web: [geeksinthewoods.com](https://geeksinthewoods.com)
