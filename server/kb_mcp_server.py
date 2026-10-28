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

Convenience
-----------
* ``kb_add_note`` / ``kb_import_file`` add material in one call; it is
  searchable immediately (no full re-index) and exact duplicates are caught.
* ``kb_organize`` moves many files at once; ``kb_trash`` is a recoverable
  delete; ``INDEX.md`` refreshes itself shortly after every change.
* When the drive (re)appears, an incremental re-index runs in the background
  so files copied in with Finder become searchable on their own.

Environment variables
----------------------
KB_VOLUME            Knowledge-base root  (default /Volumes/KnowledgeBase/Database)
KB_ENABLE_SEMANTIC   "1" to enable FastEmbed semantic search (default off)
KB_EMBED_MODEL       FastEmbed model name (default intfloat/multilingual-e5-small)
KB_PROBE_TIMEOUT     Seconds before a drive check counts as unresponsive (default 4)
KB_REQUIRE_EXTERNAL  "0" to allow a KB_VOLUME under /Volumes that is not a mount
KB_AUTO_REINDEX      "0" to disable the background re-index on start / re-plug
KB_IMPORT_DIRS       Extra folders kb_import_file may read from (":"-separated)
"""
from __future__ import annotations

import collections
import functools
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import anyio
from mcp.server.fastmcp import FastMCP

# Make sibling modules importable whether launched by path or as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from . import health  # noqa: E402
    from .indexer import (Indexer, IndexStats, META_DIRNAME, NoIndexYet,  # noqa: E402
                          TRASH_DIRNAME, file_sha256)
    from .extractors import ExtractionError, extract_text, is_supported  # noqa: E402
except ImportError:
    import health  # type: ignore  # noqa: E402
    from indexer import (Indexer, IndexStats, META_DIRNAME, NoIndexYet,  # type: ignore  # noqa: E402
                         TRASH_DIRNAME, file_sha256)
    from extractors import ExtractionError, extract_text, is_supported  # type: ignore  # noqa: E402

__version__ = "0.3.1"

VOLUME = Path(os.environ.get("KB_VOLUME", "/Volumes/KnowledgeBase/Database"))
ENABLE_SEMANTIC = os.environ.get("KB_ENABLE_SEMANTIC", "0") == "1"
EMBED_MODEL = os.environ.get("KB_EMBED_MODEL", "intfloat/multilingual-e5-small")
PROBE_TIMEOUT = float(os.environ.get("KB_PROBE_TIMEOUT", "4"))
AUTO_REINDEX = os.environ.get("KB_AUTO_REINDEX", "1") != "0"
STATUS_INDEX_TIMEOUT = 6.0     # seconds kb_status waits for index statistics
REINDEX_MAX_WAIT = 50          # keep below typical client request timeouts (60s)
INDEX_MD_DELAY = 2.0           # debounce for automatic INDEX.md refreshes
MAX_IMPORT_FILES = 1000
MAX_IMPORT_BYTES = 2 * 1024 ** 3
MAX_TREE_LINES = 600
MAX_NAME_BYTES = 200           # APFS allows 255; leave room for "_2" suffixes

CATEGORY_DIRS = ["_Inbox", "01_Documents", "02_Books", "03_Notes", "04_Archive"]
INDEX_FILENAME = "INDEX.md"
LOG_RELPATH = f"{META_DIRNAME}/organize.log"
_RESERVED_TOP = {META_DIRNAME, TRASH_DIRNAME, "mcp"}

_STARTED_AT = time.time()

INSTRUCTIONS = """\
Personal knowledge base on an external drive (folders: _Inbox, 01_Documents,
02_Books, 03_Notes, 04_Archive). Paths in tools are relative to the drive root.
- Save text the user gives you (notes, pasted data, summaries, web clips):
  kb_add_note -- one call, searchable immediately, duplicates are caught.
- Bring files/folders from the Mac (Downloads, Desktop, Documents, ~/Claude):
  kb_import_file.
- Organise: kb_scan_inbox, then ONE kb_organize call with all moves.
  Name files YYYY-MM-DD_主题_关键词.ext. Delete = kb_trash (recoverable).
- Find: kb_search (space-separated words must all match; folder= narrows),
  then kb_read_document for full text (page with start_char).
- If a tool reports drive_not_mounted / drive_unavailable, relay its "hint";
  kb_status explains the drive state. No restart is ever needed after plugging
  the drive in.
"""


def _stderr(message: str) -> None:
    """Diagnostics for the client's MCP log (stdout is the protocol channel)."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[kb-retrieval {stamp}] {message}", file=sys.stderr, flush=True)


class _StateLogger:
    """Logs drive-state changes and starts a catch-up re-index on re-plug."""

    def __init__(self) -> None:
        self.last: Optional[str] = None

    def __call__(self, probe: dict) -> None:
        state = probe.get("state")
        if state == self.last:
            return
        previous, self.last = self.last, state
        _stderr(f"drive state: {previous or 'unknown'} -> {state}"
                + (f" ({probe.get('reason')})" if probe.get("reason") else ""))
        if state == health.OK and previous not in (None, health.OK):
            threading.Thread(target=_auto_reindex, args=("remount",),
                             name="kb-auto-reindex", daemon=True).start()


_state_logger = _StateLogger()


class _LoggingProber(health.Prober):
    def probe(self, write_test: bool = False, use_cache: bool = True) -> dict:
        result = super().probe(write_test=write_test, use_cache=use_cache)
        _state_logger(result)
        return result


mcp = FastMCP("kb-retrieval", instructions=INSTRUCTIONS)
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
    except ValueError as exc:
        return _err("bad_request", str(exc))
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


def _empty_means_check(fn: Callable[..., str]) -> Callable[..., str]:
    fn._kb_empty_means_check = True  # type: ignore[attr-defined]
    return fn


def _safe(rel: str) -> Path:
    """Resolve ``rel`` under the volume root, refusing path-traversal escapes."""
    rel = (rel or "").strip()
    if rel.startswith(str(VOLUME)):
        rel = rel[len(str(VOLUME)):]  # accept absolute paths on the drive too
    rel = rel.lstrip("/")
    target = (VOLUME / rel).resolve()
    root = VOLUME.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path escapes volume root: {rel}")
    return target


def _rel(path: Path) -> str:
    return str(path.resolve().relative_to(VOLUME.resolve()))


def _top_dir(path: Path) -> str:
    try:
        parts = Path(_rel(path)).parts
    except ValueError:
        return ""
    return parts[0] if parts else ""


def _user_dir(rel: str) -> Path:
    """A folder for user material: never the drive root's tooling folders."""
    target = _safe(rel or "_Inbox")
    if target == VOLUME.resolve() or _top_dir(target) in _RESERVED_TOP:
        raise ValueError(f"not a content folder: {rel!r} (use _Inbox or a category)")
    return target


def _clean_name(name: str, keep_ext: str = "") -> str:
    """A safe, tidy file name: no slashes/colons/control chars, bounded length."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r"[\x00-\x1f\x7f/\\:]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    stem, ext = os.path.splitext(name)
    if ext and not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
        stem, ext = name, ""  # "v1.2 说明": the dot is part of the title
    if keep_ext and not ext:
        ext = keep_ext
    while stem and len((stem + ext).encode("utf-8")) > MAX_NAME_BYTES:
        stem = stem[:-1]
    stem = stem.rstrip(" ._") or "untitled"
    return stem + ext


def _unique_target(directory: Path, name: str) -> Path:
    target = directory / name
    if not target.exists():
        return target
    stem, ext = os.path.splitext(name)
    n = 2
    while (directory / f"{stem}_{n}{ext}").exists():
        n += 1
    return directory / f"{stem}_{n}{ext}"


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


def _index_now(rels: List[str]) -> dict:
    """Make new/moved files searchable right away; never fails the caller."""
    if not rels:
        return {"indexed": 0}
    try:
        st = _indexer.index_paths(rels)
        out = {"indexed": st.indexed, "failed": st.failed or None,
               "errors": st.errors[:5] or None}
        return {k: v for k, v in out.items() if v is not None}
    except Exception as exc:  # noqa: BLE001 - the file operation itself succeeded
        _stderr(f"immediate indexing failed: {exc}")
        return {"indexed": 0, "error": f"{type(exc).__name__}: {exc}",
                "hint": "文件已保存；稍后运行 kb_reindex 即可补上索引。"}


# --------------------------------------------------------------------------- #
# INDEX.md (debounced automatic refresh)
# --------------------------------------------------------------------------- #
def _build_index_md() -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# 📚 KnowledgeBase Index",
        "",
        f"> Auto-generated by kb-mcp on {today}. Do not edit by hand below this line; "
        "it refreshes automatically after every change.",
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


def _write_index_md() -> int:
    content = _build_index_md()
    index_path = VOLUME / INDEX_FILENAME
    tmp = index_path.with_suffix(f".md.tmp{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, index_path)
    return len(content.encode("utf-8"))


class _IndexMdRefresher:
    """Regenerates INDEX.md a moment after the last change (one write per burst)."""

    def __init__(self, delay: float = INDEX_MD_DELAY) -> None:
        self.delay = delay
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self.pending = False
        self.last_written: Optional[float] = None
        self.last_error: Optional[str] = None

    def schedule(self) -> None:
        with self._lock:
            self.pending = True
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        self.flush()

    def flush(self) -> Optional[int]:
        """Write now if the drive is usable. Returns bytes written or None."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if _prober.probe()["state"] != health.OK:
            self.last_error = "drive not writable; will refresh after the next change"
            return None
        try:
            size = _write_index_md()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            _stderr(f"INDEX.md refresh failed: {self.last_error}")
            return None
        self.pending, self.last_error, self.last_written = False, None, time.time()
        _log("INDEX.md regenerated")
        return size

    def snapshot(self) -> dict:
        snap = {"pending": self.pending, "last_written": _fmt_ts(self.last_written),
                "last_error": self.last_error}
        return {k: v for k, v in snap.items() if v is not None}


_refresher = _IndexMdRefresher()


# --------------------------------------------------------------------------- #
# Background re-index job
# --------------------------------------------------------------------------- #
def _reindex_payload(stats: IndexStats) -> dict:
    return {
        "scanned": stats.scanned, "indexed": stats.indexed,
        "skipped_unchanged": stats.skipped_unchanged, "removed": stats.removed,
        "failed": stats.failed, "hashed": stats.hashed or None, "seconds": stats.seconds,
        "errors": stats.errors[:20],
        "walk_errors": stats.walk_errors[:20] or None,
        "aborted": stats.aborted,
        "removal_skipped": stats.removal_skipped,
    }


def _lock_reindex():
    """Cross-process lock so two Claude windows don't index the drive at once."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        return None, True
    try:
        path = VOLUME / META_DIRNAME / "reindex.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
    except OSError:
        return None, True  # can't lock (e.g. odd filesystem): just proceed
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh, True
    except OSError:
        fh.close()
        return None, False


class _ReindexJob:
    """One re-index at a time, running in its own thread; observable."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "idle"            # idle | running | done | aborted | failed
        self.full = False
        self.trigger: Optional[str] = None
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.live: Optional[IndexStats] = None
        self.current: Optional[str] = None
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.done = threading.Event()
        self.done.set()

    def start(self, full: bool, trigger: str = "manual") -> str:
        """Returns "started", "running" (this process) or "busy" (another one)."""
        with self._lock:
            if self.state == "running":
                return "running"
            lock_fh, acquired = _lock_reindex()
            if not acquired:
                return "busy"
            self.state, self.full, self.trigger = "running", full, trigger
            self.started_at, self.finished_at = time.time(), None
            self.live, self.current, self.result, self.error = None, None, None, None
            self.done = threading.Event()
            threading.Thread(target=self._run, args=(full, self.done, lock_fh),
                             name="kb-reindex", daemon=True).start()
            return "started"

    def _run(self, full: bool, done: threading.Event, lock_fh) -> None:
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
            if stats.indexed or stats.removed:
                _refresher.schedule()
            _log(f"reindex ({self.trigger}) full={full}: indexed={stats.indexed} "
                 f"removed={stats.removed} failed={stats.failed} in {stats.seconds}s"
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
            if lock_fh is not None:
                try:
                    lock_fh.close()
                except OSError:
                    pass
            done.set()

    def snapshot(self) -> dict:
        live = self.live
        end = self.finished_at or time.time()
        snap = {
            "state": self.state,
            "trigger": self.trigger,
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


def _auto_reindex(trigger: str) -> None:
    """Catch up with files copied in by Finder (on start-up and after re-plug)."""
    if not AUTO_REINDEX:
        return
    if _prober.probe()["state"] != health.OK:
        return
    outcome = _job.start(False, trigger=trigger)
    _stderr(f"auto reindex ({trigger}): {outcome}")


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
        payload["jobs"] = {"reindex": _job.snapshot(), "index_md": _refresher.snapshot()}
        payload["server"] = {
            "name": "kb-retrieval", "version": __version__, "pid": os.getpid(),
            "uptime_s": round(time.time() - _STARTED_AT),
            "semantic_enabled": ENABLE_SEMANTIC,
            "auto_reindex": AUTO_REINDEX,
            "import_dirs": [str(p) for p in _import_roots()],
        }
        return _json(payload)
    except Exception as exc:  # noqa: BLE001 - status must never fail
        return _json({
            "ok": True, "mounted": False, "state": "status_error",
            "volume": str(VOLUME),
            "message": f"status check failed: {type(exc).__name__}: {exc}",
        })


@kb_tool(write=False)
@_empty_means_check
def kb_search(query: str, top_k: int = 8, mode: str = "hybrid", folder: str = "") -> str:
    """Search the knowledge base and return the most relevant text chunks.

    Space-separated words must all appear (if nothing has all of them, chunks
    with the most words are returned, source="keyword-partial"). File names
    are matched too (source="filename").

    :param query: Keywords or a question, e.g. "预算 硬件 2026".
    :param top_k: Number of chunks to return (1-30).
    :param mode: "keyword", "semantic" (needs KB_ENABLE_SEMANTIC) or "hybrid".
    :param folder: Optional sub-folder to search in, e.g. "02_Books".
    """
    top_k = max(1, min(int(top_k), 30))
    hits = _get_indexer().search(query, top_k=top_k, mode=mode, folder=folder or None)
    payload = {
        "query": query, "mode": mode, "folder": folder or None, "count": len(hits),
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
    """Scan the whole drive and bring the search index up to date.

    Rarely needed: added/imported/moved files are indexed immediately, and a
    catch-up scan runs by itself whenever the drive is plugged in. Use it after
    copying many files onto the drive with Finder, or with ``full=True`` to
    rebuild. Runs in the background; this call waits up to ``wait_seconds``
    (0-50) and otherwise returns progress -- follow it with ``kb_status``.

    :param full: When true, re-extract every file instead of only changed ones.
    :param wait_seconds: How long to wait for completion before returning.
    """
    probe = await anyio.to_thread.run_sync(_prober.probe)
    if probe["state"] != health.OK:
        return _drive_error(probe)
    outcome = await anyio.to_thread.run_sync(_job.start, bool(full))
    if outcome == "busy":
        return _ok({"running": True,
                    "message": "Another kb-retrieval instance (e.g. a second Claude window) "
                               "is indexing this drive right now; it writes to the same "
                               "index. Try again in a little while."})
    if outcome == "running":
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


_doc_cache: "collections.OrderedDict[Tuple[str, float, int], str]" = collections.OrderedDict()
_doc_cache_lock = threading.Lock()


def _document_text(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    with _doc_cache_lock:
        if key in _doc_cache:
            _doc_cache.move_to_end(key)
            return _doc_cache[key]
    text = extract_text(path)
    with _doc_cache_lock:
        _doc_cache[key] = text
        while len(_doc_cache) > 4:
            _doc_cache.popitem(last=False)
    return text


@kb_tool(write=False)
def kb_read_document(relpath: str, max_chars: int = 6000, start_char: int = 0) -> str:
    """Return the extracted plain text of one document (for quoting/summarising).

    Long documents are paged: when ``next_start_char`` is present, call again
    with ``start_char`` set to it to continue reading.

    :param relpath: Path relative to the volume root, e.g. ``02_Books/foo.pdf``.
    :param max_chars: Maximum characters to return (1-50000).
    :param start_char: Offset to start from (for paging through long texts).
    """
    try:
        path = _safe(relpath)
    except ValueError as exc:
        return _err("bad_path", str(exc))
    if not path.is_file():
        return _err("not_found", str(relpath))
    if not is_supported(path):
        return _err("unsupported", path.suffix or "(no extension)",
                    hint="这类文件只能存放、不能提取文字（例如图片、音视频）。")
    try:
        text = _document_text(path)
    except ExtractionError as exc:
        return _err("extract_failed", str(exc))
    start = max(0, int(start_char))
    end = start + max(1, min(int(max_chars), 50000))
    chunk = text[start:end]
    return _ok({"path": relpath, "total_chars": len(text), "start_char": start,
                "chars": len(chunk), "next_start_char": end if end < len(text) else None,
                "text": chunk})


# --------------------------------------------------------------------------- #
# Adding material
# --------------------------------------------------------------------------- #
def _content_sha(text: str) -> str:
    normalised = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


@kb_tool(write=True)
def kb_add_note(title: str, content: str, category: str = "_Inbox",
                tags: Optional[List[str]] = None, source: str = "", date: str = "",
                allow_duplicate: bool = False) -> str:
    """Save text in one step: notes, pasted data, a summary, a web clip, a chat excerpt.

    Writes ``<category>/<YYYY-MM-DD>_<title>.md`` with a small YAML header,
    makes it searchable immediately and refreshes INDEX.md. If a note with the
    same content already exists, nothing is written and its path is returned
    (``duplicate: true``) unless ``allow_duplicate`` is set.

    :param title: Short title; also used for the file name.
    :param content: The body (Markdown or plain text).
    :param category: Target folder, e.g. "_Inbox", "03_Notes", "01_Documents/财务" (created if missing).
    :param tags: Optional tags, e.g. ["预算", "2026"].
    :param source: Optional origin: a URL, "微信聊天", a book title ...
    :param date: Optional YYYY-MM-DD for the file name (default: today).
    """
    if not (content or "").strip():
        return _err("empty_content", "content is empty")
    dest_dir = _user_dir(category)
    sha = _content_sha(content)
    if not allow_duplicate:
        existing = _indexer.paths_containing(f"content_sha256: {sha}")
        if existing:
            return _ok({"duplicate": True, "existing": existing,
                        "message": "The same content is already in the knowledge base; "
                                   "nothing was written."})
    day = date.strip() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", (date or "").strip()) \
        else datetime.now().strftime("%Y-%m-%d")
    clean_title = re.sub(r"\s+", " ", (title or "").strip()) or "未命名笔记"
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _clean_name(f"{day}_{clean_title}")
    if not name.lower().endswith(".md"):
        name += ".md"
    target = _unique_target(dest_dir, name)
    header = ["---", f"title: {json.dumps(clean_title, ensure_ascii=False)}", f"date: {day}"]
    if tags:
        header.append(f"tags: {json.dumps([str(t) for t in tags], ensure_ascii=False)}")
    if source:
        header.append(f"source: {json.dumps(source, ensure_ascii=False)}")
    header += [f"added: {datetime.now().isoformat(timespec='seconds')}",
               f"content_sha256: {sha}", "---", "", ""]
    data = ("\n".join(header) + content.strip("\n") + "\n").encode("utf-8")
    tmp = target.with_name(f".kbtmp-{os.getpid()}-{target.name}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    rel = _rel(target)
    index = _index_now([rel])
    _refresher.schedule()
    _log(f"add_note {rel}")
    return _ok({"path": rel, "bytes": len(data), "index": index})


def _import_roots() -> List[Path]:
    home = Path.home()
    roots = [home / "Downloads", home / "Desktop", home / "Documents", home / "Claude",
             home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"]
    for extra in os.environ.get("KB_IMPORT_DIRS", "").split(os.pathsep):
        if extra.strip():
            roots.append(Path(os.path.expanduser(extra.strip())))
    out = []
    for r in roots:
        try:
            if r.is_dir():
                out.append(r.resolve())
        except OSError:
            continue
    return out


def _existing_copy(src: Path, sha: str, dest_dir: Path) -> Optional[str]:
    """Path of a file already on the drive with identical bytes, if any."""
    found = _indexer.find_by_hash(sha)
    if found:
        return found[0]
    try:  # unsupported types are not in the index: check the target folder
        size = src.stat().st_size
        if dest_dir.is_dir():
            for p in dest_dir.iterdir():
                if p.is_file() and p.stat().st_size == size and file_sha256(p) == sha:
                    return _rel(p)
    except OSError:
        pass
    return None


@kb_tool(write=True)
def kb_import_file(source_path: str, category: str = "_Inbox", new_name: str = "",
                   move: bool = False, allow_duplicate: bool = False) -> str:
    """Copy a file or a whole folder from this Mac into the knowledge base.

    Allowed sources: ~/Downloads, ~/Desktop, ~/Documents, ~/Claude, iCloud
    Drive, plus folders listed in KB_IMPORT_DIRS. Text-bearing files (PDF,
    Word, PPT, EPUB, Markdown ...) are searchable immediately; other files
    (images, video) are stored and listed in INDEX.md. Files whose exact
    content is already on the drive are skipped (``skipped_duplicates``).

    :param source_path: Absolute path, e.g. "~/Downloads/报告.pdf" or a folder.
    :param category: Target folder on the drive (default "_Inbox").
    :param new_name: Optional new name (a file's extension is kept if omitted).
    :param move: Delete the source after a successful copy (default: keep it).
    :param allow_duplicate: Import even if identical content already exists.
    """
    src = Path(os.path.expanduser((source_path or "").strip()))
    if not src.is_absolute():
        return _err("bad_path", "source_path must be absolute, e.g. ~/Downloads/file.pdf")
    try:
        src = src.resolve()
    except OSError as exc:
        return _err("not_found", str(exc))
    roots = _import_roots()
    if src == VOLUME.resolve() or VOLUME.resolve() in src.parents:
        return _err("already_on_drive", "That path is already on the drive; use kb_move_file.")
    if not any(src == r or r in src.parents for r in roots):
        return _err("source_not_allowed", f"{src} is outside the folders imports may read.",
                    allowed=[str(r) for r in roots],
                    hint="把文件放进「下载」「桌面」或「文稿」后再导入；"
                         "或在启动脚本里设置 KB_IMPORT_DIRS 添加目录。")
    try:
        exists = src.exists()
        is_dir = src.is_dir()
    except PermissionError:
        exists = True
        is_dir = False
    if not exists:
        return _err("not_found", str(src))
    dest_root = _user_dir(category)

    # Plan the copy (source file -> target path).
    plan: List[Tuple[Path, Path]] = []
    try:
        if is_dir:
            base = dest_root / _clean_name(new_name or src.name)
            for dirpath, dirnames, filenames in os.walk(src):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                rel_dir = Path(dirpath).relative_to(src)
                for name in sorted(filenames):
                    if name.startswith("."):
                        continue
                    target_dir = base.joinpath(*[_clean_name(p) for p in rel_dir.parts])
                    plan.append((Path(dirpath) / name, target_dir / _clean_name(name)))
                    if len(plan) > MAX_IMPORT_FILES:
                        return _err("too_many_files",
                                    f"more than {MAX_IMPORT_FILES} files in {src}",
                                    hint="分几次导入子文件夹，或用访达直接拷进 _Inbox 后运行 kb_reindex。")
        else:
            name = _clean_name(new_name, keep_ext=src.suffix) if new_name.strip() \
                else _clean_name(src.name)
            plan.append((src, dest_root / name))
        total = sum(p.stat().st_size for p, _ in plan)
    except PermissionError as exc:
        return _err("source_permission_denied", str(exc),
                    hint="macOS 拒绝读取该位置：系统设置 → 隐私与安全性 → 文件与文件夹，"
                         "允许 Claude 访问“下载”“桌面”“文稿”等文件夹。")
    if total > MAX_IMPORT_BYTES:
        return _err("too_large", f"{total / 1024 ** 3:.1f} GB in one import (limit "
                                 f"{MAX_IMPORT_BYTES / 1024 ** 3:.0f} GB)",
                    hint="大文件请用访达直接拷进硬盘的 _Inbox，再运行 kb_reindex。")
    free = shutil.disk_usage(VOLUME).free
    if total > free:
        return _err("disk_full", f"needs {total / 1024 ** 2:.0f} MB, "
                                 f"only {free / 1024 ** 2:.0f} MB free on the drive")

    imported: List[str] = []
    duplicates: List[dict] = []
    stored_only: List[str] = []
    done_sources: List[Path] = []
    for source, target in plan:
        sha = file_sha256(source)
        if not allow_duplicate:
            dup = _existing_copy(source, sha, target.parent)
            if dup:
                duplicates.append({"source": str(source), "already_at": dup})
                done_sources.append(source)
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        final = _unique_target(target.parent, target.name)
        tmp = final.with_name(f".kbtmp-{os.getpid()}-{final.name}")
        shutil.copy2(source, tmp)
        os.replace(tmp, final)
        rel = _rel(final)
        imported.append(rel)
        done_sources.append(source)
        if not is_supported(final):
            stored_only.append(rel)
    if move:
        for source in done_sources:
            try:
                source.unlink()
            except OSError:
                pass
        if is_dir:
            for dirpath, _, _ in sorted(os.walk(src), key=lambda t: -len(t[0])):
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
    index = _index_now([r for r in imported if r not in stored_only])
    if imported:
        _refresher.schedule()
        _log(f"import {src} -> {len(imported)} file(s) in {_rel(dest_root)}")
    return _ok({
        "imported": imported[:200], "count": len(imported),
        "skipped_duplicates": duplicates[:50] or None,
        "stored_not_searchable": stored_only[:50] or None,
        "index": index, "total_mb": round(total / 1024 ** 2, 2),
        "source_removed": move or None,
    })


# --------------------------------------------------------------------------- #
# Navigation & file-operation tools (index-aware)
# --------------------------------------------------------------------------- #
@kb_tool(write=False)
def kb_list_tree(subdir: str = ".", depth: int = 2) -> str:
    """List the directory tree under ``subdir`` (depth-limited, max 600 lines)."""
    try:
        base = _safe(subdir)
    except ValueError as exc:
        return _err("bad_path", str(exc))
    if not base.is_dir():
        return _err("not_a_dir", subdir)
    depth = max(0, min(int(depth), 6))
    lines: List[str] = []
    hidden = {"lines": 0}

    def walk(d: Path, level: int) -> None:
        if level > depth:
            return
        for entry in sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if entry.name.startswith(".") or entry.name == META_DIRNAME:
                continue
            if len(lines) >= MAX_TREE_LINES:
                hidden["lines"] += 1
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
    return _ok({"subdir": subdir, "tree": "\n".join(lines),
                "truncated": f"{hidden['lines']} more entries not shown; "
                             "list a sub-folder or lower depth" if hidden["lines"] else None})


@kb_tool(write=False)
def kb_scan_inbox(preview_chars: int = 600, limit: int = 30) -> str:
    """List files in ``_Inbox`` with a short content preview for triage.

    Decide a category and a name for each, then move them all with ONE
    :func:`kb_organize` call. Returns at most ``limit`` items per call.
    """
    inbox = VOLUME / "_Inbox"
    if not inbox.is_dir():
        return _err("no_inbox", "_Inbox missing; run the init script.")
    preview_chars = max(100, min(int(preview_chars), 4000))
    limit = max(1, min(int(limit), 200))
    all_files = sorted(p for p in inbox.rglob("*")
                       if p.is_file() and not p.name.startswith(".")
                       and not any(part.startswith(".") for part in p.relative_to(inbox).parts))
    items = []
    for path in all_files[:limit]:
        entry = {"path": _rel(path), "name": path.name,
                 "size_kb": round(path.stat().st_size / 1024, 1),
                 "ext": path.suffix.lower(), "supported": is_supported(path)}
        if is_supported(path):
            try:
                entry["preview"] = extract_text(path, max_chars=preview_chars)[:preview_chars]
            except ExtractionError as exc:
                entry["preview"] = f"<extract failed: {exc}>"
        items.append(entry)
    return _ok({"inbox_count": len(all_files), "items": items,
                "more": len(all_files) - len(items) or None,
                "categories": CATEGORY_DIRS[1:]})


@kb_tool(write=True)
def kb_create_directory(relpath: str) -> str:
    """Create a directory (and parents) under the volume root."""
    target = _user_dir(relpath)
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


def _move_dir(src: Path, dest_dir: Path, new_name: str) -> dict:
    """Move a whole folder (e.g. restoring one from _Trash) and keep the index in step."""
    root = VOLUME.resolve()
    if src == root or _top_dir(src) in {META_DIRNAME, "mcp"} or \
            (src.parent == root and src.name in CATEGORY_DIRS + [TRASH_DIRNAME]):
        raise ValueError(f"{_rel(src) or '/'} is a top-level or system folder")
    if dest_dir == src or src in dest_dir.parents:
        raise ValueError("cannot move a folder into itself")
    name = _clean_name(new_name) if (new_name or "").strip() else src.name
    old_rel = _rel(src)
    if (dest_dir / name).resolve() == src:
        return {"from": old_rel, "to": old_rel, "unchanged": True}
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_target(dest_dir, name)
    shutil.move(str(src), str(target))
    new_rel = _rel(target)
    try:
        _indexer.rename_prefix_in_index(old_rel, new_rel)
    except Exception as exc:  # noqa: BLE001 - reconciled below / by reindex
        _stderr(f"index prefix rename {old_rel} -> {new_rel} failed: {exc}")
    files = [p for p in target.rglob("*")
             if p.is_file() and not any(part.startswith(".") for part in p.relative_to(target).parts)]
    indexed = set(_indexer.indexed_paths())
    _log(f"move {old_rel}/ -> {new_rel}/ ({len(files)} files)")
    out = {"from": old_rel, "to": new_rel, "files": len(files)}
    missing = [_rel(p) for p in files if is_supported(p) and _rel(p) not in indexed]
    if missing:
        out["_index_many"] = missing
    return out


def _move_one(src_rel: str, dest_dir_rel: str, new_name: str = "") -> dict:
    """Move/rename one file or folder and keep the index in step."""
    src = _safe(src_rel)
    dest_dir = _user_dir(dest_dir_rel)
    if src.is_dir():
        return _move_dir(src, dest_dir, new_name)
    if not src.is_file():
        raise FileNotFoundError(f"not found: {src_rel}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _clean_name(new_name, keep_ext=src.suffix) if (new_name or "").strip() else src.name
    target = dest_dir / name
    old_rel = _rel(src)
    if target.resolve() == src.resolve():
        return {"from": old_rel, "to": old_rel, "unchanged": True}
    target = _unique_target(dest_dir, name)
    shutil.move(str(src), str(target))
    new_rel = _rel(target)
    synced = _sync_index_rename(old_rel, new_rel)
    _log(f"move {old_rel} -> {new_rel}")
    out = {"from": old_rel, "to": new_rel}
    if not synced:
        out["index_synced"] = False
    if is_supported(target) and not _indexer.is_indexed(new_rel):
        out["_index"] = new_rel
    return out


def _finish_moves(results: List[dict]) -> dict:
    to_index = [r.pop("_index") for r in results if "_index" in r]
    for r in results:
        to_index.extend(r.pop("_index_many", []))
    if any(not r.get("unchanged") for r in results if r.get("to")):
        _refresher.schedule()
    return _index_now(to_index) if to_index else {}


@kb_tool(write=True)
def kb_move_file(src_rel: str, dest_dir_rel: str, new_name: str = "") -> str:
    """Move (and optionally rename) one file or folder. For many, use kb_organize.

    Also restores items from _Trash. The search index is kept in sync and
    INDEX.md refreshes automatically.

    :param src_rel: Current path, e.g. ``_Inbox/report.pdf`` or a folder.
    :param dest_dir_rel: Target directory, e.g. ``01_Documents``.
    :param new_name: Optional new filename; the extension is kept if omitted.
    """
    try:
        result = _move_one(src_rel, dest_dir_rel, new_name)
    except FileNotFoundError as exc:
        return _err("not_found", str(exc))
    except ValueError as exc:
        return _err("bad_path", str(exc))
    index = _finish_moves([result])
    if index:
        result["index"] = index
    return _ok(result)


@kb_tool(write=True)
def kb_organize(moves: List[Dict[str, str]]) -> str:
    """Move/rename many files in ONE call (e.g. everything from kb_scan_inbox).

    Each item: {"src": "_Inbox/a.pdf", "dest_dir": "02_Books", "new_name":
    "2026-10-28_书名_作者.pdf"} -- new_name is optional. Items are processed
    independently; failures are reported per item. Up to 200 items per call.
    """
    results: List[dict] = []
    for item in list(moves or [])[:200]:
        src = item.get("src") or item.get("src_rel") or ""
        dest = item.get("dest_dir") or item.get("dest_dir_rel") or ""
        try:
            results.append(_move_one(src, dest, item.get("new_name", "")))
        except (OSError, ValueError) as exc:
            results.append({"from": src, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    index = _finish_moves(results)
    failed = [r for r in results if r.get("ok") is False]
    payload = {"moved": len(results) - len(failed), "failed": len(failed),
               "results": results, "index": index or None,
               "skipped": max(0, len(moves or []) - 200) or None}
    if failed:
        probe = _prober.probe(use_cache=False)
        if probe["state"] != health.OK:
            payload["drive"] = {"state": probe["state"], "hint": probe.get("hint")}
    return _ok(payload)


@kb_tool(write=True)
def kb_rename_file(src_rel: str, new_name: str) -> str:
    """Rename a file in place (same directory). Index is kept in sync."""
    try:
        src = _safe(src_rel)
    except ValueError as exc:
        return _err("bad_path", str(exc))
    if not src.is_file():
        return _err("not_found", src_rel)
    name = _clean_name(new_name, keep_ext=src.suffix)
    target = src.parent / name
    if target.exists() and target.resolve() != src.resolve():
        return _err("exists", name)
    old_rel = _rel(src)
    src.rename(target)
    new_rel = _rel(target)
    synced = _sync_index_rename(old_rel, new_rel)
    _refresher.schedule()
    _log(f"rename {old_rel} -> {new_rel}")
    return _ok({"from": old_rel, "to": new_rel,
                "index_synced": synced if not synced else None})


@kb_tool(write=True)
def kb_trash(relpath: str, reason: str = "") -> str:
    """Remove a file or folder from the knowledge base -- recoverably.

    It is moved to ``_Trash/<today>/`` (not indexed, not in INDEX.md). Restore
    with kb_move_file; empty _Trash in Finder when you are sure.

    :param relpath: e.g. "03_Notes/重复的笔记.md".
    :param reason: Optional note for the organize log (e.g. "duplicate of ...").
    """
    try:
        src = _safe(relpath)
    except ValueError as exc:
        return _err("bad_path", str(exc))
    if not src.exists():
        return _err("not_found", relpath)
    if src == VOLUME.resolve() or _top_dir(src) in _RESERVED_TOP or \
            (src.parent == VOLUME.resolve() and src.name in CATEGORY_DIRS):
        return _err("protected", f"{relpath} is a top-level or system folder")
    old_rel = _rel(src)
    if src.is_dir():
        gone = [p for p in _indexer.indexed_paths() if p.startswith(old_rel + "/")]
    else:
        gone = [old_rel]
    day_dir = VOLUME / TRASH_DIRNAME / datetime.now().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_target(day_dir, src.name)
    shutil.move(str(src), str(target))
    removed = _indexer.delete_paths(gone)
    _refresher.schedule()
    new_rel = _rel(target)
    _log(f"trash {old_rel} -> {new_rel}" + (f" ({reason})" if reason else ""))
    return _ok({"from": old_rel, "to": new_rel, "removed_from_index": removed,
                "restore": {"tool": "kb_move_file", "src_rel": new_rel,
                            "dest_dir_rel": str(Path(old_rel).parent)}})


@kb_tool(write=False)
def kb_find_duplicates(limit: int = 30) -> str:
    """List groups of files with identical content (SHA-256), largest first.

    Keep one of each group and remove the others with kb_trash.
    """
    found = _indexer.find_duplicates(limit=max(1, min(int(limit), 200)))
    note = None
    if found["unhashed"]:
        note = (f"{found['unhashed']} older file(s) have no fingerprint yet; "
                "kb_reindex adds them.")
    return _ok({"groups": found["groups"], "count": len(found["groups"]), "note": note})


@kb_tool(write=True)
def kb_log(message: str) -> str:
    """Append a line to the organize log (``.mcp_meta/organize.log``)."""
    _log(message)
    return _ok({"logged": message})


# --------------------------------------------------------------------------- #
# INDEX.md maintenance & health check
# --------------------------------------------------------------------------- #
@kb_tool(write=True)
def kb_update_index_md() -> str:
    """Regenerate the root ``INDEX.md`` now (it also refreshes automatically).

    Writes atomically (temp file + rename) so an interrupted run never leaves a
    truncated index.
    """
    size = _refresher.flush()
    if size is None:
        return _err("index_md_failed", _refresher.last_error or "unknown error")
    return _ok({"index": INDEX_FILENAME, "bytes": size})


def _health_report(max_items: int) -> dict:
    # Files physically present under the managed category dirs (excluding Inbox).
    on_disk = set()
    for cat in CATEGORY_DIRS[1:]:
        cat_dir = VOLUME / cat
        if cat_dir.is_dir():
            for p in cat_dir.rglob("*"):
                if p.is_file() and not p.name.startswith("."):
                    on_disk.add(_rel(p))

    referenced = set()
    index_path = VOLUME / INDEX_FILENAME
    if index_path.is_file():
        text = index_path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\]\(<?([^)>]+)>?\)", text):
            referenced.add(m.group(1).strip())

    inbox = VOLUME / "_Inbox"
    backlog = []
    if inbox.is_dir():
        backlog = sorted(_rel(p) for p in inbox.rglob("*")
                         if p.is_file() and not p.name.startswith("."))

    dead_links = sorted(r for r in referenced
                        if not (VOLUME / r).is_file() and not r.startswith("http"))
    orphans = sorted((on_disk | set(backlog)) - referenced)
    indexed = set(_get_indexer().indexed_paths())
    # Only text-bearing files can be indexed; images etc. are not "missing".
    unindexed = sorted(p for p in (on_disk | set(backlog)) - indexed
                       if is_supported(VOLUME / p))
    return {
        "healthy": not (dead_links or orphans or backlog or unindexed),
        "inbox_backlog": backlog[:max_items],
        "orphans_not_in_index_md": orphans[:max_items],
        "dead_links_in_index_md": dead_links[:max_items],
        "files_not_in_search_index": unindexed[:max_items],
        "summary": {
            "backlog": len(backlog), "orphans": len(orphans),
            "dead_links": len(dead_links), "unindexed": len(unindexed),
        },
        "_unindexed_all": unindexed,
    }


@kb_tool(write=False)
def kb_health_check(fix: bool = False, max_items: int = 100) -> str:
    """Audit the knowledge base; with ``fix=True`` also repair what it can.

    Reports: files still in ``_Inbox`` (backlog), files missing from
    ``INDEX.md`` (orphans), ``INDEX.md`` links to missing files (dead links)
    and text files not yet in the search index. ``fix=True`` indexes the
    missing files and rewrites INDEX.md, then reports again. The inbox backlog
    is left for you to organise.
    """
    max_items = max(1, min(int(max_items), 1000))
    report = _health_report(max_items)
    fixed = None
    if fix:
        if _prober.probe()["state"] != health.OK:
            return _drive_error(_prober.probe(), during="kb_health_check(fix)")
        todo = report["_unindexed_all"]
        index = _index_now(todo) if todo else {"indexed": 0}
        size = _refresher.flush() if (report["summary"]["orphans"]
                                      or report["summary"]["dead_links"]) else None
        fixed = {"indexed": index.get("indexed", 0), "index_md_rewritten": size is not None}
        report = _health_report(max_items)
    report.pop("_unindexed_all", None)
    report["fixed"] = fixed
    if _job.state == "running":
        report["note"] = "A reindex is running; 'files_not_in_search_index' may shrink."
    return _ok(report)


# --------------------------------------------------------------------------- #
# Prompts (Claude Desktop: "+" menu -> kb-retrieval)
# --------------------------------------------------------------------------- #
@mcp.prompt(name="organize_inbox", title="一键整理收件箱",
            description="扫描 _Inbox，命名归档，一次性移动并汇报")
def prompt_organize_inbox() -> str:
    return (
        "请整理我的知识库收件箱：\n"
        "1. 调用 kb_scan_inbox 查看 _Inbox 里的文件和内容预览；\n"
        "2. 为每个文件定一个名字 YYYY-MM-DD_主题_关键词.扩展名（日期优先用内容里的日期，"
        "没有就用今天），并选好分类：01_Documents 文档报告、02_Books 书和手册、"
        "03_Notes 笔记、04_Archive 归档；拿不准的留在 _Inbox 并告诉我；\n"
        "3. 用一次 kb_organize 把所有文件移动到位；\n"
        "4. 如果发现内容重复，用 kb_find_duplicates 确认后用 kb_trash 移走多余的；\n"
        "5. 最后列一张表告诉我每个文件去了哪里。")


@mcp.prompt(name="add_to_kb", title="收录到知识库",
            description="把文字、数据或本机文件收进知识库")
def prompt_add_to_kb(material: str = "") -> str:
    return (
        "请把下面的内容收录到我的知识库：\n"
        f"{material or '（内容见我接下来发的消息）'}\n\n"
        "规则：文字、数据、摘录用 kb_add_note（起一个简短标题，挑合适的分类和标签，"
        "有出处就写 source）；如果给的是我电脑上的文件或文件夹路径，用 kb_import_file。"
        "如果工具提示重复，告诉我已有的是哪一份，不要重复保存。完成后告诉我存放路径。")


@mcp.prompt(name="kb_checkup", title="知识库体检",
            description="检查硬盘状态、索引、INDEX.md 和重复文件")
def prompt_checkup() -> str:
    return (
        "请给我的知识库做一次体检：先 kb_status 看硬盘和索引状态；再 kb_health_check(fix=True) "
        "修复能自动修的问题；然后 kb_find_duplicates 看有没有重复文件。"
        "用几句话汇报结果，需要我决定的事情（比如收件箱积压、重复文件保留哪份）单独列出来。")


@mcp.prompt(name="ask_kb", title="检索知识库",
            description="在知识库里查资料并注明出处")
def prompt_ask(question: str = "") -> str:
    return (
        f"请在我的知识库里查找：{question or '（问题见我接下来发的消息）'}\n"
        "先用 kb_search 搜关键词（多个词用空格分开，可以换几种说法），需要细节时用 "
        "kb_read_document 读原文。回答时注明每个结论来自哪个文件；知识库里没有就直说。")


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
        threading.Thread(target=_auto_reindex, args=("startup",),
                         name="kb-auto-reindex", daemon=True).start()
    mcp.run()


if __name__ == "__main__":
    main()
