#!/usr/bin/env bash
# Mount-aware launcher for the OFFICIAL @modelcontextprotocol/server-filesystem.
# When the volume is mounted -> launch the real filesystem server scoped to it.
# When it is NOT mounted      -> launch a tiny placeholder server so Claude
#                                Desktop starts cleanly instead of erroring.
set -euo pipefail

SERVER_DIR="/Users/ym1r/kb-mcp/server"
PY="${SERVER_DIR}/.venv/bin/python"
VOLUME="${KB_VOLUME:-/Volumes/KnowledgeBase/Database}"

export KB_VOLUME="$VOLUME"

if [[ -d "$VOLUME" && -w "$VOLUME" ]]; then
  # -y so npx never blocks on an install prompt (first run downloads the pkg).
  exec npx -y @modelcontextprotocol/server-filesystem "$VOLUME"
else
  exec "$PY" "${SERVER_DIR}/placeholder_server.py"
fi
