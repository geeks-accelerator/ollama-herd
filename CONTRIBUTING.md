# Contributing to Ollama Herd

Thanks for your interest in contributing! Whether you're fixing a bug, adding a feature, or improving docs — we appreciate it.

## Getting Started

```bash
git clone https://github.com/geeks-accelerator/ollama-herd.git
cd ollama-herd
uv sync --extra dev  # installs test + lint deps
```

## Development Workflow

### Run the tests

```bash
uv run pytest           # all 445 tests (~5s)
uv run pytest -v        # verbose output
uv run pytest tests/test_server/  # server tests only
uv run pytest tests/test_models/  # model tests only
```

### Lint and format

```bash
uv run ruff check src/   # lint
uv run ruff format src/  # format
```

### Full health check

```bash
./scripts/health.sh
```

## Code Style

- **Fully async** — no sync blocking calls
- **Pydantic v2** models for all data structures
- **Ruff** for linting and formatting (config in `pyproject.toml`)
- **Line length:** 100 characters
- Route files live in `server/routes/`, one per API surface
- Keep it simple — read the Design Principles in `CLAUDE.md`

## Pull Request Process

1. **Fork the repo** and create your branch from `main`
2. **Write tests** for any new functionality
3. **Run the full test suite** — all tests must pass
4. **Run the linter** — no lint errors
5. **Keep commits focused** — one logical change per commit
6. **Write a clear PR description** explaining what and why

### PR Title Convention

Use a short, descriptive title that starts with a verb:

- `Add model fallback support for multi-node routing`
- `Fix stale httpx client after connection disruption`
- `Update scoring engine to include context fit signal`

## What to Contribute

### Good first issues

- Improving test coverage for edge cases
- Documentation fixes and clarifications
- Adding type hints where missing

### Bigger contributions

- New scoring signals for the routing engine
- Additional API compatibility (e.g., Anthropic format)
- Performance improvements to the streaming proxy
- Dashboard enhancements

If you're planning a larger change, **open an issue first** to discuss the approach. This saves everyone time and ensures alignment with the project's design principles.

## Architecture Overview

See `CLAUDE.md` for the full architecture breakdown, key modules, and request flow. The short version:

- `herd` — FastAPI router server (scoring, queuing, streaming, dashboard)
- `herd-node` — agent on each device (heartbeats, metrics, capacity learning)
- mDNS for zero-config discovery
- SQLite for persistence (latency, traces)
- Everything is async, everything is HTTP, everything is simple

## Reporting Bugs

Open a [GitHub issue](https://github.com/geeks-accelerator/ollama-herd/issues) with:

- Steps to reproduce
- Expected vs actual behavior
- Your environment (OS, Python version, Ollama version)
- Relevant log output (check `~/.fleet-manager/logs/`)

## Security Issues

**Do not open a public issue for security vulnerabilities.** See [SECURITY.md](SECURITY.md) for responsible disclosure instructions.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
