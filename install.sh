#!/bin/bash
# multicodex installer. Discovers your logged-in assistant CLIs and sets up the
# bridge without enabling anything in Codex until you choose.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v codex   >/dev/null || echo "warning: 'codex' not on PATH — install Codex first"
chmod +x "$ROOT/bin/bridgectl" "$ROOT/bin/uninstall" "$ROOT"/bin/*.py 2>/dev/null || true
exec python3 "$ROOT/bin/install.py"
