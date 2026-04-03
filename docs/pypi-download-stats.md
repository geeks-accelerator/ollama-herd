# PyPI Download Stats

Tracked via [pypistats.org](https://pypistats.org/packages/ollama-herd). Updated manually or via `pypistats` CLI.

## Latest Snapshot (2026-04-03)

### Recent Downloads

| Period | Downloads |
|--------|-----------|
| Last day | 142 |
| Last week | 390 |
| Last month | 718 |

### Overall Downloads

| Category | Downloads |
|----------|-----------|
| With mirrors | 2,073 |
| Without mirrors | 718 |

### By Operating System

| OS | Downloads |
|----|-----------|
| Windows | 91 |
| Linux | 55 |
| macOS | 16 |
| Unknown | 556 |

### By Python Version

| Version | Downloads |
|---------|-----------|
| 3.13 | 93 |
| 3.11 | 39 |
| 3.12 | 26 |
| 3.14 | 4 |
| Unknown | 556 |

## How to Update

```bash
# Install pypistats (one-time)
uv tool install pypistats

# Fetch all stats
pypistats recent ollama-herd
pypistats overall ollama-herd
pypistats system ollama-herd
pypistats python_minor ollama-herd
```

## Notes

- "Without mirrors" is the more accurate number — "with mirrors" includes CI bots, CDN caches, and automated tooling
- "Unknown" OS/Python version comes from downloads that don't send user-agent metadata (typically CI pipelines and mirror syncs)
- PyPI stats have a ~24h delay from [Google BigQuery](https://packaging.python.org/en/latest/guides/analyzing-pypi-package-downloads/)
