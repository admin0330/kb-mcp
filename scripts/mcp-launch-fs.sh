#!/usr/bin/env bash
# Launcher for kb-files (the OFFICIAL @modelcontextprotocol/server-filesystem).
#
# Default: run it behind server/fs_gateway.py. The gateway always advertises
# the full filesystem tool list and starts the real server lazily once the
# drive is usable, so plugging the drive in AFTER Claude Desktop started no
# longer requires a restart (Claude Desktop never refreshes a tool list).
#
# KB_FS_GATEWAY=0 restores the old start-up decision:
#   drive mounted -> real filesystem server, else -> placeholder server.
set -euo pipefail

ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="${ROOT}/server"
PY="${SERVER_DIR}/.venv/bin/python"

export KB_VOLUME="${KB_VOLUME:-/Volumes/KnowledgeBase/Database}"
# GUI apps may start servers with a minimal PATH; make Homebrew's npx findable.
export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH}"

FS_CMD=(npx -y @modelcontextprotocol/server-filesystem "$KB_VOLUME")

if [[ ! -x "$PY" ]]; then
  echo "kb-files: venv python not found at $PY — run scripts/setup.sh first." >&2
  if [[ -d "$KB_VOLUME" && -w "$KB_VOLUME" ]]; then
    exec "${FS_CMD[@]}"
  fi
  exit 1
fi

if [[ "${KB_FS_GATEWAY:-1}" != "0" ]]; then
  exec "$PY" "${SERVER_DIR}/fs_gateway.py" -- "${FS_CMD[@]}"
fi

if [[ -d "$KB_VOLUME" && -w "$KB_VOLUME" ]]; then
  exec "${FS_CMD[@]}"
else
  exec "$PY" "${SERVER_DIR}/placeholder_server.py"
fi
