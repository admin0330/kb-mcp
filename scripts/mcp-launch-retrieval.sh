#!/usr/bin/env bash
# Launcher for the custom knowledge-base retrieval MCP server.
# Starts regardless of whether the drive is mounted (the server reports
# "drive_not_mounted" per tool call), so Claude Desktop never crashes on boot.
set -euo pipefail

SERVER_DIR="/Users/ym1r/kb-mcp/server"
PY="${SERVER_DIR}/.venv/bin/python"

export KB_VOLUME="${KB_VOLUME:-/Volumes/KnowledgeBase/Database}"
export KB_ENABLE_SEMANTIC="${KB_ENABLE_SEMANTIC:-0}"
export KB_EMBED_MODEL="${KB_EMBED_MODEL:-intfloat/multilingual-e5-small}"

if [[ ! -x "$PY" ]]; then
  echo "kb-mcp: venv python not found at $PY — run setup first." >&2
  exit 1
fi

exec "$PY" "${SERVER_DIR}/kb_mcp_server.py"
