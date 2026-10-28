#!/usr/bin/env python3
"""Minimal stand-in MCP server used when the external drive is NOT mounted.

The ``kb-files`` launcher execs this instead of the real filesystem server when
the volume is missing, so Claude Desktop sees a server that starts cleanly and
reports unavailability rather than a server that crashes on boot.
"""
from __future__ import annotations

import json
import os

from mcp.server.fastmcp import FastMCP

VOLUME = os.environ.get("KB_VOLUME", "/Volumes/KnowledgeBase")
mcp = FastMCP("kb-files-offline")


@mcp.tool()
def kb_status() -> str:
    """Report that the knowledge-base drive is not mounted."""
    return json.dumps({
        "ok": True,
        "mounted": False,
        "volume": VOLUME,
        "message": "KnowledgeBase drive is not mounted. Plug it in and restart "
                   "Claude Desktop to enable file access.",
    }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
