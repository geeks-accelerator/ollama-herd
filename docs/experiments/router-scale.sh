#!/bin/zsh
# Router control-plane scale (results: docs/experiments/router_scale_results.json).
# A router (HERD_BIN, default `herd`) on 11835, one mock Ollama on 11801, N fake herd-nodes, and 50 req/s of open-loop load
# through the router while they heartbeat. Per N: heartbeat POST latency,
# request latency added by the router, route_ms from traces, /fleet/status
# latency and size, router CPU and RSS.
set -u
# Usage: docs/experiments/router-scale.sh OUTDIR [HERD_BIN]
# Uses ports 11835 (router) and 11801 (mock); stops everything it starts.
D=${1:?output directory}
HERD=${2:-herd}
mkdir -p $D
cd ${0:A:h}/../..
: > $D/empty.env
uv run python scripts/mock_ollama.py --port 11801 --workers 4 --delay-ms 20 > $D/mock.log 2>&1 &
MOCK=$!
sleep 4
for N in 1 2 4 8 14 32 64 256; do
  rm -rf $D/router-$N; mkdir -p $D/router-$N
  FLEET_PORT=11835 FLEET_DATA_DIR=$D/router-$N FLEET_ENV_FILE=$D/empty.env \
    FLEET_TELEMETRY=false FLEET_NODE_TELEMETRY=false \
    $HERD > $D/router-$N.log 2>&1 &
  ROUTER=$!
  sleep 5
  uv run python scripts/fake_fleet.py --router http://127.0.0.1:11835 --nodes $N \
    --ollama http://127.0.0.1:11801 --num-parallel 64 --duration 75 --status-every 30 \
    --output $D/fleet-$N.json > $D/fleet-$N.log 2>&1 &
  FLEET=$!
  sleep 20
  uv run python scripts/router_overhead.py --ollama http://127.0.0.1:11801 \
    --router http://127.0.0.1:11835 --model mock:latest --rate 50 --seconds 20 \
    --output $D/load-$N.json > $D/load-$N.log 2>&1
  python3 - $N $ROUTER $D <<'EOF'
import json, subprocess, sys, time, urllib.request
n, pid, d = sys.argv[1], sys.argv[2], sys.argv[3]
base = "http://127.0.0.1:11835"
lat = []
for _ in range(10):
    t = time.perf_counter()
    body = urllib.request.urlopen(base + "/fleet/status").read()
    lat.append((time.perf_counter() - t) * 1000)
st = json.loads(body)
traces = json.load(urllib.request.urlopen(base + "/dashboard/api/traces?limit=2000"))
rows = traces if isinstance(traces, list) else traces.get("traces", [])
route = sorted(
    (r.get("timings") or {}).get("route_ms") for r in rows if (r.get("timings") or {}).get("route_ms") is not None
)
def pct(v, p):
    if not v: return None
    k = (len(v) - 1) * p / 100; lo = int(k); hi = min(lo + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 2)
ps = subprocess.run(["ps", "-o", "%cpu=,rss=", "-p", pid], capture_output=True, text=True).stdout.split()
out = {
    "nodes": int(n),
    "nodes_seen": len(st.get("nodes", [])),
    "fleet_status_bytes": len(body),
    "fleet_status_ms_median": round(sorted(lat)[len(lat) // 2], 1),
    "route_ms_p50": pct(route, 50), "route_ms_p99": pct(route, 99), "route_samples": len(route),
    "router_cpu_pct": float(ps[0]) if ps else None,
    "router_rss_mb": round(int(ps[1]) / 1024) if len(ps) > 1 else None,
}
json.dump(out, open(f"{d}/router-{n}.json", "w"), indent=2)
print(json.dumps(out))
EOF
  wait $FLEET
  kill $ROUTER; wait $ROUTER 2>/dev/null
  sleep 2
done
kill $MOCK
echo done
