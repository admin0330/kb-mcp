#!/usr/bin/env bash
# Launcher for the custom knowledge-base retrieval MCP server.
# Starts regardless of whether the drive is mounted (each tool reports the
# drive state), so Claude Desktop never fails on boot.
set -euo pipefail

ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="${ROOT}/server"
PY="${SERVER_DIR}/.venv/bin/python"

export KB_VOLUME="${KB_VOLUME:-/Volumes/KnowledgeBase/Database}"
export KB_ENABLE_SEMANTIC="${KB_ENABLE_SEMANTIC:-0}"
export KB_EMBED_MODEL="${KB_EMBED_MODEL:-intfloat/multilingual-e5-small}"
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH}"

if [[ -x "$PY" ]]; then
  exec "$PY" "${SERVER_DIR}/kb_mcp_server.py"
fi

# No local venv: fall back to running the package from this checkout via uvx.
if command -v uvx >/dev/null 2>&1; then
  echo "kb-mcp: venv not found at $PY — falling back to uvx (run scripts/setup.sh to fix)." >&2
  exec uvx --from "$ROOT" kb-mcp
fi

echo "kb-mcp: venv python not found at $PY and uvx is unavailable — run scripts/setup.sh first." >&2
exit 1
