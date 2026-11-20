#!/usr/bin/env bash
# End-to-end check of the container image on a Linux host with Ollama running:
# a router container and a node container on the host network find each other
# over mDNS, and one chat request routed through the router is answered by the
# node. Run by .github/workflows/image.yml before anything is pushed, and by
# hand on any Linux machine:
#
#     scripts/docker-smoke.sh <image> [model] [expected-version]
#
# Needs: docker, curl, jq, and Ollama on this host with <model> (default
# qwen2.5:0.5b) and nomic-embed-text pulled. Telemetry is off in both
# containers, so a test run never reaches ollamaherd.com/stats.
set -euo pipefail

IMAGE=${1:?usage: docker-smoke.sh <image> [model] [expected-version]}
MODEL=${2:-qwen2.5:0.5b}
EXPECT_VERSION=${3:-}
ROUTER=http://127.0.0.1:11435
NODE_ID=smoke-node

fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    for c in smoke-router smoke-node; do
      echo "----- docker logs $c -----" >&2
      docker logs "$c" 2>&1 | tail -60 >&2 || true
    done
  fi
  docker rm -f smoke-router smoke-node >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "== versions"
for cmd in herd herd-node; do
  out=$(docker run --rm "$IMAGE" "$cmd" --version)
  echo "$cmd: $out"
  if [ -n "$EXPECT_VERSION" ] && [ "$out" != "ollama-herd $EXPECT_VERSION" ]; then
    fail "$cmd reports '$out', expected 'ollama-herd $EXPECT_VERSION'"
  fi
done

echo "== runs as a non-root user"
uid=$(docker run --rm "$IMAGE" id -u)
[ "$uid" != "0" ] || fail "image runs as root"

curl -sf http://127.0.0.1:11434/api/version >/dev/null || fail "Ollama is not answering on 127.0.0.1:11434"

echo "== router and node containers, host network, discovery over mDNS"
docker run -d --name smoke-router --network host \
  -e FLEET_TELEMETRY=false -e FLEET_NODE_TELEMETRY=false "$IMAGE" >/dev/null
docker run -d --name smoke-node --network host \
  -e FLEET_NODE_TELEMETRY=false -e FLEET_NODE_NODE_ID="$NODE_ID" "$IMAGE" herd-node >/dev/null

online=0
for _ in $(seq 1 60); do
  online=$(curl -sf "$ROUTER/fleet/status" | jq -r '.fleet.nodes_online // 0' 2>/dev/null || echo 0)
  [ "$online" -ge 1 ] && break
  sleep 2
done
[ "$online" -ge 1 ] || fail "no node came online within 120 s"
curl -sf "$ROUTER/fleet/status" \
  | jq -r '.nodes[] | "\(.node_id) \(.status) \(.hardware.memory_total_gb) GB \(.ollama_url)"'
curl -sf "$ROUTER/fleet/status" | jq -e --arg id "$NODE_ID" \
  '.nodes[] | select(.node_id == $id) | .hardware.memory_total_gb > 0' >/dev/null \
  || fail "$NODE_ID is not registered with its memory"

echo "== one chat request through the router"
headers=$(mktemp)
body=$(mktemp)
code=$(curl -s -m 300 -o "$body" -D "$headers" -w '%{http_code}' \
  "$ROUTER/v1/chat/completions" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi.\"}]}")
served_by=$(grep -i '^x-fleet-node:' "$headers" | cut -d: -f2- | tr -d ' \r')
echo "HTTP $code, x-fleet-node: $served_by"
[ "$code" = "200" ] || { cat "$body" >&2; fail "chat request returned HTTP $code"; }
[ "$served_by" = "$NODE_ID" ] || fail "served by '$served_by', expected '$NODE_ID'"
jq -e '.choices[0].message.content | length > 0' "$body" >/dev/null || fail "empty completion"

# The image has no fastembed, so Ollama's nomic-embed-text must answer (until
# 2026-11 every one of these 503'd: "No node is running the native server").
echo "== one nomic-embed-text request through the router"
code=$(curl -s -m 120 -o "$body" -D "$headers" -w '%{http_code}' "$ROUTER/api/embed" \
  -H 'Content-Type: application/json' -d '{"model":"nomic-embed-text","input":"hello"}')
backend=$(grep -i '^x-fleet-backend:' "$headers" | cut -d: -f2- | tr -d ' \r')
echo "HTTP $code, x-fleet-backend: $backend"
[ "$code" = "200" ] || { cat "$body" >&2; fail "embed request returned HTTP $code"; }
[ "$backend" = "ollama" ] || fail "embed served by backend '$backend', expected 'ollama'"
jq -e '.embeddings[0] | length > 0' "$body" >/dev/null || fail "empty embedding"

echo "PASS"
