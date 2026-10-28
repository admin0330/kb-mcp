#!/usr/bin/env bash
#
# setup.sh — one-shot environment setup for the kb-mcp servers.
# Installs uv (if missing), creates an isolated Python 3.11 venv, and installs
# all dependencies. Logs everything to kb-mcp/setup.log.
#
# Usage:
#   ./setup.sh            # core dependencies only (keyword + hybrid-less search)
#   ./setup.sh --semantic # also install FastEmbed for semantic/hybrid search
#
set -euo pipefail

ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="${ROOT}/server"
VENV="${SERVER_DIR}/.venv"
LOG="${ROOT}/setup.log"

mkdir -p "$ROOT"
exec > >(tee -a "$LOG") 2>&1
echo "=============================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] kb-mcp setup starting"
echo "=============================================================="

# --- 1. ensure uv ---------------------------------------------------------- #
if ! command -v uv >/dev/null 2>&1; then
  if [[ -x "$HOME/.local/bin/uv" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
  else
    echo ">> uv not found — installing via astral.sh installer"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi
echo ">> uv: $(command -v uv) ($(uv --version))"

# --- 2. create venv (Python 3.11, downloaded by uv if absent) -------------- #
cd "$SERVER_DIR"
echo ">> creating venv at ${VENV}"
uv venv --python 3.11 "$VENV"

# --- 3. install dependencies ----------------------------------------------- #
echo ">> installing core dependencies"
uv pip install --python "${VENV}/bin/python" -r requirements.txt

if [[ "${1:-}" == "--semantic" ]]; then
  echo ">> installing FastEmbed (semantic search)"
  uv pip install --python "${VENV}/bin/python" fastembed
fi

# --- 4. verify ------------------------------------------------------------- #
echo ">> verifying imports"
"${VENV}/bin/python" - <<'PY'
import importlib.util, sys
mods = ["mcp", "pypdf", "docx", "pptx", "openpyxl", "ebooklib", "bs4", "numpy"]
missing = [m for m in mods if importlib.util.find_spec(m) is None]
try:
    import fastembed  # noqa: F401
    sem = "yes"
except ModuleNotFoundError:
    sem = "no (run with --semantic to enable)"
print("  python   :", sys.version.split()[0])
print("  core deps:", "OK" if not missing else f"MISSING {missing}")
print("  semantic :", sem)
sys.exit(1 if missing else 0)
PY

# --- 5. pinned local copy of the official filesystem server ---------------- #
# kb-files then starts in ~0.3s and works offline (npx checks the registry).
FS_VERSION="${KB_FS_VERSION:-2.0.1}"
if command -v npm >/dev/null 2>&1; then
  echo ">> installing @modelcontextprotocol/server-filesystem@${FS_VERSION} into server/node"
  npm install --prefix "${SERVER_DIR}/node" --no-audit --no-fund --silent \
    "@modelcontextprotocol/server-filesystem@${FS_VERSION}" \
    || echo "   (npm install failed; kb-files will fall back to npx)"
else
  echo ">> npm not found; kb-files will fall back to npx at runtime"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] setup complete."
echo "Next: copy config/claude_desktop_config.json into"
echo "  ~/Library/Application Support/Claude/  and restart Claude Desktop."
