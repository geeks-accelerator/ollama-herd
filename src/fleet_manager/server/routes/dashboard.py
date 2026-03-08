"""Dashboard routes — fleet overview, trends, model insights, SSE stream, and data APIs."""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

router = APIRouter(tags=["dashboard"])

# ---------------------------------------------------------------------------
# Favicon — FontAwesome horse-saddle (sharp duotone) in brand accent purple
# ---------------------------------------------------------------------------

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 640">'
    '<path opacity=".4" fill="#6c63ff" d="M353.2 188.7L353.5 185.9'
    "C361.1 121.4 415.7 72.8 480.6 72.8L555 72.8L589.9 127.3L600.9 238.6"
    "L519.6 278.8L489.4 216.1L472.3 216.1L472.3 320.3L460.6 350.9L441.1"
    " 372.7L441.1 568L351.8 568L351.8 389.9L232 364.6L232 405.2C232 413.3"
    " 230.5 421.3 227.5 428.8L211.8 468.4L238.1 568.1L137.8 568.1L109.3"
    " 460.1L136.1 397.1L136.1 378L96.8 292.7L96.8 272.3C96.8 232.5 129"
    " 200.3 168.8 200.3L193.2 200.3L193.2 226.6C193.2 270.8 229 303.1"
    " 273.2 303.1C317.4 303.1 353.2 263.5 353.2 226.6L353.2 188.7z\"/>"
    '<path fill="#6c63ff" d="M360.5 200C360.5 133.7 414.2 80 480.5 80'
    "L548.2 80L582 130.8L592 235.3L524.1 269.3L495.7 212.5L493.5 208.1"
    "L464.6 208.1L464.6 304.1C464.4 329.5 453 351.7 435.4 366.6L432.6"
    " 369L432.6 560.2L360.6 560.2L360.6 383.4L354.3 382L280.6 365.8"
    "L280.6 311.8C325.5 307.8 360.6 270.1 360.6 224.2L360.6 200.2z"
    "M272.5 296C232.7 296 200.5 263.8 200.5 224L200.5 208L344.5 208"
    "L344.5 224C344.5 263.8 312.3 296 272.5 296zM264.5 311.6L264.5 362.1"
    "C240.9 356.9 227.5 354 224.5 353.3L224.5 416.1L221.3 423.6L203.9"
    " 464.1L202.8 466.7L203.6 469.4L228.7 559.9L145.7 559.9L122 474.6"
    "L118.2 461.1C119.5 458.2 128 438.2 143.9 401.2L144.5 399.7L144.5"
    " 376.6L143.8 375L110.5 301.7C106.6 293.1 104.6 283.7 104.6 274.3"
    "C104.6 237.7 134.3 208 170.9 208L184.6 208L184.6 224C184.6 269.9"
    " 219.7 307.6 264.6 311.6zM224.3 192L170.8 192C144.3 192 120.7 204.5"
    " 105.7 224L96.5 224C61.2 224 32.5 252.7 32.5 288L32.5 384L48.5 384"
    "L48.5 288C48.5 261.7 69.7 240.3 96 240C91.2 250.4 88.5 262.1 88.5"
    " 274.3C88.5 286 91 297.7 95.9 308.4L128.5 380.2L128.5 396.6C94.1"
    " 476.8 104.2 453.1 101.3 460L102.1 462.7L106.6 479L131.9 570.2"
    "L133.5 576.1L249.7 576.1L246.9 566L219.7 468.1C248.7 400.4 237.1"
    " 427.5 240.5 419.6L240.5 373.4L344.5 396.2L344.5 576.1L448.5 576.1"
    "L448.5 376.2C465.8 360.4 477.6 338.3 480 313.2L480.5 313.3L480.5"
    " 224.2L483.6 224.2L513.4 283.8L517 291L524.2 287.4L604.2 247.4"
    "L609.1 244.9L608.6 239.4L597.9 127.4L597.7 125.4L596.6 123.7"
    "L567.6 80.1L592.7 80.1L592.7 64.1L480.7 64.1C408.1 64 348.9 120.6"
    " 344.7 192L224.3 192zM544.5 144C544.5 135.2 537.3 128 528.5 128"
    "C519.7 128 512.5 135.2 512.5 144C512.5 152.8 519.7 160 528.5 160"
    'C537.3 160 544.5 152.8 544.5 144z"/>'
    "</svg>"
)


@router.get("/favicon.svg")
async def favicon_svg():
    """Serve the horse-saddle favicon as SVG."""
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


@router.get("/favicon.ico")
async def favicon_ico():
    """Redirect favicon.ico to SVG favicon."""
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# SSE event stream (shared by the fleet overview page)
# ---------------------------------------------------------------------------


@router.get("/dashboard/events")
async def dashboard_events(request: Request):
    """SSE endpoint for real-time fleet state updates."""

    async def event_stream():
        while True:
            registry = request.app.state.registry
            queue_mgr = request.app.state.queue_mgr

            nodes = []
            for node in registry.get_all_nodes():
                node_data = {
                    "node_id": node.node_id,
                    "status": node.status.value,
                    "hardware": {
                        "memory_total_gb": node.hardware.memory_total_gb,
                        "cores_physical": node.hardware.cores_physical,
                    },
                }
                if node.cpu:
                    node_data["cpu"] = {
                        "cores_physical": node.cpu.cores_physical,
                        "utilization_pct": node.cpu.utilization_pct,
                    }
                if node.memory:
                    node_data["memory"] = {
                        "total_gb": round(node.memory.total_gb, 1),
                        "used_gb": round(node.memory.used_gb, 1),
                        "available_gb": round(node.memory.available_gb, 1),
                        "pressure": node.memory.pressure.value,
                    }
                if node.ollama:
                    node_data["ollama"] = {
                        "models_loaded": [
                            {"name": m.name, "size_gb": round(m.size_gb, 2)}
                            for m in node.ollama.models_loaded
                        ],
                        "models_available_count": len(node.ollama.models_available),
                        "requests_active": node.ollama.requests_active,
                    }
                nodes.append(node_data)

            data = {
                "nodes": nodes,
                "queues": queue_mgr.get_queue_info(),
                "timestamp": time.time(),
            }

            yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# JSON data APIs for dashboard pages
# ---------------------------------------------------------------------------


@router.get("/dashboard/api/trends")
async def dashboard_trends_data(request: Request, hours: int = 72):
    """Hourly aggregated request counts and latencies for the trends chart."""
    latency_store = getattr(request.app.state, "latency_store", None)
    if not latency_store:
        return {"hours": hours, "data": []}
    data = await latency_store.get_hourly_trends(hours=hours)
    return {"hours": hours, "data": data}


@router.get("/dashboard/api/models")
async def dashboard_models_data(request: Request, days: int = 7):
    """Per-model daily aggregated stats for the model insights page."""
    latency_store = getattr(request.app.state, "latency_store", None)
    if not latency_store:
        return {"days": days, "daily": [], "summary": []}
    daily = await latency_store.get_model_daily_stats(days=days)
    summary = await latency_store.get_model_summary()
    return {"days": days, "daily": daily, "summary": summary}


@router.get("/dashboard/api/overview")
async def dashboard_overview_data(request: Request):
    """Quick summary stats for dashboard header cards."""
    latency_store = getattr(request.app.state, "latency_store", None)
    if not latency_store:
        return {
            "total_requests": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "models_count": 0,
        }
    summary = await latency_store.get_model_summary()
    total_requests = sum(m["total_requests"] for m in summary)
    total_prompt = sum(m["total_prompt_tokens"] for m in summary)
    total_completion = sum(m["total_completion_tokens"] for m in summary)
    return {
        "total_requests": total_requests,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "models_count": len(summary),
    }


@router.get("/dashboard/api/usage")
async def dashboard_usage_data(request: Request, days: int = 7):
    """Per-node, per-model, per-day usage stats from request traces."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"days": days, "data": []}
    data = await trace_store.get_usage_by_node_model_day(days=days)
    return {"days": days, "data": data}


@router.get("/dashboard/api/traces")
async def dashboard_traces(request: Request, limit: int = 50):
    """Recent request traces for debugging and observability."""
    trace_store = getattr(request.app.state, "trace_store", None)
    if not trace_store:
        return {"traces": []}
    traces = await trace_store.get_recent_traces(limit=limit)
    return {"traces": traces}


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page():
    """Fleet overview — live node and queue state."""
    return _dashboard_page("Fleet Overview", "overview", _OVERVIEW_BODY)


@router.get("/dashboard/trends", response_class=HTMLResponse)
async def dashboard_trends_page():
    """Historical trends — requests, latency, and token throughput over time."""
    return _dashboard_page(
        "Trends",
        "trends",
        _TRENDS_BODY,
        extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>',
    )


@router.get("/dashboard/models", response_class=HTMLResponse)
async def dashboard_models_page():
    """Model insights — per-model performance and token usage."""
    return _dashboard_page(
        "Model Insights",
        "models",
        _MODELS_BODY,
        extra_head='<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>',
    )


# ---------------------------------------------------------------------------
# Shared layout helper
# ---------------------------------------------------------------------------

_SHARED_CSS = """
:root {
  --bg: #0a0a0f;
  --card: #12121a;
  --border: #1e1e2e;
  --text: #e0e0e8;
  --text-dim: #8888a0;
  --accent: #6c63ff;
  --green: #22c55e;
  --yellow: #eab308;
  --red: #ef4444;
  --orange: #f97316;
  --blue: #3b82f6;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
.header {
  padding: 16px 32px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header h1 {
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.3px;
  margin-right: 24px;
}
.header-left {
  display: flex;
  align-items: center;
}
.nav-tabs {
  display: flex;
  gap: 4px;
}
.nav-tab {
  padding: 6px 16px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-dim);
  text-decoration: none;
  transition: all 0.15s;
}
.nav-tab:hover {
  color: var(--text);
  background: rgba(108,99,255,0.1);
}
.nav-tab.active {
  color: var(--accent);
  background: rgba(108,99,255,0.15);
}
.header-stats {
  display: flex;
  gap: 24px;
}
.header-stat {
  text-align: center;
}
.header-stat .value {
  font-size: 22px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.header-stat .label {
  font-size: 11px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.main {
  padding: 24px 32px;
  display: flex;
  flex-direction: column;
  gap: 24px;
  flex: 1;
}
.section-title {
  font-size: 13px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.8px;
  color: var(--text-dim);
  margin-bottom: 12px;
}
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
}
.status-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  position: relative;
  top: -1px;
}
.status-dot.online { background: var(--green); box-shadow: 0 0 8px var(--green); }
.status-dot.degraded { background: var(--yellow); box-shadow: 0 0 8px var(--yellow); }
.status-dot.offline { background: var(--red); }
.badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 500;
}
.badge.online { background: rgba(34,197,94,0.15); color: var(--green); }
.badge.degraded { background: rgba(234,179,8,0.15); color: var(--yellow); }
.badge.offline { background: rgba(239,68,68,0.15); color: var(--red); }
.empty-state {
  color: var(--text-dim);
  font-size: 14px;
  text-align: center;
  padding: 40px;
}
.pulse { animation: pulse 2s infinite; }
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}
.footer {
  padding: 16px 32px;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  color: var(--text-dim);
  font-size: 12px;
}
.connected-indicator {
  display: flex;
  align-items: center;
  gap: 6px;
}
/* Summary cards row */
.summary-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
.summary-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 20px;
  text-align: center;
}
.summary-card .sc-value {
  font-size: 28px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.summary-card .sc-label {
  font-size: 11px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-top: 4px;
}
/* Time range buttons */
.time-range {
  display: flex;
  gap: 4px;
  margin-bottom: 16px;
}
.time-btn {
  padding: 5px 14px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  background: var(--card);
  border: 1px solid var(--border);
  color: var(--text-dim);
  cursor: pointer;
  transition: all 0.15s;
}
.time-btn:hover { color: var(--text); border-color: var(--accent); }
.time-btn.active { color: var(--accent); background: rgba(108,99,255,0.15); border-color: var(--accent); }
/* Charts grid */
.charts-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.chart-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
}
.chart-card.full-width {
  grid-column: 1 / -1;
}
.chart-card h4 {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 16px;
}
/* Model insights table */
.model-table {
  width: 100%;
  border-collapse: collapse;
}
.model-table th {
  font-size: 11px;
  color: var(--text-dim);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  text-align: left;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-weight: 600;
}
.model-table td {
  padding: 10px 12px;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  border-bottom: 1px solid rgba(30,30,46,0.5);
}
.model-table tr { cursor: pointer; transition: background 0.15s; }
.model-table tr:hover { background: rgba(108,99,255,0.05); }
.model-table tr.selected { background: rgba(108,99,255,0.1); }
.model-table .model-name {
  font-weight: 600;
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 12px;
}
"""


def _dashboard_page(title: str, active_tab: str, body_html: str, extra_head: str = "") -> str:
    """Generate a full dashboard HTML page with shared nav, styles, and footer."""
    nav_items = [
        ("overview", "Fleet Overview", "/dashboard"),
        ("trends", "Trends", "/dashboard/trends"),
        ("models", "Model Insights", "/dashboard/models"),
    ]
    nav_html = "".join(
        f'<a href="{href}" class="nav-tab {"active" if key == active_tab else ""}">{label}</a>'
        for key, label, href in nav_items
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} - Ollama Herd</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>{_SHARED_CSS}</style>
{extra_head}
</head>
<body>
<div class="header">
  <div class="header-left">
    <h1>Ollama Herd</h1>
    <nav class="nav-tabs">{nav_html}</nav>
  </div>
  <div class="header-stats" id="header-stats"></div>
</div>
{body_html}
<div class="footer">
  <div>Ollama Herd v0.1.0</div>
  <div class="connected-indicator">
    <span class="status-dot online pulse" id="sse-dot"></span>
    <span id="sse-status">Connected</span>
  </div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Fleet Overview page body
# ---------------------------------------------------------------------------

_OVERVIEW_BODY = """
<style>
.nodes-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 16px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.card-header h3 { font-size: 15px; font-weight: 600; }
.metrics-row {
  display: flex;
  gap: 16px;
  margin-bottom: 14px;
}
.metric { flex: 1; }
.metric .label { font-size: 11px; color: var(--text-dim); margin-bottom: 4px; }
.metric .value { font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; }
.bar-container { height: 6px; background: var(--border); border-radius: 3px; margin-top: 6px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 3px; transition: width 0.8s ease; }
.bar-fill.cpu { background: var(--blue); }
.bar-fill.mem { background: var(--accent); }
.bar-fill.mem.warn { background: var(--yellow); }
.bar-fill.mem.critical { background: var(--red); }
.models-list { margin-top: 12px; }
.model-chip {
  display: inline-flex; align-items: center; gap: 4px;
  background: rgba(108,99,255,0.1); border: 1px solid rgba(108,99,255,0.2);
  border-radius: 6px; padding: 3px 10px; font-size: 12px;
  margin: 2px 4px 2px 0; font-variant-numeric: tabular-nums;
}
.model-chip .size { color: var(--text-dim); font-size: 11px; }
.queues-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}
.queue-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 18px; display: flex; align-items: center; justify-content: space-between;
}
.queue-name { font-size: 13px; font-weight: 500; font-family: 'SF Mono', 'Fira Code', monospace; }
.queue-stats { display: flex; gap: 14px; align-items: center; }
.queue-stat { text-align: center; }
.queue-stat .num { font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; }
.queue-stat .lbl { font-size: 10px; color: var(--text-dim); text-transform: uppercase; }
</style>

<div class="main">
  <div>
    <div class="section-title">Fleet Nodes</div>
    <div class="nodes-grid" id="nodes-container">
      <div class="empty-state">Waiting for nodes...</div>
    </div>
  </div>
  <div>
    <div class="section-title">Request Queues</div>
    <div class="queues-grid" id="queues-container">
      <div class="empty-state">No active queues</div>
    </div>
  </div>
</div>

<script>
function formatGB(gb) {
  if (gb >= 100) return Math.round(gb) + ' GB';
  if (gb >= 10) return gb.toFixed(1) + ' GB';
  return gb.toFixed(2) + ' GB';
}

function renderNodes(nodes) {
  const container = document.getElementById('nodes-container');
  if (!nodes.length) {
    container.innerHTML = '<div class="empty-state">No nodes connected</div>';
    return;
  }
  let totalModels = 0, onlineCount = 0;
  container.innerHTML = nodes.map(node => {
    const status = node.status;
    if (status === 'online') onlineCount++;
    const cpu = node.cpu ? node.cpu.utilization_pct : 0;
    const memUsed = node.memory ? node.memory.used_gb : 0;
    const memTotal = node.memory ? node.memory.total_gb : node.hardware.memory_total_gb;
    const memPct = memTotal > 0 ? (memUsed / memTotal) * 100 : 0;
    const pressure = node.memory ? node.memory.pressure : 'normal';
    const models = node.ollama ? node.ollama.models_loaded : [];
    totalModels += models.length;
    const availCount = node.ollama ? node.ollama.models_available_count : 0;
    const modelsHtml = models.length > 0
      ? models.map(m => `<span class="model-chip">${m.name} <span class="size">${formatGB(m.size_gb)}</span></span>`).join('')
      : '<span style="color:var(--text-dim);font-size:12px">No models loaded</span>';
    return `
      <div class="card">
        <div class="card-header">
          <h3><span class="status-dot ${status}"></span>${node.node_id}</h3>
          <span class="badge ${status}">${status}</span>
        </div>
        <div class="metrics-row">
          <div class="metric">
            <div class="label">CPU</div>
            <div class="value">${cpu.toFixed(1)}%</div>
            <div class="bar-container"><div class="bar-fill cpu" style="width:${cpu}%"></div></div>
          </div>
          <div class="metric">
            <div class="label">Memory (${pressure})</div>
            <div class="value">${formatGB(memUsed)} / ${formatGB(memTotal)}</div>
            <div class="bar-container"><div class="bar-fill mem ${pressure}" style="width:${memPct}%"></div></div>
          </div>
          <div class="metric">
            <div class="label">Cores</div>
            <div class="value">${node.hardware.cores_physical}</div>
          </div>
        </div>
        <div class="models-list">
          <div class="label" style="font-size:11px;color:var(--text-dim);margin-bottom:6px">
            Models (${models.length} loaded, ${availCount} on disk)
          </div>
          ${modelsHtml}
        </div>
      </div>`;
  }).join('');
  document.getElementById('header-stats').innerHTML = `
    <div class="header-stat"><div class="value">${onlineCount}</div><div class="label">Nodes</div></div>
    <div class="header-stat"><div class="value">${totalModels}</div><div class="label">Models Loaded</div></div>
  `;
}

function renderQueues(queues) {
  const container = document.getElementById('queues-container');
  const entries = Object.entries(queues);
  if (!entries.length) {
    container.innerHTML = '<div class="empty-state">No active queues</div>';
    return;
  }
  let totalQueued = 0, totalCompleted = 0;
  container.innerHTML = entries.map(([key, q]) => {
    totalQueued += q.pending + q.in_flight;
    totalCompleted += q.completed;
    const pendingColor = q.pending > 3 ? 'var(--orange)' : q.pending > 0 ? 'var(--yellow)' : 'var(--text-dim)';
    const inflightColor = q.in_flight > 0 ? 'var(--blue)' : 'var(--text-dim)';
    return `
      <div class="queue-card">
        <div class="queue-name">${key}</div>
        <div class="queue-stats">
          <div class="queue-stat"><div class="num" style="color:${pendingColor}">${q.pending}</div><div class="lbl">Pending</div></div>
          <div class="queue-stat"><div class="num" style="color:${inflightColor}">${q.in_flight}</div><div class="lbl">In-Flight</div></div>
          <div class="queue-stat"><div class="num" style="color:var(--green)">${q.completed}</div><div class="lbl">Done</div></div>
          <div class="queue-stat"><div class="num" style="color:var(--red)">${q.failed || 0}</div><div class="lbl">Failed</div></div>
        </div>
      </div>`;
  }).join('');
  const stats = document.getElementById('header-stats');
  const existing = stats.innerHTML;
  if (!existing.includes('Queued')) {
    stats.innerHTML += `
      <div class="header-stat"><div class="value">${totalQueued}</div><div class="label">Queued</div></div>
      <div class="header-stat"><div class="value">${totalCompleted}</div><div class="label">Completed</div></div>
    `;
  }
}

function connect() {
  const dot = document.getElementById('sse-dot');
  const statusEl = document.getElementById('sse-status');
  const es = new EventSource('/dashboard/events');
  es.onopen = () => { dot.className = 'status-dot online pulse'; statusEl.textContent = 'Live'; };
  es.onmessage = (e) => {
    try { const data = JSON.parse(e.data); renderNodes(data.nodes); renderQueues(data.queues); }
    catch (err) { console.error('Parse error:', err); }
  };
  es.onerror = () => { dot.className = 'status-dot offline'; statusEl.textContent = 'Reconnecting...'; es.close(); setTimeout(connect, 3000); };
}
connect();
</script>
"""

# ---------------------------------------------------------------------------
# Historical Trends page body
# ---------------------------------------------------------------------------

_TRENDS_BODY = """
<div class="main">
  <div class="summary-cards" id="summary-cards"></div>

  <div>
    <div class="time-range" id="time-range">
      <button class="time-btn" data-hours="24">24h</button>
      <button class="time-btn" data-hours="48">48h</button>
      <button class="time-btn active" data-hours="72">72h</button>
      <button class="time-btn" data-hours="168">7d</button>
    </div>

    <div class="charts-row">
      <div class="chart-card">
        <h4>Requests per Hour</h4>
        <canvas id="requests-chart"></canvas>
      </div>
      <div class="chart-card">
        <h4>Average Latency per Hour</h4>
        <canvas id="latency-chart"></canvas>
      </div>
    </div>
    <div class="charts-row" style="margin-top:16px">
      <div class="chart-card full-width">
        <h4>Token Throughput per Hour</h4>
        <canvas id="tokens-chart"></canvas>
      </div>
    </div>
  </div>
</div>

<script>
const cs = getComputedStyle(document.documentElement);
const C = {
  accent: cs.getPropertyValue('--accent').trim(),
  blue: cs.getPropertyValue('--blue').trim(),
  green: cs.getPropertyValue('--green').trim(),
  border: cs.getPropertyValue('--border').trim(),
  textDim: cs.getPropertyValue('--text-dim').trim(),
  text: cs.getPropertyValue('--text').trim(),
  card: cs.getPropertyValue('--card').trim(),
};
const chartDefaults = {
  responsive: true,
  maintainAspectRatio: true,
  plugins: { legend: { labels: { color: C.textDim, font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: C.textDim, font: { size: 10 }, maxRotation: 45 }, grid: { color: C.border } },
    y: { ticks: { color: C.textDim, font: { size: 10 } }, grid: { color: C.border }, beginAtZero: true },
  },
};

let requestsChart, latencyChart, tokensChart;

function fmtNum(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toString();
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString([], {month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
}

async function loadTrends(hours) {
  const [trendsResp, overviewResp] = await Promise.all([
    fetch('/dashboard/api/trends?hours=' + hours),
    fetch('/dashboard/api/overview'),
  ]);
  const trends = await trendsResp.json();
  const overview = await overviewResp.json();

  // Summary cards
  const avgLat = trends.data.length > 0
    ? Math.round(trends.data.reduce((s,d) => s + d.avg_latency_ms, 0) / trends.data.length)
    : 0;
  document.getElementById('summary-cards').innerHTML = `
    <div class="summary-card"><div class="sc-value">${fmtNum(overview.total_requests)}</div><div class="sc-label">Total Requests</div></div>
    <div class="summary-card"><div class="sc-value">${avgLat > 0 ? (avgLat/1000).toFixed(1)+'s' : '-'}</div><div class="sc-label">Avg Latency</div></div>
    <div class="summary-card"><div class="sc-value">${fmtNum(overview.total_prompt_tokens)}</div><div class="sc-label">Tokens In</div></div>
    <div class="summary-card"><div class="sc-value">${fmtNum(overview.total_completion_tokens)}</div><div class="sc-label">Tokens Out</div></div>
  `;

  const labels = trends.data.map(d => fmtTime(d.hour_bucket));
  const requests = trends.data.map(d => d.request_count);
  const latencies = trends.data.map(d => d.avg_latency_ms / 1000);
  const promptTok = trends.data.map(d => d.total_prompt_tokens);
  const completionTok = trends.data.map(d => d.total_completion_tokens);

  // Destroy old charts
  if (requestsChart) requestsChart.destroy();
  if (latencyChart) latencyChart.destroy();
  if (tokensChart) tokensChart.destroy();

  requestsChart = new Chart(document.getElementById('requests-chart'), {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Requests', data: requests, backgroundColor: C.accent + '99', borderColor: C.accent, borderWidth: 1 }] },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  latencyChart = new Chart(document.getElementById('latency-chart'), {
    type: 'line',
    data: { labels, datasets: [{ label: 'Avg Latency (s)', data: latencies, borderColor: C.blue, backgroundColor: C.blue + '22', fill: true, tension: 0.3, pointRadius: 2 }] },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  tokensChart = new Chart(document.getElementById('tokens-chart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Prompt Tokens', data: promptTok, backgroundColor: C.blue + '99' },
        { label: 'Completion Tokens', data: completionTok, backgroundColor: C.green + '99' },
      ],
    },
    options: {
      ...chartDefaults,
      scales: { ...chartDefaults.scales, x: { ...chartDefaults.scales.x, stacked: true }, y: { ...chartDefaults.scales.y, stacked: true } },
    },
  });
}

// Time range buttons
document.getElementById('time-range').addEventListener('click', (e) => {
  if (!e.target.classList.contains('time-btn')) return;
  document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
  e.target.classList.add('active');
  loadTrends(parseInt(e.target.dataset.hours));
});

// Connection status (no SSE on this page, just static)
// Footer may not be parsed yet; defer until DOM ready.
window.addEventListener('DOMContentLoaded', () => {
  const dot = document.getElementById('sse-dot');
  const st = document.getElementById('sse-status');
  if (dot) dot.className = 'status-dot online';
  if (st) st.textContent = 'API';
});

loadTrends(72);
</script>
"""

# ---------------------------------------------------------------------------
# Model Insights page body
# ---------------------------------------------------------------------------

_MODELS_BODY = """
<div class="main">
  <div class="summary-cards" id="model-summary-cards"></div>

  <div class="charts-row">
    <div class="chart-card">
      <h4>Avg Latency by Model</h4>
      <canvas id="comparison-chart"></canvas>
    </div>
    <div class="chart-card">
      <h4>Token Distribution</h4>
      <canvas id="token-dist-chart"></canvas>
    </div>
  </div>

  <div class="card" style="margin-top:16px;padding:0;overflow:hidden">
    <table class="model-table" id="model-table">
      <thead>
        <tr>
          <th>Model</th>
          <th>Requests</th>
          <th>Avg Latency</th>
          <th>Tokens/sec</th>
          <th>Prompt Tokens</th>
          <th>Completion Tokens</th>
          <th>Last Seen</th>
        </tr>
      </thead>
      <tbody id="model-tbody"></tbody>
    </table>
  </div>

  <div class="charts-row" style="margin-top:16px" id="daily-section" hidden>
    <div class="chart-card">
      <h4 id="daily-title">Daily Requests</h4>
      <canvas id="daily-requests-chart"></canvas>
    </div>
    <div class="chart-card">
      <h4>Daily Avg Latency</h4>
      <canvas id="daily-latency-chart"></canvas>
    </div>
  </div>
</div>

<script>
const cs = getComputedStyle(document.documentElement);
const C = {
  accent: cs.getPropertyValue('--accent').trim(),
  blue: cs.getPropertyValue('--blue').trim(),
  green: cs.getPropertyValue('--green').trim(),
  orange: cs.getPropertyValue('--orange').trim(),
  red: cs.getPropertyValue('--red').trim(),
  border: cs.getPropertyValue('--border').trim(),
  textDim: cs.getPropertyValue('--text-dim').trim(),
  text: cs.getPropertyValue('--text').trim(),
};
const chartDefaults = {
  responsive: true,
  maintainAspectRatio: true,
  plugins: { legend: { labels: { color: C.textDim, font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: C.textDim, font: { size: 10 } }, grid: { color: C.border } },
    y: { ticks: { color: C.textDim, font: { size: 10 } }, grid: { color: C.border }, beginAtZero: true },
  },
};
const MODEL_COLORS = [C.accent, C.blue, C.green, C.orange, C.red, '#a855f7', '#ec4899', '#14b8a6', '#f59e0b', '#8b5cf6'];

let comparisonChart, tokenDistChart, dailyReqChart, dailyLatChart;
let allDaily = [];

function fmtNum(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toString();
}

function timeAgo(ts) {
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

function fmtDay(ts) {
  return new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

async function loadModels() {
  const resp = await fetch('/dashboard/api/models?days=7');
  const data = await resp.json();
  const summary = data.summary;
  allDaily = data.daily;

  // Summary cards
  const totalReqs = summary.reduce((s, m) => s + m.total_requests, 0);
  const totalPrompt = summary.reduce((s, m) => s + m.total_prompt_tokens, 0);
  const totalCompletion = summary.reduce((s, m) => s + m.total_completion_tokens, 0);
  const avgLat = summary.length > 0
    ? Math.round(summary.reduce((s, m) => s + m.avg_latency_ms * m.total_requests, 0) / (totalReqs || 1))
    : 0;

  document.getElementById('model-summary-cards').innerHTML = `
    <div class="summary-card"><div class="sc-value">${summary.length}</div><div class="sc-label">Models</div></div>
    <div class="summary-card"><div class="sc-value">${fmtNum(totalReqs)}</div><div class="sc-label">Total Requests</div></div>
    <div class="summary-card"><div class="sc-value">${fmtNum(totalPrompt + totalCompletion)}</div><div class="sc-label">Total Tokens</div></div>
    <div class="summary-card"><div class="sc-value">${avgLat > 0 ? (avgLat/1000).toFixed(1)+'s' : '-'}</div><div class="sc-label">Avg Latency</div></div>
  `;

  // Table
  const tbody = document.getElementById('model-tbody');
  tbody.innerHTML = summary.map((m, i) => {
    const tokPerSec = m.avg_latency_ms > 0 && m.total_completion_tokens > 0
      ? ((m.total_completion_tokens / m.total_requests) / (m.avg_latency_ms / 1000)).toFixed(1)
      : '-';
    return `<tr data-model="${m.model_name}" onclick="selectModel('${m.model_name}', this)">
      <td class="model-name">${m.model_name}</td>
      <td>${fmtNum(m.total_requests)}</td>
      <td>${(m.avg_latency_ms / 1000).toFixed(1)}s</td>
      <td>${tokPerSec}</td>
      <td>${fmtNum(m.total_prompt_tokens)}</td>
      <td>${fmtNum(m.total_completion_tokens)}</td>
      <td style="color:${C.textDim}">${m.last_seen ? timeAgo(m.last_seen) : '-'}</td>
    </tr>`;
  }).join('');

  // Comparison chart
  if (comparisonChart) comparisonChart.destroy();
  comparisonChart = new Chart(document.getElementById('comparison-chart'), {
    type: 'bar',
    data: {
      labels: summary.map(m => m.model_name),
      datasets: [{
        label: 'Avg Latency (s)',
        data: summary.map(m => m.avg_latency_ms / 1000),
        backgroundColor: summary.map((_, i) => MODEL_COLORS[i % MODEL_COLORS.length] + '99'),
        borderColor: summary.map((_, i) => MODEL_COLORS[i % MODEL_COLORS.length]),
        borderWidth: 1,
      }],
    },
    options: { ...chartDefaults, indexAxis: 'y', plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  // Token distribution
  if (tokenDistChart) tokenDistChart.destroy();
  const totalTok = summary.map(m => m.total_prompt_tokens + m.total_completion_tokens);
  tokenDistChart = new Chart(document.getElementById('token-dist-chart'), {
    type: 'doughnut',
    data: {
      labels: summary.map(m => m.model_name),
      datasets: [{
        data: totalTok,
        backgroundColor: summary.map((_, i) => MODEL_COLORS[i % MODEL_COLORS.length] + 'cc'),
        borderColor: 'transparent',
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { position: 'right', labels: { color: C.textDim, font: { size: 11 }, padding: 12 } },
      },
    },
  });

  // Auto-select first model
  if (summary.length > 0) {
    const firstRow = tbody.querySelector('tr');
    if (firstRow) selectModel(summary[0].model_name, firstRow);
  }
}

function selectModel(modelName, row) {
  document.querySelectorAll('.model-table tr.selected').forEach(r => r.classList.remove('selected'));
  if (row) row.classList.add('selected');

  const filtered = allDaily.filter(d => d.model_name === modelName);
  if (filtered.length === 0) {
    document.getElementById('daily-section').hidden = true;
    return;
  }

  document.getElementById('daily-section').hidden = false;
  document.getElementById('daily-title').textContent = 'Daily Requests — ' + modelName;

  const labels = filtered.map(d => fmtDay(d.day_bucket));
  const requests = filtered.map(d => d.request_count);
  const latencies = filtered.map(d => d.avg_latency_ms / 1000);

  if (dailyReqChart) dailyReqChart.destroy();
  if (dailyLatChart) dailyLatChart.destroy();

  dailyReqChart = new Chart(document.getElementById('daily-requests-chart'), {
    type: 'bar',
    data: { labels, datasets: [{ label: 'Requests', data: requests, backgroundColor: C.accent + '99', borderColor: C.accent, borderWidth: 1 }] },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });

  dailyLatChart = new Chart(document.getElementById('daily-latency-chart'), {
    type: 'line',
    data: { labels, datasets: [{ label: 'Avg Latency (s)', data: latencies, borderColor: C.blue, backgroundColor: C.blue + '22', fill: true, tension: 0.3, pointRadius: 3 }] },
    options: { ...chartDefaults, plugins: { ...chartDefaults.plugins, legend: { display: false } } },
  });
}

window.addEventListener('DOMContentLoaded', () => {
  const dot = document.getElementById('sse-dot');
  const st = document.getElementById('sse-status');
  if (dot) dot.className = 'status-dot online';
  if (st) st.textContent = 'API';
});

loadModels();
</script>
"""
