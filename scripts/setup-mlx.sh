#!/usr/bin/env bash
# Install mlx-lm at a known-good version and apply the ollama-herd
# KV-cache-quantization patch required for production use with the
# --kv-bits / --kv-group-size / --quantized-kv-start flags that
# ``mlx_supervisor.py`` passes.
#
# Why: stock ``mlx_lm.server`` does not expose KV quantization flags
# (see ``docs/experiments/mlx-lm-server-kv-bits.patch`` for the full
# rationale, benchmarks, and upstream PR links).  Without the patch,
# the node agent's auto-start fails with:
#
#     mlx_lm.server: error: unrecognized arguments: --kv-bits 8 --kv-group-size 64
#
# Usage:
#     ./scripts/setup-mlx.sh             # fresh install + patch
#     ./scripts/setup-mlx.sh --reinstall # force reinstall + re-patch
#
# Re-run after any ``uv tool upgrade mlx-lm`` — upgrading wipes the patch.
#
# Requires: macOS + Apple Silicon.  mlx-lm is Apple-GPU-only.

set -euo pipefail

PINNED_VERSION="0.31.3"  # version the patch was verified against

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCH_REF="$REPO_ROOT/docs/experiments/mlx-lm-server-kv-bits.patch"

if [[ "$(uname -s)" != "Darwin" ]] || [[ "$(uname -m)" != "arm64" ]]; then
    echo "ERROR: mlx-lm requires macOS on Apple Silicon (arm64)."
    echo "Skipping setup; MLX backend will be unavailable on this machine."
    exit 0  # soft-skip; core routing works without MLX
fi

# --- 1. Install mlx-lm pinned to the known-good version ----------------------

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

# --- 2. Locate the installed server.py ---------------------------------------

SERVER_PY=""
for candidate in \
    "$HOME/.local/share/uv/tools/mlx-lm/lib/python"*/site-packages/mlx_lm/server.py \
    "$HOME/Library/Application Support/uv/tools/mlx-lm/lib/python"*/site-packages/mlx_lm/server.py \
    "$HOME/.local/lib/python"*/site-packages/mlx_lm/server.py; do
    if [[ -f "$candidate" ]]; then
        SERVER_PY="$candidate"
        break
    fi
done

if [[ -z "$SERVER_PY" ]]; then
    echo "ERROR: couldn't locate mlx_lm/server.py after install."
    echo "If you installed mlx-lm via a non-standard method, patch manually"
    echo "using $PATCH_REF as reference."
    exit 1
fi

echo "Target: $SERVER_PY"

# --- 3. Apply the patch via Python (embedded — safer than `patch`) -----------

python3 - "$SERVER_PY" <<'PYEOF'
import sys

path = sys.argv[1]
with open(path) as f:
    src = f.read()

# Already patched?
if "ollama-herd patch" in src:
    print("Already patched — nothing to do.")
    sys.exit(0)

# Hunk 1: _is_batchable must return False when --kv-bits is set (no
# BatchQuantizedKVCache exists upstream yet).  See mlx-lm PR #1073.
old1 = (
    "    def _is_batchable(self, args):\n"
    "        return self.model_provider.is_batchable and args.seed is None\n"
)
new1 = (
    "    def _is_batchable(self, args):\n"
    "        # ollama-herd patch: BatchQuantizedKVCache doesn't exist yet, so continuous\n"
    "        # batching must be disabled when the user has asked for a quantized KV cache.\n"
    '        if getattr(self.cli_args, "kv_bits", None) is not None:\n'
    "            return False\n"
    "        return self.model_provider.is_batchable and args.seed is None\n"
)
assert old1 in src, "Hunk 1 anchor missing — mlx-lm version drift?"
src = src.replace(old1, new1, 1)

# Hunk 2: forward kv_bits / kv_group_size / quantized_kv_start to
# stream_generate (the non-batched _generate path).  Two call sites
# match this pattern — we patch the LAST (the streaming _generate),
# which is what the server path uses for chat completions.
old2 = (
    "                num_draft_tokens=args.num_draft_tokens,\n"
    "                prompt_progress_callback=progress,\n"
    "                prefill_step_size=self.cli_args.prefill_step_size,\n"
    "            ):\n"
)
new2 = (
    "                num_draft_tokens=args.num_draft_tokens,\n"
    "                prompt_progress_callback=progress,\n"
    "                prefill_step_size=self.cli_args.prefill_step_size,\n"
    "                **({\n"
    '                    "kv_bits": self.cli_args.kv_bits,\n'
    '                    "kv_group_size": self.cli_args.kv_group_size,\n'
    '                    "quantized_kv_start": self.cli_args.quantized_kv_start,\n'
    '                } if getattr(self.cli_args, "kv_bits", None) is not None else {}),\n'
    "            ):\n"
)
parts = src.rsplit(old2, 1)
assert len(parts) == 2, "Hunk 2 anchor missing — mlx-lm version drift?"
src = parts[0] + new2 + parts[1]

# Hunk 3: expose --kv-bits / --kv-group-size / --quantized-kv-start on
# the argparse.  Inserted directly after --pipeline.
old3 = (
    '    parser.add_argument(\n'
    '        "--pipeline",\n'
    '        action="store_true",\n'
    '        help="Use pipelining instead of tensor parallelism",\n'
    '    )\n'
)
new3 = old3 + (
    "    # --- ollama-herd patch: expose KV cache quantization (matches Ollama's OLLAMA_KV_CACHE_TYPE) ---\n"
    "    parser.add_argument(\n"
    '        "--kv-bits",\n'
    "        type=int,\n"
    "        default=None,\n"
    "        choices=[4, 8],\n"
    '        help="Quantize KV cache to N bits (4 or 8). 8 matches OLLAMA_KV_CACHE_TYPE=q8_0.",\n'
    "    )\n"
    "    parser.add_argument(\n"
    '        "--kv-group-size",\n'
    "        type=int,\n"
    "        default=64,\n"
    '        help="Group size for KV cache quantization (default: 64).",\n'
    "    )\n"
    "    parser.add_argument(\n"
    '        "--quantized-kv-start",\n'
    "        type=int,\n"
    "        default=0,\n"
    '        help="Start KV quantization after N tokens (default: 0 = from start).",\n'
    "    )\n"
    "    # --- end patch ---\n"
)
assert old3 in src, "Hunk 3 anchor missing — mlx-lm version drift?"
src = src.replace(old3, new3, 1)

with open(path, "w") as f:
    f.write(src)

print("Patched successfully.")
PYEOF

# --- 4. Verify the patched server advertises the new flags -------------------

if ! mlx_lm.server --help 2>&1 | grep -q -- "--kv-bits"; then
    echo "ERROR: --kv-bits flag not found after patching — patch may have applied"
    echo "to the wrong file, or a cached install is intercepting."
    exit 1
fi

echo ""
echo "✓ mlx-lm $PINNED_VERSION installed and patched."
echo "✓ --kv-bits / --kv-group-size / --quantized-kv-start flags available."
echo ""
echo "Next: ensure your shell env has the FLEET_NODE_MLX_* vars set."
echo "See docs/guides/mlx-setup.md for the full env block."
