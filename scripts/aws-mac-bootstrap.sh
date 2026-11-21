#!/bin/bash
# Bring an EC2 Mac host up as an ollama-herd node, with no hands.
#
# Written for run 6 (docs/plans/aws-mac-fleet-benchmark.md).  Every Mac in that
# fleet runs this once; the router runs on a separate Linux instance (r1).
#
# What it does, in order:
#   1. Grows the root APFS container to the EBS volume's size (models live
#      there; see the note in step 1 for why not on a second disk).
#   2. Installs Ollama from the GitHub release zip at a pinned version.  Not
#      Homebrew: its formula is stale (CLAUDE.md, Ollama notes).
#   3. Syncs Ollama's model store (and optionally a Hugging Face cache for MLX)
#      from S3, so six hosts do not each pull ~400 GB from the Ollama registry.
#   4. Installs ollama-herd from PyPI at the released version (never main), and
#      mlx-lm at herd's pinned version when MLX_SERVERS is set.
#   5. Runs `ollama serve` and `herd-node` as LaunchDaemons owned by ec2-user,
#      so they start without anyone logging in and restart on a crash.
#   6. Waits until the router lists this node as online, and writes
#      /var/db/herd-bootstrap.done.  It exits non-zero on any failure.
#
# Run it as root, either as EC2 user data (fill in the CONFIG block below; user
# data must stay under 16 KB) or over SSH:
#
#   sudo NODE_ID=x1 ROUTER_URL=http://10.0.1.10:11435 HERD_VERSION=0.12.13 \
#        MODELS_S3=s3://herd-run6-models/ollama bash aws-mac-bootstrap.sh
#
# Re-running is safe: every step checks what is already there.
# Log: /var/log/herd-bootstrap.log.
#
# Network: mDNS does not cross a VPC (no multicast), so the node is pointed at
# the router explicitly.  The security group must let r1 reach every Mac on
# 11434 (Ollama), 11438 (vision embeddings), 11439 (text embeddings) and, on an
# MLX node, the MLX_SERVERS ports.  Ollama binds 0.0.0.0 for that reason.

set -euo pipefail

# ---- CONFIG (environment wins; edit the defaults for user data) -------------
NODE_ID="${NODE_ID:-}"                    # short name: u1, x1, x2, p1, p2, s1
ROUTER_URL="${ROUTER_URL:-}"              # http://<r1 private IP>:11435
HERD_VERSION="${HERD_VERSION:-}"          # released version on PyPI, e.g. 0.12.13
OLLAMA_VERSION="${OLLAMA_VERSION:-0.40.13}"
OLLAMA_ZIP_SHA256="${OLLAMA_ZIP_SHA256:-}"   # optional; checked when set
MODELS_S3="${MODELS_S3:-}"                # s3://bucket/prefix holding blobs/ and manifests/
HF_S3="${HF_S3:-}"                        # optional s3:// Hugging Face hub cache, for MLX
MLX_SERVERS="${MLX_SERVERS:-}"            # FLEET_NODE_MLX_SERVERS JSON, empty = no MLX
OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}"
READY_TIMEOUT="${READY_TIMEOUT:-900}"     # seconds to wait for the router to list the node
# ------------------------------------------------------------------------------

USER_NAME="ec2-user"
USER_HOME="/Users/$USER_NAME"
LOG=/var/log/herd-bootstrap.log
DONE=/var/db/herd-bootstrap.done
OLLAMA_LABEL="com.geeksaccelerator.ollama-herd.bench.ollama"
NODE_LABEL="com.geeksaccelerator.ollama-herd.bench.node"
MLX_LM_VERSION="0.32.2"   # keep in step with scripts/setup-mlx.sh
USER_PATH="$USER_HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
exec > >(tee -a "$LOG") 2>&1
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
die() { log "FAILED: $*"; exit 1; }
as_user() { sudo -u "$USER_NAME" -H env PATH="$USER_PATH" "$@"; }

[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || die "needs macOS on Apple Silicon"
[[ -n "$NODE_ID" ]] || die "NODE_ID is not set"
[[ -n "$ROUTER_URL" ]] || die "ROUTER_URL is not set (mDNS does not cross a VPC)"
[[ -z "${MODELS_DISK:-}" ]] || die "MODELS_DISK is no longer supported: a LaunchDaemon cannot write to an external volume (TCC); size the root EBS volume instead"
[[ -n "$HERD_VERSION" ]] || die "HERD_VERSION is not set (run a release from PyPI, not main)"
id "$USER_NAME" >/dev/null 2>&1 || die "user $USER_NAME does not exist (not an EC2 macOS AMI?)"
rm -f "$DONE"
log "bootstrap $NODE_ID: herd $HERD_VERSION, ollama $OLLAMA_VERSION, router $ROUTER_URL"
log "host: $(sysctl -n machdep.cpu.brand_string), $(( $(sysctl -n hw.memsize) / 1073741824 )) GB, macOS $(sw_vers -productVersion)"

# ---- 1. Disk -----------------------------------------------------------------
root_store="$(diskutil info / | awk -F': *' '/APFS Physical Store/{print $2; exit}')"
root_disk="${root_store%s*}"
if [[ -n "$root_store" ]]; then
    # AWS's documented step for a root EBS volume larger than the AMI.
    # Harmless when the container already fills the disk.
    yes | diskutil repairDisk "$root_disk" >/dev/null 2>&1 || true
    diskutil apfs resizeContainer "$root_store" 0 >/dev/null 2>&1 || true
fi

# Models live on the root volume.  A dedicated disk (instance store or a second
# EBS volume) cannot be used: macOS mounts it under /Volumes as an external
# volume, and TCC refuses a LaunchDaemon access to it ("mkdir ...: operation not
# permitted"), measured on mac-m4.metal, macOS 26.7.3, 2026-11-21.  Size the
# root EBS volume for the models instead.
MODELS_DIR="$USER_HOME/.ollama/models"
mkdir -p "$MODELS_DIR"
chown -R "$USER_NAME" "$(dirname "$MODELS_DIR")" "$MODELS_DIR"
log "models in $MODELS_DIR ($(df -h "$MODELS_DIR" | awk 'NR==2{print $4}') free)"

# ---- 2. Ollama ---------------------------------------------------------------
ollama_bin=/Applications/Ollama.app/Contents/Resources/ollama
if [[ -x "$ollama_bin" ]] && "$ollama_bin" --version 2>/dev/null | grep -q "$OLLAMA_VERSION"; then
    log "ollama $OLLAMA_VERSION already installed"
else
    tmp="$(mktemp -d)"
    curl -fsSL --retry 5 -o "$tmp/Ollama-darwin.zip" \
        "https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/Ollama-darwin.zip"
    if [[ -n "$OLLAMA_ZIP_SHA256" ]]; then
        echo "$OLLAMA_ZIP_SHA256  $tmp/Ollama-darwin.zip" | shasum -a 256 -c - \
            || die "Ollama-darwin.zip sha256 mismatch"
    fi
    rm -rf /Applications/Ollama.app
    ditto -x -k "$tmp/Ollama-darwin.zip" /Applications
    xattr -dr com.apple.quarantine /Applications/Ollama.app 2>/dev/null || true
    rm -rf "$tmp"
    [[ -x "$ollama_bin" ]] || die "Ollama.app has no CLI at $ollama_bin"
    log "installed ollama: $("$ollama_bin" --version 2>&1 | tail -1)"
fi
mkdir -p /usr/local/bin
ln -sf "$ollama_bin" /usr/local/bin/ollama

# Shared by the Ollama daemon and herd-node: herd-node starts `ollama serve`
# with its own environment if Ollama is ever down (CLAUDE.md, prompt cache
# gotcha), so both must carry the same OLLAMA_* values.
ollama_env() {
    cat <<EOF
    <key>OLLAMA_HOST</key><string>0.0.0.0:11434</string>
    <key>OLLAMA_MODELS</key><string>$MODELS_DIR</string>
    <key>OLLAMA_NUM_PARALLEL</key><string>4</string>
    <key>OLLAMA_KEEP_ALIVE</key><string>-1</string>
    <key>OLLAMA_MAX_LOADED_MODELS</key><string>10</string>
    <key>OLLAMA_CONTEXT_LENGTH</key><string>$OLLAMA_CONTEXT_LENGTH</string>
EOF
}

# ---- 3. Models from S3 -------------------------------------------------------
if [[ -n "$MODELS_S3$HF_S3" ]]; then
    # Not preinstalled: amzn-ec2-macos-26.7.3-20261120 has no aws on any
    # PATH (checked on mac-m4.metal, 2026-11-21).  AWS's own installer.
    aws_bin="$(command -v aws || true)"
    [[ -n "$aws_bin" || ! -x /usr/local/bin/aws ]] || aws_bin=/usr/local/bin/aws
    if [[ -z "$aws_bin" ]]; then
        log "installing the AWS CLI"
        tmp="$(mktemp -d)"
        curl -fsSL --retry 5 -o "$tmp/AWSCLIV2.pkg" https://awscli.amazonaws.com/AWSCLIV2.pkg \
            || die "AWS CLI download failed"
        installer -pkg "$tmp/AWSCLIV2.pkg" -target / >/dev/null || die "AWS CLI install failed"
        rm -rf "$tmp"
        aws_bin=/usr/local/bin/aws
    fi
    [[ -x "$aws_bin" ]] || die "aws CLI not found at $aws_bin"
    as_user "$aws_bin" configure set default.s3.max_concurrent_requests 64
fi
if [[ -n "$MODELS_S3" ]]; then
    log "syncing $MODELS_S3 -> $MODELS_DIR"
    start=$(date +%s)
    as_user "$aws_bin" s3 sync "$MODELS_S3" "$MODELS_DIR" --only-show-errors \
        || die "S3 sync of the Ollama models failed"
    log "models synced in $(( $(date +%s) - start ))s: $(du -sh "$MODELS_DIR" | cut -f1)"
    [[ -d "$MODELS_DIR/manifests" ]] || die "$MODELS_S3 has no manifests/ (upload ~/.ollama/models itself)"
fi
if [[ -n "$HF_S3" ]]; then
    hf_dir="$USER_HOME/.cache/huggingface/hub"
    as_user mkdir -p "$hf_dir"
    log "syncing $HF_S3 -> $hf_dir"
    as_user "$aws_bin" s3 sync "$HF_S3" "$hf_dir" --only-show-errors \
        || die "S3 sync of the Hugging Face cache failed"
fi

# ---- 4. herd (and mlx-lm) ----------------------------------------------------
if [[ ! -x "$USER_HOME/.local/bin/uv" ]]; then
    as_user sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' >/dev/null
fi
uv="$USER_HOME/.local/bin/uv"
[[ -x "$uv" ]] || die "uv did not install"

installed="$(as_user "$uv" tool list 2>/dev/null | awk '/^ollama-herd /{print $2}' | sed 's/^v//')"
if [[ "$installed" != "$HERD_VERSION" ]]; then
    log "installing ollama-herd[embedding]==$HERD_VERSION"
    as_user "$uv" tool install --force --python 3.12 "ollama-herd[embedding]==$HERD_VERSION" \
        || die "uv tool install ollama-herd==$HERD_VERSION failed (is it on PyPI yet?)"
fi
herd_node="$USER_HOME/.local/bin/herd-node"
[[ -x "$herd_node" ]] || die "herd-node not found at $herd_node"
log "herd-node: $(as_user "$herd_node" --version 2>&1 | tail -1)"

if [[ -n "$MLX_SERVERS" ]]; then
    mlx_installed="$(as_user "$uv" tool list 2>/dev/null | awk '/^mlx-lm /{print $2}' | sed 's/^v//')"
    if [[ "$mlx_installed" != "$MLX_LM_VERSION" ]]; then
        log "installing mlx-lm==$MLX_LM_VERSION"
        as_user "$uv" tool install --force --python 3.12 "mlx-lm==$MLX_LM_VERSION" \
            || die "uv tool install mlx-lm failed"
    fi
    as_user "$USER_HOME/.local/bin/mlx_lm.server" --help 2>&1 | grep -q -- --quantized-kv-start \
        || die "mlx_lm.server lacks --quantized-kv-start (wrong version on PATH?)"
fi

# herd-node reads ~/.fleet-manager/env at startup.  The router URL is also
# passed on the command line, so neither can be shadowed by the other.
fleet_dir="$USER_HOME/.fleet-manager"
as_user mkdir -p "$fleet_dir/logs"
{
    echo "# written by scripts/aws-mac-bootstrap.sh ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
    echo "FLEET_NODE_NODE_ID=$NODE_ID"
    echo "FLEET_NODE_ROUTER_URL=$ROUTER_URL"
    echo "FLEET_NODE_TELEMETRY=false"
    echo "FLEET_TELEMETRY=false"
    if [[ -n "$MLX_SERVERS" ]]; then
        echo "FLEET_NODE_MLX_ENABLED=true"
        echo "FLEET_NODE_MLX_BIND_HOST=0.0.0.0"
        echo "FLEET_NODE_MLX_SERVERS=$MLX_SERVERS"
    fi
} > "$fleet_dir/env"
chown "$USER_NAME" "$fleet_dir/env"

# ---- 5. LaunchDaemons ----------------------------------------------------------
# Daemons, not agents: an EC2 Mac boots with nobody logged in, and an agent in
# the gui/ domain would never start.  UserName runs them as ec2-user.
write_daemon() {  # label, log name, program arguments as <string> lines
    local label="$1" logname="$2" args="$3"
    cat > "/Library/LaunchDaemons/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>UserName</key><string>$USER_NAME</string>
  <key>ProgramArguments</key>
  <array>
$args
  </array>
  <key>WorkingDirectory</key><string>$USER_HOME</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$USER_HOME</string>
    <key>PATH</key><string>$USER_PATH</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
$(ollama_env)
  </dict>
  <key>StandardOutPath</key><string>$fleet_dir/logs/$logname.out</string>
  <key>StandardErrorPath</key><string>$fleet_dir/logs/$logname.err</string>
</dict>
</plist>
EOF
    chown root:wheel "/Library/LaunchDaemons/$label.plist"
    chmod 644 "/Library/LaunchDaemons/$label.plist"
    launchctl bootout "system/$label" 2>/dev/null || true
    launchctl bootstrap system "/Library/LaunchDaemons/$label.plist" \
        || die "launchctl bootstrap $label failed"
}

write_daemon "$OLLAMA_LABEL" ollama \
    "    <string>$ollama_bin</string>
    <string>serve</string>"

for _ in $(seq 60); do
    curl -sf localhost:11434/api/version >/dev/null && break
    sleep 1
done
version="$(curl -sf localhost:11434/api/version || true)"
[[ "$version" == *"$OLLAMA_VERSION"* ]] \
    || die "Ollama on :11434 reports '${version:-nothing}', expected $OLLAMA_VERSION"
models="$(curl -sf localhost:11434/api/tags | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["models"]))')"
log "ollama up: $version, $models models visible"

write_daemon "$NODE_LABEL" herd-node \
    "    <string>$herd_node</string>
    <string>--node-id</string><string>$NODE_ID</string>
    <string>--router-url</string><string>$ROUTER_URL</string>"

# ---- 6. Wait for the router to list this node ---------------------------------
log "waiting up to ${READY_TIMEOUT}s for $ROUTER_URL to list $NODE_ID online"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
status=""
while (( $(date +%s) < deadline )); do
    status="$(curl -sf --max-time 5 "$ROUTER_URL/fleet/status" | NODE_ID="$NODE_ID" python3 -c '
import json, os, sys
for n in json.load(sys.stdin)["nodes"]:
    if n["node_id"] == os.environ["NODE_ID"]:
        hw = n.get("hardware") or {}
        print(n["status"], n.get("agent_version"), hw.get("chip"), hw.get("memory_bandwidth_gbps"))
' 2>/dev/null || true)"
    [[ "$status" == online* ]] && break
    sleep 5
done
[[ "$status" == online* ]] \
    || die "router never listed $NODE_ID online (last: '${status:-absent}'); see $fleet_dir/logs/herd-node.err"
[[ "$status" == "online $HERD_VERSION "* ]] \
    || die "router lists $NODE_ID as '$status', expected agent version $HERD_VERSION"

log "READY $NODE_ID: $status"
echo "$NODE_ID $status $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$DONE"
