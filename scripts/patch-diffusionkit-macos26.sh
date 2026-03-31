#!/usr/bin/env bash
# Patches DiffusionKit's argmaxtools for macOS 26+ compatibility.
#
# macOS 26 added ProductVersionExtra to sw_vers output, breaking
# argmaxtools.test_utils.os_spec() which expects exactly 3 lines.
#
# Usage: ./scripts/patch-diffusionkit-macos26.sh
#
# Re-run after upgrading DiffusionKit:
#   uv tool upgrade diffusionkit && ./scripts/patch-diffusionkit-macos26.sh

set -euo pipefail

# Find the argmaxtools test_utils.py file
CANDIDATES=(
    # uv tool install location
    "$HOME/.local/share/uv/tools/diffusionkit/lib/python"*/site-packages/argmaxtools/test_utils.py
    # pip install location
    "$HOME/.local/lib/python"*/site-packages/argmaxtools/test_utils.py
    # system site-packages
    /Library/Python/*/site-packages/argmaxtools/test_utils.py
)

TARGET=""
for f in "${CANDIDATES[@]}"; do
    if [ -f "$f" ]; then
        TARGET="$f"
        break
    fi
done

if [ -z "$TARGET" ]; then
    echo "ERROR: Could not find argmaxtools/test_utils.py"
    echo "Is DiffusionKit installed? Run: uv tool install diffusionkit"
    exit 1
fi

echo "Found: $TARGET"

# Check if already patched
if grep -q 'parsed.get("ProductName"' "$TARGET" 2>/dev/null; then
    echo "Already patched. Nothing to do."
    exit 0
fi

# Check if the buggy pattern exists
if ! grep -q 'os_type, os_version, os_build_number = \[' "$TARGET" 2>/dev/null; then
    echo "WARNING: Expected pattern not found. The file may have been updated upstream."
    echo "Check $TARGET manually."
    exit 1
fi

# Apply the patch
sed -i.bak '
/os_type, os_version, os_build_number = \[/{
    N;N;N
    s/os_type, os_version, os_build_number = \[\n *line\.rsplit("\\\\t\\\\t")\[1\]\n *for line in sw_vers\.rsplit("\\\\n")\n *\]/parsed = {\
            line.rsplit("\\t\\t")[0].rstrip(":"): line.rsplit("\\t\\t")[1]\
            for line in sw_vers.rsplit("\\n")\
            if "\\t\\t" in line\
        }\
        os_type = parsed.get("ProductName", "macOS")\
        os_version = parsed.get("ProductVersion", "0.0")\
        os_build_number = parsed.get("BuildVersion", "unknown")/
}
' "$TARGET"

# Verify the patch worked
if grep -q 'parsed.get("ProductName"' "$TARGET" 2>/dev/null; then
    echo "Patched successfully."
    rm -f "${TARGET}.bak"
else
    echo "WARNING: sed patch may not have applied cleanly."
    echo "Restoring backup and applying manual patch..."
    mv "${TARGET}.bak" "$TARGET"

    # Fallback: use Python to apply the patch
    python3 -c "
import re

with open('$TARGET', 'r') as f:
    content = f.read()

old = '''os_type, os_version, os_build_number = [
            line.rsplit(\"\\t\\t\")[1]
            for line in sw_vers.rsplit(\"\\n\")
        ]'''

new = '''parsed = {
            line.rsplit(\"\\t\\t\")[0].rstrip(\":\"): line.rsplit(\"\\t\\t\")[1]
            for line in sw_vers.rsplit(\"\\n\")
            if \"\\t\\t\" in line
        }
        os_type = parsed.get(\"ProductName\", \"macOS\")
        os_version = parsed.get(\"ProductVersion\", \"0.0\")
        os_build_number = parsed.get(\"BuildVersion\", \"unknown\")'''

if old in content:
    content = content.replace(old, new)
    with open('$TARGET', 'w') as f:
        f.write(content)
    print('Patched successfully (Python fallback).')
else:
    print('ERROR: Could not find the expected code pattern.')
    print('The file may already be patched or updated upstream.')
    exit(1)
"
fi
