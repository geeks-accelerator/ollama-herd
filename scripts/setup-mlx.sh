#!/usr/bin/env bash
# Install mlx-lm at the version ollama-herd is tested against.
#
# ``mlx_supervisor.py`` passes --kv-bits / --kv-group-size / --quantized-kv-start
# to ``mlx_lm.server`` for KV-cache quantization.  mlx-lm ships those natively
# since 0.32.0 (PR #1832) — before that, this script patched them in (see
# ``docs/experiments/mlx-lm-server-kv-bits.patch``, kept for history).  An older
# mlx-lm makes the node agent's auto-start fail with:
#
#     mlx_lm.server: error: unrecognized arguments: --kv-bits 8 --kv-group-size 64
#
# The pin is exact on purpose: this subsystem has broken on mlx-lm upgrades
# before, so a version is adopted only after a soak on the reference fleet.
#
# Usage:
#     ./scripts/setup-mlx.sh             # install (no-op if already pinned)
#     ./scripts/setup-mlx.sh --reinstall # force reinstall
#
# Requires: macOS + Apple Silicon.  mlx-lm is Apple-GPU-only.

set -euo pipefail

PINNED_VERSION="0.32.0"  # first release with native --kv-bits (PR #1832)

if [[ "$(uname -s)" != "Darwin" ]] || [[ "$(uname -m)" != "arm64" ]]; then
    echo "ERROR: mlx-lm requires macOS on Apple Silicon (arm64)."
    echo "Skipping setup; MLX backend will be unavailable on this machine."
    exit 0  # soft-skip; core routing works without MLX
fi

# --- 1. Install mlx-lm pinned to the tested version --------------------------

NEED_INSTALL=1
if command -v mlx_lm.server >/dev/null 2>&1; then
    CURRENT="$(uv tool list 2>/dev/null | awk '/^mlx-lm /{print $2}' | sed 's/^v//')"
    if [[ "$CURRENT" == "$PINNED_VERSION" ]] && [[ "${1:-}" != "--reinstall" ]]; then
        echo "mlx-lm $CURRENT already installed — skipping install."
        NEED_INSTALL=0
    fi
fi

if [[ $NEED_INSTALL -eq 1 ]]; then
    echo "Installing mlx-lm==$PINNED_VERSION via uv tool..."
    uv tool uninstall mlx-lm >/dev/null 2>&1 || true
    uv tool install "mlx-lm==$PINNED_VERSION"
fi

# --- 2. Verify the server advertises the flags the supervisor passes ---------

HELP="$(mlx_lm.server --help 2>&1)"
for flag in --kv-bits --kv-group-size --quantized-kv-start; do
    if ! grep -q -- "$flag" <<<"$HELP"; then
        echo "ERROR: mlx_lm.server does not advertise $flag."
        echo "Another mlx_lm.server may be earlier on PATH: $(command -v mlx_lm.server)"
        exit 1
    fi
done

echo ""
echo "✓ mlx-lm $PINNED_VERSION installed."
echo "✓ --kv-bits / --kv-group-size / --quantized-kv-start available natively."
echo ""
echo "Next: ensure your shell env has the FLEET_NODE_MLX_* vars set."
echo "See docs/guides/mlx-setup.md for the full env block."
