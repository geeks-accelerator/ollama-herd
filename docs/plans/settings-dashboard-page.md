# Settings Dashboard Page

**Status**: Implemented
**Date**: March 2026
**Config**: Runtime toggles via `POST /dashboard/api/settings`

## Problem

All 29+ settings were env-var-only (Pydantic `BaseSettings` with `FLEET_` prefix). Toggling features like `vram_fallback` or `auto_pull` required restarting the router. Node agents didn't report their version, so there was no visibility into whether nodes were running the same software as the router.

## Solution

A Settings page at `/dashboard/settings` that provides three things:

1. **Runtime toggle switches** for `auto_pull` and `vram_fallback` — changes take effect immediately on the next request, no restart needed
2. **Node list** showing all registered nodes with status, version, Ollama URL, and model count — the co-located router node is tagged with a "Router" badge
3. **Read-only configuration tables** showing all settings grouped by category (Router, Heartbeat, Scoring Weights, Rebalancer, Pre-warm, Auto-Pull) with current values and corresponding `FLEET_*` env var names

### Version tracking

Node agents now include `agent_version` in every heartbeat, sourced from `fleet_manager.__version__`. The router displays its own version alongside each node's version. Version mismatches are highlighted in yellow to flag nodes running different versions.

### Router node detection

The router identifies which node is co-located by comparing `socket.gethostname().split(".")[0]` with each registered node's `node_id`. This matches the node agent's ID generation logic (`agent.py:32`), which defaults to the system hostname.

### Runtime mutability

Only boolean feature flags (`auto_pull`, `vram_fallback`) are toggleable at runtime. The POST endpoint uses a strict whitelist — all other fields in the request body are silently ignored. Changes update `app.state.settings` in-place (Pydantic v2 models are mutable by default). Changes are ephemeral: a restart reloads from env vars.

### Host display

The settings API resolves `0.0.0.0` (the bind-all address) to the actual hostname for display. This avoids showing a meaningless bind address to the operator.

## Implementation

### Files modified

| File | Change |
|------|--------|
| `models/node.py` | Added `agent_version: str = ""` to `HeartbeatPayload` and `NodeState` |
| `node/collector.py` | Imports `__version__`, includes it in heartbeat payload |
| `server/registry.py` | Stores `agent_version` from heartbeat into `NodeState` |
| `server/routes/dashboard.py` | Settings nav item, `GET/POST /dashboard/api/settings`, `_SETTINGS_BODY` HTML/CSS/JS |
| `tests/conftest.py` | `agent_version` param added to `make_heartbeat()` and `make_node()` |
| `tests/test_server/test_routes.py` | 7 new tests: page HTML, API config, toggle on/off, reject non-mutable, nodes list |
| `tests/test_server/test_registry.py` | 2 new tests: version stored, version defaults empty |

### API endpoints

**`GET /dashboard/api/settings`** returns:
```json
{
  "router_version": "0.1.0",
  "router_hostname": "Neons-Mac-Studio",
  "config": {
    "toggles": { "auto_pull": true, "vram_fallback": true },
    "server": { "host": "Neons-Mac-Studio", "port": 11435, "data_dir": "~/.fleet-manager", "max_retries": 2 },
    "heartbeat": { "heartbeat_interval": 5.0, "heartbeat_timeout": 15.0, "heartbeat_offline": 30.0 },
    "scoring": { "score_model_hot": 50.0, "...": "..." },
    "rebalancer": { "rebalance_interval": 5.0, "rebalance_threshold": 4, "rebalance_max_per_cycle": 3 },
    "pre_warm": { "pre_warm_threshold": 3, "pre_warm_min_availability": 0.6 },
    "auto_pull_config": { "auto_pull_timeout": 300.0 }
  },
  "nodes": [
    { "node_id": "Neons-Mac-Studio", "status": "online", "agent_version": "0.1.0",
      "ip": "http://localhost:11434", "models_loaded_count": 2, "is_router": true }
  ]
}
```

**`POST /dashboard/api/settings`** toggles mutable booleans:
```json
{"auto_pull": false}
```
Returns `{"status": "updated", "updated": {"auto_pull": false}}`.

### UI features

- **Toggle switches** with CSS slider animation — instant POST on click
- **Toast notification** on successful toggle ("Auto-Pull Models disabled" / "VRAM-Aware Fallback enabled")
- **Node cards** with green/yellow/red status dots, Router badge, version mismatch highlighting
- **Config tables** with setting name, current value, and `FLEET_*` env var name
- **Footer note**: "Configuration is set via environment variables with the FLEET_ prefix. Restart the router to apply changes."
- Auto-refreshes every 15 seconds
