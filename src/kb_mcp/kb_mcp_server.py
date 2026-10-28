#!/usr/bin/env python3
"""Knowledge-Base MCP server (retrieval + index-aware file operations).

Runs over stdio for Claude Desktop. Everything is scoped to a single external
volume (``KB_VOLUME``). The server is deliberately *mount-tolerant*: it starts
even when the drive is unplugged and every tool returns a friendly
"drive not mounted" payload instead of crashing the MCP client.

Environment variables
----------------------
KB_VOLUME            Mount point of the knowledge base  (default /Volumes/KnowledgeBase)
KB_ENABLE_SEMANTIC   "1" to enable FastEmbed semantic search (default off)
KB_EMBED_MODEL       FastEmbed model name (default intfloat/multilingual-e5-small)
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

# Make sibling modules importable whether launched by path or as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from .indexer import Indexer, META_DIRNAME  # noqa: E402
    from .extractors import ExtractionError, extract_text, is_supported  # noqa: E402
except (ImportError, ValueError):
    from indexer import Indexer, META_DIRNAME  # noqa: E402
    from extractors import ExtractionError, extract_text, is_supported  # noqa: E402

VOLUME = Path(os.environ.get("KB_VOLUME", "/Volumes/KnowledgeBase/Database"))
ENABLE_SEMANTIC = os.environ.get("KB_ENABLE_SEMANTIC", "0") == "1"
EMBED_MODEL = os.environ.get("KB_EMBED_MODEL", "intfloat/multilingual-e5-small")

CATEGORY_DIRS = ["_Inbox", "01_Documents", "02_Books", "03_Notes", "04_Archive"]
INDEX_FILENAME = "INDEX.md"
LOG_RELPATH = f"{META_DIRNAME}/organize.log"

mcp = FastMCP("kb-retrieval")
_indexer: Optional[Indexer] = None


# --------------------------------------------------------------------------- #
# Infrastructure helpers
# --------------------------------------------------------------------------- #
def _mounted() -> bool:
    """A mounted APFS volume has our category dirs (or at least exists & writable)."""
    return VOLUME.is_dir() and os.access(VOLUME, os.W_OK)


def _require_mount() -> Optional[str]:
    """Return a JSON error string when the drive is unavailable, else ``None``."""
    if not _mounted():
        return json.dumps({
            "ok": False,
            "error": "drive_not_mounted",
            "message": f"Volume {VOLUME} is not mounted or not writable. "
                       f"Plug in the drive and retry.",
        }, ensure_ascii=False)
    return None


def _get_indexer() -> Indexer:
    global _indexer
    if _indexer is None:
        _indexer = Indexer(VOLUME, enable_semantic=ENABLE_SEMANTIC, embed_model=EMBED_MODEL)
    return _indexer


def _safe(rel: str) -> Path:
    """Resolve ``rel`` under the volume root, refusing path-traversal escapes."""
    rel = rel.strip().lstrip("/")
    target = (VOLUME / rel).resolve()
    root = VOLUME.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes volume root: {rel}")
    return target


def _rel(path: Path) -> str:
    return str(path.resolve().relative_to(VOLUME.resolve()))


def _ok(payload: dict) -> str:
    payload.setdefault("ok", True)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _log(message: str) -> None:
    try:
        log_path = VOLUME / LOG_RELPATH
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass  # logging must never break a tool call


# --------------------------------------------------------------------------- #
# Status & retrieval tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def kb_status() -> str:
    """Report whether the drive is mounted and basic index statistics."""
    if not _mounted():
        return json.dumps({
            "ok": True, "mounted": False, "volume": str(VOLUME),
            "message": "Drive not mounted. All data tools are paused.",
        }, ensure_ascii=False, indent=2)
    stats = _get_indexer().stats()
    return _ok({
        "mounted": True, "volume": str(VOLUME),
        "categories": [d for d in CATEGORY_DIRS if (VOLUME / d).is_dir()],
        "index": stats,
    })


@mcp.tool()
def kb_search(query: str, top_k: int = 8, mode: str = "hybrid") -> str:
    """Search the knowledge base and return the most relevant text chunks.

    :param query: Natural-language question or keywords (Chinese/English).
    :param top_k: Number of chunks to return (1-30).
    :param mode: "keyword" (BM25/substring), "semantic" (embeddings), or
        "hybrid" (both, rank-fused). "hybrid" is recommended.
    """
    err = _require_mount()
    if err:
        return err
    top_k = max(1, min(int(top_k), 30))
    hits = _get_indexer().search(query, top_k=top_k, mode=mode)
    return _ok({
        "query": query, "mode": mode, "count": len(hits),
        "results": [
            {"path": h.path, "chunk": h.chunk_id, "score": round(h.score, 4),
             "source": h.source, "snippet": h.snippet}
            for h in hits
        ],
    })


@mcp.tool()
def kb_reindex(full: bool = False) -> str:
    """Scan the volume and update the search index incrementally.

    :param full: When true, re-extract every file instead of only changed ones.
    """
    err = _require_mount()
    if err:
        return err
    stats = _get_indexer().reindex(full=bool(full))
    _log(f"reindex full={full}: indexed={stats.indexed} removed={stats.removed} "
         f"failed={stats.failed} in {stats.seconds}s")
    return _ok({
        "scanned": stats.scanned, "indexed": stats.indexed,
        "skipped_unchanged": stats.skipped_unchanged, "removed": stats.removed,
        "failed": stats.failed, "seconds": stats.seconds,
        "errors": stats.errors[:20],
    })


@mcp.tool()
def kb_read_document(relpath: str, max_chars: int = 6000) -> str:
    """Return the extracted plain text of one document (for quoting/summarising).

    :param relpath: Path relative to the volume root, e.g. ``_Inbox/foo.pdf``.
    :param max_chars: Maximum characters to return (1-50000).
    """
    err = _require_mount()
    if err:
        return err
    from extractors import extract_text, is_supported, ExtractionError

    try:
        path = _safe(relpath)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": "bad_path", "message": str(exc)},
                          ensure_ascii=False)
    if not path.is_file():
        return json.dumps({"ok": False, "error": "not_found", "message": str(relpath)},
                          ensure_ascii=False)
    if not is_supported(path):
        return json.dumps({"ok": False, "error": "unsupported", "message": path.suffix},
                          ensure_ascii=False)
    try:
        text = extract_text(path, max_chars=max(1, min(int(max_chars), 50000)))
    except ExtractionError as exc:
        return json.dumps({"ok": False, "error": "extract_failed", "message": str(exc)},
                          ensure_ascii=False)
    return _ok({"path": relpath, "chars": len(text), "text": text})


# --------------------------------------------------------------------------- #
# Navigation & file-operation tools (index-aware)
# --------------------------------------------------------------------------- #
@mcp.tool()
def kb_list_tree(subdir: str = ".", depth: int = 2) -> str:
    """List the directory tree under ``subdir`` (depth-limited)."""
    err = _require_mount()
    if err:
        return err
    try:
        base = _safe(subdir)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": "bad_path", "message": str(exc)},
                          ensure_ascii=False)
    if not base.is_dir():
        return json.dumps({"ok": False, "error": "not_a_dir", "message": subdir},
                          ensure_ascii=False)
    depth = max(0, min(int(depth), 6))
    lines: List[str] = []

    def walk(d: Path, level: int) -> None:
        if level > depth:
            return
        for entry in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if entry.name.startswith(".") or entry.name == META_DIRNAME:
                continue
            indent = "  " * level
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                walk(entry, level + 1)
            else:
                size_kb = entry.stat().st_size / 1024
                lines.append(f"{indent}{entry.name} ({size_kb:.0f} KB)")

    walk(base, 0)
    return _ok({"subdir": subdir, "tree": "\n".join(lines)})


@mcp.tool()
def kb_scan_inbox(preview_chars: int = 600) -> str:
    """List files in ``_Inbox`` with a short content preview for triage.

    Use the previews to decide a target category and a new name, then call
    :func:`kb_move_file`.
    """
    err = _require_mount()
    if err:
        return err
    from extractors import extract_text, is_supported, ExtractionError

    inbox = VOLUME / "_Inbox"
    if not inbox.is_dir():
        return json.dumps({"ok": False, "error": "no_inbox",
                           "message": "_Inbox missing; run the init script."},
                          ensure_ascii=False)
    preview_chars = max(100, min(int(preview_chars), 4000))
    items = []
    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        entry = {"path": _rel(path), "name": path.name,
                 "size_kb": round(path.stat().st_size / 1024, 1),
                 "ext": path.suffix.lower(), "supported": is_supported(path)}
        if is_supported(path):
            try:
                entry["preview"] = extract_text(path, max_chars=preview_chars)[:preview_chars]
            except ExtractionError as exc:
                entry["preview"] = f"<extract failed: {exc}>"
        items.append(entry)
    return _ok({"inbox_count": len(items), "items": items,
                "categories": CATEGORY_DIRS[1:]})


@mcp.tool()
def kb_create_directory(relpath: str) -> str:
    """Create a directory (and parents) under the volume root."""
    err = _require_mount()
    if err:
        return err
    try:
        target = _safe(relpath)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": "bad_path", "message": str(exc)},
                          ensure_ascii=False)
    target.mkdir(parents=True, exist_ok=True)
    _log(f"mkdir {relpath}")
    return _ok({"created": _rel(target)})


@mcp.tool()
def kb_move_file(src_rel: str, dest_dir_rel: str, new_name: str = "") -> str:
    """Move (and optionally rename) a file into a category directory.

    The search index is updated in place — no reindex needed afterwards.

    :param src_rel: Current path, e.g. ``_Inbox/report.pdf``.
    :param dest_dir_rel: Target directory, e.g. ``01_Documents``.
    :param new_name: Optional new filename; keeps the original when empty.
    """
    err = _require_mount()
    if err:
        return err
    try:
        src = _safe(src_rel)
        dest_dir = _safe(dest_dir_rel)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": "bad_path", "message": str(exc)},
                          ensure_ascii=False)
    if not src.is_file():
        return json.dumps({"ok": False, "error": "not_found", "message": src_rel},
                          ensure_ascii=False)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / (new_name.strip() or src.name)
    if target.exists():
        stem, suffix = target.stem, target.suffix
        target = dest_dir / f"{stem}_{int(time.time())}{suffix}"
    old_rel = _rel(src)
    shutil.move(str(src), str(target))
    new_rel = _rel(target)
    try:
        _get_indexer().rename_in_index(old_rel, new_rel)
    except Exception:  # noqa: BLE001 - index drift is recoverable via reindex
        pass
    _log(f"move {old_rel} -> {new_rel}")
    return _ok({"from": old_rel, "to": new_rel})


@mcp.tool()
def kb_rename_file(src_rel: str, new_name: str) -> str:
    """Rename a file in place (same directory). Index is kept in sync."""
    err = _require_mount()
    if err:
        return err
    try:
        src = _safe(src_rel)
    except ValueError as exc:
        return json.dumps({"ok": False, "error": "bad_path", "message": str(exc)},
                          ensure_ascii=False)
    if not src.is_file():
        return json.dumps({"ok": False, "error": "not_found", "message": src_rel},
                          ensure_ascii=False)
    target = src.parent / new_name.strip()
    if target.exists():
        return json.dumps({"ok": False, "error": "exists", "message": new_name},
                          ensure_ascii=False)
    old_rel = _rel(src)
    src.rename(target)
    new_rel = _rel(target)
    try:
        _get_indexer().rename_in_index(old_rel, new_rel)
    except Exception:  # noqa: BLE001
        pass
    _log(f"rename {old_rel} -> {new_rel}")
    return _ok({"from": old_rel, "to": new_rel})


@mcp.tool()
def kb_log(message: str) -> str:
    """Append a line to the organize log (``.mcp_meta/organize.log``)."""
    err = _require_mount()
    if err:
        return err
    _log(message)
    return _ok({"logged": message})


# --------------------------------------------------------------------------- #
# INDEX.md maintenance & health check
# --------------------------------------------------------------------------- #
def _build_index_md() -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 📚 KnowledgeBase Index",
        "",
        f"> Auto-generated by kb-mcp on {today}. Do not edit by hand below this line; "
        "run the 【健康检查】/【一键整理】 prompt to refresh.",
        "",
    ]
    descriptions = {
        "_Inbox": "待整理缓冲区 (triage buffer)",
        "01_Documents": "通用文档、报告 (documents & reports)",
        "02_Books": "电子书、手册 (books & manuals)",
        "03_Notes": "个人笔记、卡片 (notes & cards)",
        "04_Archive": "历史归档 (archive)",
    }
    for cat in CATEGORY_DIRS:
        cat_dir = VOLUME / cat
        if not cat_dir.is_dir():
            continue
        files = sorted(p for p in cat_dir.rglob("*")
                       if p.is_file() and not p.name.startswith("."))
        lines.append(f"## {cat}  —  {descriptions.get(cat, '')}  ({len(files)})")
        if not files:
            lines.append("- _(empty)_")
        for f in files:
            rel = _rel(f)
            size_kb = f.stat().st_size / 1024
            lines.append(f"- [{f.name}](<{rel}>)  · {size_kb:.0f} KB")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@mcp.tool()
def kb_update_index_md() -> str:
    """Regenerate the root ``INDEX.md`` from the current directory contents.

    Writes atomically (temp file + rename) so an interrupted run never leaves a
    truncated index.
    """
    err = _require_mount()
    if err:
        return err
    content = _build_index_md()
    index_path = VOLUME / INDEX_FILENAME
    tmp = index_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, index_path)
    _log("INDEX.md regenerated")
    return _ok({"index": INDEX_FILENAME, "bytes": len(content.encode("utf-8"))})


@mcp.tool()
def kb_health_check() -> str:
    """Audit the knowledge base for common problems.

    Reports: files still in ``_Inbox`` (backlog), files on disk that are not
    referenced in ``INDEX.md`` (orphans), ``INDEX.md`` links that point to
    missing files (dead links), and files not yet in the search index.
    """
    err = _require_mount()
    if err:
        return err
    import re

    # Files physically present under the managed category dirs (excluding Inbox).
    on_disk = set()
    for cat in CATEGORY_DIRS[1:]:
        cat_dir = VOLUME / cat
        if cat_dir.is_dir():
            for p in cat_dir.rglob("*"):
                if p.is_file() and not p.name.startswith("."):
                    on_disk.add(_rel(p))

    # Paths referenced by INDEX.md.
    referenced = set()
    index_path = VOLUME / INDEX_FILENAME
    if index_path.is_file():
        text = index_path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\]\(<?([^)>]+)>?\)", text):
            referenced.add(m.group(1).strip())

    dead_links = sorted(r for r in referenced
                        if not (VOLUME / r).is_file() and not r.startswith("http"))
    orphans = sorted(on_disk - referenced)

    inbox = VOLUME / "_Inbox"
    backlog = []
    if inbox.is_dir():
        backlog = sorted(_rel(p) for p in inbox.rglob("*")
                         if p.is_file() and not p.name.startswith("."))

    indexed = set(_get_indexer().indexed_paths())
    unindexed = sorted((on_disk | set(backlog)) - indexed)

    healthy = not (dead_links or orphans or backlog or unindexed)
    return _ok({
        "healthy": healthy,
        "inbox_backlog": backlog,
        "orphans_not_in_index_md": orphans,
        "dead_links_in_index_md": dead_links,
        "files_not_in_search_index": unindexed,
        "summary": {
            "backlog": len(backlog), "orphans": len(orphans),
            "dead_links": len(dead_links), "unindexed": len(unindexed),
        },
    })


def main():
    """CLI Entry point for MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
