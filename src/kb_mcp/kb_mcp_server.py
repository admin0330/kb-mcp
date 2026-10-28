#!/usr/bin/env python3
"""Knowledge-Base MCP server (retrieval + index-aware file operations).

Runs over stdio for Claude Desktop. Everything is scoped to a single external
volume (``KB_VOLUME``). The server is deliberately *mount-tolerant*: it starts
even when the drive is unplugged and every tool returns a structured
"drive unavailable" payload instead of crashing the MCP client.

Status-related guarantees
-------------------------
* ``kb_status`` never raises. It reports a precise drive state (see
  :mod:`health`), index statistics and background-job progress, and it
  answers within a few seconds even when the drive hangs.
* Blocking work runs in worker threads, so ``kb_status`` stays responsive
  while a long ``kb_reindex`` or a big PDF extraction is running.
* No SQLite connection is kept across calls, so unplugging and re-plugging
  the drive never leaves the server stuck on "disk I/O error".
* ``kb_reindex`` runs as a background job: on a large drive the call returns
  after ``wait_seconds`` while indexing continues; progress shows up in
  ``kb_status``.

Environment variables
----------------------
KB_VOLUME            Knowledge-base root  (default /Volumes/KnowledgeBase/Database)
KB_ENABLE_SEMANTIC   "1" to enable FastEmbed semantic search (default off)
KB_EMBED_MODEL       FastEmbed model name (default intfloat/multilingual-e5-small)
KB_PROBE_TIMEOUT     Seconds before a drive check counts as unresponsive (default 4)
KB_REQUIRE_EXTERNAL  "0" to allow a KB_VOLUME under /Volumes that is not a mount
"""
from __future__ import annotations

import functools
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

import anyio
from mcp.server.fastmcp import FastMCP

# Make sibling modules importable whether launched by path or as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from . import health  # noqa: E402
    from .indexer import Indexer, IndexStats, META_DIRNAME, NoIndexYet  # noqa: E402
    from .extractors import ExtractionError, extract_text, is_supported  # noqa: E402
except ImportError:
    import health  # type: ignore  # noqa: E402
    from indexer import Indexer, IndexStats, META_DIRNAME, NoIndexYet  # type: ignore  # noqa: E402
    from extractors import ExtractionError, extract_text, is_supported  # type: ignore  # noqa: E402

__version__ = "0.2.0"

VOLUME = Path(os.environ.get("KB_VOLUME", "/Volumes/KnowledgeBase/Database"))
ENABLE_SEMANTIC = os.environ.get("KB_ENABLE_SEMANTIC", "0") == "1"
EMBED_MODEL = os.environ.get("KB_EMBED_MODEL", "intfloat/multilingual-e5-small")
PROBE_TIMEOUT = float(os.environ.get("KB_PROBE_TIMEOUT", "4"))
STATUS_INDEX_TIMEOUT = 6.0     # seconds kb_status waits for index statistics
REINDEX_MAX_WAIT = 50          # keep below typical client request timeouts (60s)

CATEGORY_DIRS = ["_Inbox", "01_Documents", "02_Books", "03_Notes", "04_Archive"]
INDEX_FILENAME = "INDEX.md"
LOG_RELPATH = f"{META_DIRNAME}/organize.log"

_STARTED_AT = time.time()


def _stderr(message: str) -> None:
    """Diagnostics for the client's MCP log (stdout is the protocol channel)."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[kb-retrieval {stamp}] {message}", file=sys.stderr, flush=True)


class _StateLogger:
    """Writes one stderr line whenever the drive state changes."""

    def __init__(self) -> None:
        self.last: Optional[str] = None

    def __call__(self, probe: dict) -> None:
        state = probe.get("state")
        if state != self.last:
            _stderr(f"drive state: {self.last or 'unknown'} -> {state}"
                    + (f" ({probe.get('reason')})" if probe.get("reason") else ""))
            self.last = state


_state_logger = _StateLogger()


class _LoggingProber(health.Prober):
    def probe(self, write_test: bool = False, use_cache: bool = True) -> dict:
        result = super().probe(write_test=write_test, use_cache=use_cache)
        _state_logger(result)
        return result


mcp = FastMCP("kb-retrieval")
_prober = _LoggingProber(VOLUME, timeout=PROBE_TIMEOUT)
_indexer = Indexer(VOLUME, enable_semantic=ENABLE_SEMANTIC, embed_model=EMBED_MODEL)

_READ_STATES = (health.OK, health.READ_ONLY, health.DISK_FULL)
_WRITE_STATES = (health.OK,)


# --------------------------------------------------------------------------- #
# Infrastructure helpers
# --------------------------------------------------------------------------- #
def _get_indexer() -> Indexer:
    return _indexer


def _mounted() -> bool:
    """Back-compat helper: ``True`` when the drive is fully usable."""
    return _prober.is_ok()


def _json(payload: dict) -> str:
    return json.dumps({k: v for k, v in payload.items() if v is not None},
                      ensure_ascii=False, indent=2)


def _ok(payload: dict) -> str:
    payload.setdefault("ok", True)
    return _json(payload)


def _err(error: str, message: str, **extra: Any) -> str:
    payload = {"ok": False, "error": error, "message": message}
    payload.update(extra)
    return _json(payload)


def _drive_error(probe: dict, during: Optional[str] = None) -> str:
    state = probe.get("state")
    code = ("drive_not_mounted"
            if state in (health.NOT_MOUNTED, health.PHANTOM_DIR, health.KB_DIR_MISSING)
            else "drive_unavailable")
    what = f" during {during}" if during else ""
    return _err(code,
                f"Knowledge-base drive is not usable{what} (state={state}). "
                f"{probe.get('reason', '')}".strip(),
                state=state, volume=str(VOLUME), hint=probe.get("hint") or None,
                candidates=probe.get("candidates") or None)


def _require_mount(write: bool = True) -> Optional[str]:
    """Return a JSON error string when the drive is unusable, else ``None``."""
    probe = _prober.probe()
    if probe["state"] in (_WRITE_STATES if write else _READ_STATES):
        return None
    return _drive_error(probe)


def _guarded(fn: Callable[..., str], write: bool, args: tuple, kwargs: dict) -> str:
    """Run a tool body with a drive check and uniform error reporting."""
    err = _require_mount(write=write)
    if err:
        return err
    try:
        result = fn(*args, **kwargs)
        # A tool that failed (or found nothing) may have lost the drive after
        # the pre-check: report the real cause instead of "not found".
        if '"ok": false' in result[:200] or getattr(fn, "_kb_empty_means_check", False) \
                and '"count": 0' in result:
            probe = _prober.probe(use_cache=False)
            if probe["state"] not in _READ_STATES:
                return _drive_error(probe, during=fn.__name__)
        return result
    except NoIndexYet:
        return _err("index_missing", "The search index has not been built yet.",
                    hint="先运行 kb_reindex 建立索引。")
    except sqlite3.Error as exc:
        probe = _prober.probe(use_cache=False)
        if probe["state"] not in _READ_STATES:
            return _drive_error(probe, during=fn.__name__)
        return _err("index_error", f"{type(exc).__name__}: {exc}",
                    hint="索引库读写失败。刚拔插过硬盘的话重试一次即可；"
                         "反复出现可运行 kb_reindex(full=True) 重建索引。")
    except OSError as exc:
        probe = _prober.probe(use_cache=False)
        if probe["state"] not in _READ_STATES:
            return _drive_error(probe, during=fn.__name__)
        return _err("io_error", f"{type(exc).__name__}: {exc}",
                    path=getattr(exc, "filename", None))
    except Exception as exc:  # noqa: BLE001 - a tool must answer, not crash
        _stderr(f"{fn.__name__} failed: {type(exc).__name__}: {exc}")
        return _err("internal_error", f"{type(exc).__name__}: {exc}")


def kb_tool(*, write: bool) -> Callable[[Callable[..., str]], Callable[..., Any]]:
    """Register a tool whose (blocking) body runs in a worker thread.

    FastMCP runs plain ``def`` tools directly on the event loop, so one slow
    call (re-index, large PDF) would block every other request, including
    ``kb_status``. Wrapping the body in ``anyio.to_thread`` keeps the loop free.
    """
    def decorate(fn: Callable[..., str]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            return await anyio.to_thread.run_sync(
                functools.partial(_guarded, fn, write, args, kwargs))
        return mcp.tool()(wrapper)
    return decorate


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


def _log(message: str) -> None:
    try:
        log_path = VOLUME / LOG_RELPATH
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except OSError:
        pass  # logging must never break a tool call


def _fmt_ts(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None


# --------------------------------------------------------------------------- #
# Background re-index job
# --------------------------------------------------------------------------- #
def _reindex_payload(stats: IndexStats) -> dict:
    return {
        "scanned": stats.scanned, "indexed": stats.indexed,
        "skipped_unchanged": stats.skipped_unchanged, "removed": stats.removed,
        "failed": stats.failed, "seconds": stats.seconds,
        "errors": stats.errors[:20],
        "walk_errors": stats.walk_errors[:20] or None,
        "aborted": stats.aborted,
        "removal_skipped": stats.removal_skipped,
    }


class _ReindexJob:
    """One re-index at a time, running in its own thread; observable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "idle"            # idle | running | done | aborted | failed
        self.full = False
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.live: Optional[IndexStats] = None
        self.current: Optional[str] = None
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.done = threading.Event()
        self.done.set()

    def start(self, full: bool) -> bool:
        with self._lock:
            if self.state == "running":
                return False
            self.state, self.full = "running", full
            self.started_at, self.finished_at = time.time(), None
            self.live, self.current, self.result, self.error = None, None, None, None
            self.done = threading.Event()
            threading.Thread(target=self._run, args=(full, self.done),
                             name="kb-reindex", daemon=True).start()
            return True

    def _run(self, full: bool, done: threading.Event) -> None:
        def progress(stats: IndexStats, rel: str) -> None:
            self.live, self.current = stats, rel

        def should_abort() -> Optional[str]:
            probe = _prober.probe()
            return None if probe["state"] == health.OK else f"drive {probe['state']}"

        try:
            stats = _indexer.reindex(full=full, progress=progress,
                                     should_abort=should_abort)
            self.live, self.result = stats, _reindex_payload(stats)
            self.state = "aborted" if stats.aborted else "done"
            _log(f"reindex full={full}: indexed={stats.indexed} removed={stats.removed} "
                 f"failed={stats.failed} in {stats.seconds}s"
                 + (f" (aborted: {stats.aborted})" if stats.aborted else ""))
        except Exception as exc:  # noqa: BLE001
            probe = _prober.probe(use_cache=False)
            self.error = f"{type(exc).__name__}: {exc}"
            if probe["state"] != health.OK:
                self.error += f" (drive {probe['state']})"
            self.state = "failed"
            _stderr(f"reindex failed: {self.error}")
        finally:
            self.finished_at = time.time()
            done.set()

    def snapshot(self) -> dict:
        live = self.live
        end = self.finished_at or time.time()
        snap = {
            "state": self.state,
            "full": self.full if self.started_at else None,
            "started_at": _fmt_ts(self.started_at),
            "finished_at": _fmt_ts(self.finished_at),
            "elapsed_s": round(end - self.started_at, 1) if self.started_at else None,
            "error": self.error,
        }
        if self.state == "running":
            snap["progress"] = {
                "scanned": live.scanned if live else 0,
                "indexed": live.indexed if live else 0,
                "skipped_unchanged": live.skipped_unchanged if live else 0,
                "failed": live.failed if live else 0,
                "current": self.current,
            }
        elif self.result is not None:
            snap["result"] = {k: v for k, v in self.result.items() if v is not None}
        return {k: v for k, v in snap.items() if v is not None}


_job = _ReindexJob()


# --------------------------------------------------------------------------- #
# Status & retrieval tools
# --------------------------------------------------------------------------- #
def _status_index_part() -> dict:
    out: dict = {"categories": [d for d in CATEGORY_DIRS if (VOLUME / d).is_dir()]}
    try:
        out["index"] = _indexer.stats()
    except Exception as exc:  # noqa: BLE001
        out["index"] = {
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "索引库暂时读不了（可能正被重建任务写入，或文件损坏）。"
                    "稍后重试；持续失败可运行 kb_reindex(full=True)。",
        }
    return out


@mcp.tool()
async def kb_status() -> str:
    """Report the knowledge-base drive state, index statistics and job progress.

    Always answers (never errors) within a few seconds. ``state`` is one of:
    ok, not_mounted, phantom_dir, kb_dir_missing, permission_denied, read_only,
    disk_full, unresponsive, io_error. When it is not ``ok``, ``hint`` says
    what the user should do.
    """
    try:
        probe = await anyio.to_thread.run_sync(
            functools.partial(_prober.probe, write_test=True, use_cache=False))
        payload: dict = {
            "ok": True,
            "mounted": probe["mounted"],
            "volume": str(VOLUME),
            "state": probe["state"],
            "writable": probe["writable"],
        }
        if probe["state"] != health.OK:
            payload["message"] = (
                "Drive not mounted. All data tools are paused."
                if not probe["mounted"] else
                f"Drive is mounted but not fully usable ({probe['state']}).")
            payload["hint"] = probe.get("hint")
            payload["reason"] = probe.get("reason")
            payload["candidates"] = probe.get("candidates") or None
        if probe["readable"]:
            extra = None
            with anyio.move_on_after(STATUS_INDEX_TIMEOUT):
                extra = await anyio.to_thread.run_sync(
                    _status_index_part, abandon_on_cancel=True)
            if extra is None:
                extra = {"index": {
                    "error": f"index statistics did not return within "
                             f"{STATUS_INDEX_TIMEOUT:.0f}s",
                    "hint": "硬盘响应很慢，稍后再查一次状态。"}}
            payload.update(extra)
        payload["drive"] = {
            "readable": probe["readable"],
            "checks": probe.get("checks"),
            "state_since": _fmt_ts(probe.get("state_since")),
            "last_ok_at": _fmt_ts(probe.get("last_ok_at")),
        }
        payload["jobs"] = {"reindex": _job.snapshot()}
        payload["server"] = {
            "name": "kb-retrieval", "version": __version__, "pid": os.getpid(),
            "uptime_s": round(time.time() - _STARTED_AT),
            "semantic_enabled": ENABLE_SEMANTIC,
        }
        return _json(payload)
    except Exception as exc:  # noqa: BLE001 - status must never fail
        return _json({
            "ok": True, "mounted": False, "state": "status_error",
            "volume": str(VOLUME),
            "message": f"status check failed: {type(exc).__name__}: {exc}",
        })


def _empty_means_check(fn: Callable[..., str]) -> Callable[..., str]:
    fn._kb_empty_means_check = True  # type: ignore[attr-defined]
    return fn


@kb_tool(write=False)
@_empty_means_check
def kb_search(query: str, top_k: int = 8, mode: str = "hybrid") -> str:
    """Search the knowledge base and return the most relevant text chunks.

    :param query: Natural-language question or keywords (Chinese/English).
    :param top_k: Number of chunks to return (1-30).
    :param mode: "keyword" (BM25/substring), "semantic" (embeddings), or
        "hybrid" (both, rank-fused). "hybrid" is recommended.
    """
    top_k = max(1, min(int(top_k), 30))
    hits = _get_indexer().search(query, top_k=top_k, mode=mode)
    payload = {
        "query": query, "mode": mode, "count": len(hits),
        "results": [
            {"path": h.path, "chunk": h.chunk_id, "score": round(h.score, 4),
             "source": h.source, "snippet": h.snippet}
            for h in hits
        ],
    }
    if _job.state == "running":
        payload["note"] = "A reindex is running; results may be incomplete."
    return _ok(payload)


@mcp.tool()
async def kb_reindex(full: bool = False, wait_seconds: int = 40) -> str:
    """Scan the volume and update the search index incrementally.

    The job runs in the background. This call waits up to ``wait_seconds``
    (0-50) and returns the result if it finished; otherwise it returns the
    current progress and indexing continues -- poll ``kb_status`` to follow it.

    :param full: When true, re-extract every file instead of only changed ones.
    :param wait_seconds: How long to wait for completion before returning.
    """
    probe = await anyio.to_thread.run_sync(_prober.probe)
    if probe["state"] != health.OK:
        return _drive_error(probe)
    if not _job.start(bool(full)):
        return _ok({"running": True,
                    "message": "A reindex is already running; poll kb_status for progress.",
                    "job": _job.snapshot()})
    wait = max(0, min(int(wait_seconds), REINDEX_MAX_WAIT))
    finished = await anyio.to_thread.run_sync(_job.done.wait, wait)
    snap = _job.snapshot()
    if not finished:
        return _ok({"running": True,
                    "message": f"Still indexing after {wait}s; the job continues in the "
                               "background. Call kb_status to see progress.",
                    "job": snap})
    if _job.state == "failed":
        return _err("reindex_failed", _job.error or "unknown error", job=snap)
    result = dict(_job.result or {})
    result["job_state"] = _job.state
    if _job.state == "aborted":
        result["hint"] = "硬盘在重建过程中断开，已完成的部分已保存；插好后再运行一次 kb_reindex。"
    return _ok(result)


@kb_tool(write=False)
def kb_read_document(relpath: str, max_chars: int = 6000) -> str:
    """Return the extracted plain text of one document (for quoting/summarising).

    :param relpath: Path relative to the volume root, e.g. ``_Inbox/foo.pdf``.
    :param max_chars: Maximum characters to return (1-50000).
    """
    try:
        path = _safe(relpath)
    except ValueError as exc:
        return _err("bad_path", str(exc))
    if not path.is_file():
        return _err("not_found", str(relpath))
    if not is_supported(path):
        return _err("unsupported", path.suffix)
    try:
        text = extract_text(path, max_chars=max(1, min(int(max_chars), 50000)))
    except ExtractionError as exc:
        return _err("extract_failed", str(exc))
    return _ok({"path": relpath, "chars": len(text), "text": text})


# --------------------------------------------------------------------------- #
# Navigation & file-operation tools (index-aware)
# --------------------------------------------------------------------------- #
@kb_tool(write=False)
def kb_list_tree(subdir: str = ".", depth: int = 2) -> str:
    """List the directory tree under ``subdir`` (depth-limited)."""
    try:
        base = _safe(subdir)
    except ValueError as exc:
        return _err("bad_path", str(exc))
    if not base.is_dir():
        return _err("not_a_dir", subdir)
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
                try:
                    size = f"{entry.stat().st_size / 1024:.0f} KB"
                except OSError:
                    size = "? KB"
                lines.append(f"{indent}{entry.name} ({size})")

    walk(base, 0)
    return _ok({"subdir": subdir, "tree": "\n".join(lines)})


@kb_tool(write=False)
def kb_scan_inbox(preview_chars: int = 600) -> str:
    """List files in ``_Inbox`` with a short content preview for triage.

    Use the previews to decide a target category and a new name, then call
    :func:`kb_move_file`.
    """
    inbox = VOLUME / "_Inbox"
    if not inbox.is_dir():
        return _err("no_inbox", "_Inbox missing; run the init script.")
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


@kb_tool(write=True)
def kb_create_directory(relpath: str) -> str:
    """Create a directory (and parents) under the volume root."""
    try:
        target = _safe(relpath)
    except ValueError as exc:
        return _err("bad_path", str(exc))
    target.mkdir(parents=True, exist_ok=True)
    _log(f"mkdir {relpath}")
    return _ok({"created": _rel(target)})


def _sync_index_rename(old_rel: str, new_rel: str) -> bool:
    try:
        _get_indexer().rename_in_index(old_rel, new_rel)
        return True
    except Exception as exc:  # noqa: BLE001 - index drift is recoverable via reindex
        _stderr(f"index rename {old_rel} -> {new_rel} failed: {exc}")
        return False


@kb_tool(write=True)
def kb_move_file(src_rel: str, dest_dir_rel: str, new_name: str = "") -> str:
    """Move (and optionally rename) a file into a category directory.

    The search index is updated in place — no reindex needed afterwards.

    :param src_rel: Current path, e.g. ``_Inbox/report.pdf``.
    :param dest_dir_rel: Target directory, e.g. ``01_Documents``.
    :param new_name: Optional new filename; keeps the original when empty.
    """
    try:
        src = _safe(src_rel)
        dest_dir = _safe(dest_dir_rel)
    except ValueError as exc:
        return _err("bad_path", str(exc))
    if not src.is_file():
        return _err("not_found", src_rel)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / (new_name.strip() or src.name)
    if target.exists():
        stem, suffix = target.stem, target.suffix
        target = dest_dir / f"{stem}_{int(time.time())}{suffix}"
    old_rel = _rel(src)
    shutil.move(str(src), str(target))
    new_rel = _rel(target)
    synced = _sync_index_rename(old_rel, new_rel)
    _log(f"move {old_rel} -> {new_rel}")
    return _ok({"from": old_rel, "to": new_rel,
                "index_synced": synced if not synced else None})


@kb_tool(write=True)
def kb_rename_file(src_rel: str, new_name: str) -> str:
    """Rename a file in place (same directory). Index is kept in sync."""
    try:
        src = _safe(src_rel)
    except ValueError as exc:
        return _err("bad_path", str(exc))
    if not src.is_file():
        return _err("not_found", src_rel)
    target = src.parent / new_name.strip()
    if target.exists():
        return _err("exists", new_name)
    old_rel = _rel(src)
    src.rename(target)
    new_rel = _rel(target)
    synced = _sync_index_rename(old_rel, new_rel)
    _log(f"rename {old_rel} -> {new_rel}")
    return _ok({"from": old_rel, "to": new_rel,
                "index_synced": synced if not synced else None})


@kb_tool(write=True)
def kb_log(message: str) -> str:
    """Append a line to the organize log (``.mcp_meta/organize.log``)."""
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


@kb_tool(write=True)
def kb_update_index_md() -> str:
    """Regenerate the root ``INDEX.md`` from the current directory contents.

    Writes atomically (temp file + rename) so an interrupted run never leaves a
    truncated index.
    """
    content = _build_index_md()
    index_path = VOLUME / INDEX_FILENAME
    tmp = index_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, index_path)
    _log("INDEX.md regenerated")
    return _ok({"index": INDEX_FILENAME, "bytes": len(content.encode("utf-8"))})


@kb_tool(write=False)
def kb_health_check() -> str:
    """Audit the knowledge base for common problems.

    Reports: files still in ``_Inbox`` (backlog), files on disk that are not
    referenced in ``INDEX.md`` (orphans), ``INDEX.md`` links that point to
    missing files (dead links), and files not yet in the search index.
    """
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
    payload = {
        "healthy": healthy,
        "inbox_backlog": backlog,
        "orphans_not_in_index_md": orphans,
        "dead_links_in_index_md": dead_links,
        "files_not_in_search_index": unindexed,
        "summary": {
            "backlog": len(backlog), "orphans": len(orphans),
            "dead_links": len(dead_links), "unindexed": len(unindexed),
        },
    }
    if _job.state == "running":
        payload["note"] = "A reindex is running; 'files_not_in_search_index' may shrink."
    return _ok(payload)


def main() -> None:
    """CLI entry point for the MCP server."""
    probe = _prober.probe(use_cache=False)
    _stderr(f"v{__version__} starting; volume={VOLUME} state={probe['state']}")
    if probe["state"] == health.OK:
        try:
            mode = _indexer.prepare()
            if mode:
                _stderr(f"index journal_mode={mode}")
        except Exception as exc:  # noqa: BLE001 - retried on first use
            _stderr(f"index prepare skipped: {type(exc).__name__}: {exc}")
    mcp.run()


if __name__ == "__main__":
    main()
