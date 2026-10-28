"""Incremental hybrid index over the knowledge-base volume.

Storage lives in a single SQLite database at ``<volume>/.mcp_meta/index.db``:

* ``files``       - one row per indexed file (mtime/size for change detection,
                    sha256 of the bytes for duplicate detection).
* ``chunks``      - FTS5 virtual table (``trigram`` tokenizer so Chinese
                    substring search works) holding the chunked text.
* ``embeddings``  - optional per-chunk float32 vectors for semantic search.

The design goal is *robustness over cleverness*: a single file failing to
extract must never abort a full re-index, and the whole thing must run from a
plain ``pip install`` with no server process or container.

Removable-drive rules (learned the hard way):

* Connections are opened per operation and closed right after. A connection
  kept open across an unplug/replug points at a vanished file and keeps
  failing, so nothing here holds one between calls.
* No memory-mapped I/O. WAL mode memory-maps the ``-shm`` file; when the
  drive is yanked while that mapping is in use, macOS kills the whole process
  with SIGBUS (no exception, nothing to catch). The index therefore uses a
  rollback journal (``journal_mode=DELETE``) and ``mmap_size=0``, which turns
  a yanked drive into an ordinary, catchable I/O error. Readers still work
  while a re-index writes, because commits are small and frequent.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote

try:
    from .extractors import ExtractionError, extract_text, is_supported
except (ImportError, ValueError):
    from extractors import ExtractionError, extract_text, is_supported

CHUNK_SIZE = 1000          # characters per chunk
CHUNK_OVERLAP = 150        # characters shared between neighbouring chunks
META_DIRNAME = ".mcp_meta"
DB_FILENAME = "index.db"
BUSY_TIMEOUT_S = 10.0      # how long a writer waits for another writer
READ_TIMEOUT_S = 2.0       # status/stat reads must answer quickly
JOURNAL_MODE = os.environ.get("KB_SQLITE_JOURNAL", "DELETE").upper()
TRASH_DIRNAME = "_Trash"
MAX_TERMS = 8
# Generated files at the drive root that must not pollute search results.
SKIP_ROOT_FILES = {"INDEX.md"}

# Directories we never index (metadata, macOS/Spotlight cruft, VCS, tooling).
SKIP_DIRS = {
    META_DIRNAME, ".git", ".svn", "node_modules", "__pycache__", "mcp",
    ".Trashes", ".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100",
    ".TemporaryItems", TRASH_DIRNAME,
}


def file_sha256(path: Path, block: int = 1 << 20) -> str:
    """Hex SHA-256 of a file's bytes (streamed, constant memory)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(block), b""):
            digest.update(chunk)
    return digest.hexdigest()


class NoIndexYet(RuntimeError):
    """The index database has not been created yet."""


@dataclass
class SearchHit:
    """One retrieved chunk."""

    path: str          # path relative to the volume root
    chunk_id: int
    snippet: str
    score: float       # higher == more relevant (normalised)
    source: str        # "keyword" | "semantic" | "hybrid"


@dataclass
class IndexStats:
    """Summary returned by :meth:`Indexer.reindex`."""

    scanned: int = 0
    indexed: int = 0
    skipped_unchanged: int = 0
    removed: int = 0
    failed: int = 0
    hashed: int = 0                         # unchanged files that only got a hash
    errors: List[str] = field(default_factory=list)
    seconds: float = 0.0
    walk_errors: List[str] = field(default_factory=list)
    aborted: Optional[str] = None          # reason, when stopped early
    removal_skipped: Optional[str] = None  # reason, when stale rows were kept


ProgressFn = Callable[[IndexStats, str], None]
AbortFn = Callable[[], Optional[str]]


def _chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split ``text`` into overlapping windows, breaking on paragraph edges."""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    chunks: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        # Prefer to break on a paragraph / sentence boundary near the window end.
        if end < n:
            window = text[start:end]
            for sep in ("\n\n", "\n", "。", ". ", "! ", "? "):
                idx = window.rfind(sep)
                if idx > size * 0.5:
                    end = start + idx + len(sep)
                    break
        chunks.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


class Indexer:
    """All read/write index operations. Holds no open connection between calls."""

    def __init__(self, volume: Path, enable_semantic: bool = False,
                 embed_model: str = "intfloat/multilingual-e5-small") -> None:
        self.volume = Path(volume)
        self.meta_dir = self.volume / META_DIRNAME
        self.db_path = self.meta_dir / DB_FILENAME
        self.enable_semantic = enable_semantic
        self.embed_model_name = embed_model
        self._embedder = None  # lazily constructed
        self._embedder_lock = threading.Lock()
        self._schema_lock = threading.Lock()
        self._schema_key: Optional[Tuple[int, int]] = None
        self._journal_ok = False
        self.has_fts = True

    # ---- connection / schema ------------------------------------------- #
    def _db_key(self) -> Optional[Tuple[int, int]]:
        try:
            st = os.stat(self.db_path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def _open(self, create: bool = True) -> sqlite3.Connection:
        if not self.db_path.exists():
            if not create:
                raise NoIndexYet(str(self.db_path))
            self.meta_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=BUSY_TIMEOUT_S,
                               check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)};")
            conn.execute("PRAGMA mmap_size=0;")
            conn.execute("PRAGMA cache_size=-65536;")  # 64 MB: batches never spill
            with self._schema_lock:
                key = self._db_key()
                if key is None or key != self._schema_key:
                    self._journal_ok = False
                    self._ensure_schema(conn)
                    self._schema_key = self._db_key()
                if not self._journal_ok:
                    self._set_journal_mode(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _open_readonly(self) -> sqlite3.Connection:
        """A short-timeout reader that never creates or migrates anything."""
        if not self.db_path.exists():
            raise NoIndexYet(str(self.db_path))
        uri = f"file:{quote(str(self.db_path))}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=READ_TIMEOUT_S,
                                   check_same_thread=False)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.OperationalError:
            # A pure read-only open fails when a hot journal must be rolled
            # back (e.g. after an unplug mid-commit); a normal connection
            # recovers it and still only reads here.
            conn = sqlite3.connect(str(self.db_path), timeout=READ_TIMEOUT_S,
                                   check_same_thread=False)
        conn.execute("PRAGMA mmap_size=0;")
        conn.row_factory = sqlite3.Row
        return conn

    def _set_journal_mode(self, conn: sqlite3.Connection) -> None:
        """Convert (once) to the mmap-free journal mode; retried until it sticks."""
        try:
            mode = conn.execute(f"PRAGMA journal_mode={JOURNAL_MODE};").fetchone()[0]
            self._journal_ok = str(mode).upper() == JOURNAL_MODE
        except sqlite3.Error:
            self._journal_ok = False

    def prepare(self) -> Optional[str]:
        """Open the existing index once (schema check + journal conversion).

        Called at start-up so even the first read-only status query runs on
        the mmap-free journal mode. Returns the journal mode, or ``None``.
        """
        if not self.db_path.exists():
            return None
        conn = self._open(create=False)
        try:
            return str(conn.execute("PRAGMA journal_mode;").fetchone()[0])
        finally:
            conn.close()

    @contextmanager
    def _db(self, create: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self._open(create=create)
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        """Open a *new* connection (caller closes it). Kept for compatibility."""
        return self._open(create=True)

    def close(self) -> None:
        """No-op: connections are per operation now."""

    @staticmethod
    def _detect_fts(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'chunks'").fetchone()
        return bool(row and row[0] and "fts5" in row[0].lower())

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS files (
                   path        TEXT PRIMARY KEY,
                   mtime       REAL NOT NULL,
                   size        INTEGER NOT NULL,
                   n_chunks    INTEGER NOT NULL DEFAULT 0,
                   indexed_at  REAL NOT NULL
               )"""
        )
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'chunks'").fetchone()
        if exists:
            self.has_fts = self._detect_fts(conn)
        else:
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks "
                    "USING fts5(path UNINDEXED, cid UNINDEXED, content, "
                    "tokenize='trigram')"
                )
                self.has_fts = True
            except sqlite3.OperationalError:
                # FTS5 unavailable: fall back to a plain table + LIKE search.
                self.has_fts = False
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS chunks (
                           path TEXT, cid INTEGER, content TEXT
                       )"""
                )
                conn.execute("CREATE INDEX IF NOT EXISTS ix_chunks_path ON chunks(path)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS embeddings (
                   path TEXT, cid INTEGER, dim INTEGER, vec BLOB,
                   PRIMARY KEY (path, cid)
               )"""
        )
        # v0.3 migration: content hash for duplicate detection.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
        if "sha256" not in cols:
            conn.execute("ALTER TABLE files ADD COLUMN sha256 TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_files_sha ON files(sha256)")
        conn.commit()

    # ---- embedding helpers --------------------------------------------- #
    def _embedder_or_none(self):
        if not self.enable_semantic:
            return None
        with self._embedder_lock:
            if self._embedder is None:
                try:
                    from fastembed import TextEmbedding  # type: ignore
                except ModuleNotFoundError:
                    self.enable_semantic = False
                    return None
                self._embedder = TextEmbedding(model_name=self.embed_model_name)
        return self._embedder

    def _embed(self, texts: Sequence[str]):
        embedder = self._embedder_or_none()
        if embedder is None:
            return None
        import numpy as np

        vectors = list(embedder.embed(list(texts)))
        return [np.asarray(v, dtype="float32") for v in vectors]

    @staticmethod
    def _pack(vec) -> bytes:
        return struct.pack(f"{len(vec)}f", *vec.tolist())

    @staticmethod
    def _unpack(blob: bytes, dim: int):
        import numpy as np

        return np.asarray(struct.unpack(f"{dim}f", blob), dtype="float32")

    # ---- scanning ------------------------------------------------------- #
    def _iter_files(self, walk_errors: Optional[List[str]] = None) -> Iterable[Path]:
        def onerror(exc: OSError) -> None:
            if walk_errors is not None:
                walk_errors.append(f"{getattr(exc, 'filename', '?')}: {exc.strerror or exc}")

        for root, dirs, names in os.walk(self.volume, onerror=onerror):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            at_root = Path(root) == self.volume
            for name in names:
                if name.startswith(".") or (at_root and name in SKIP_ROOT_FILES):
                    continue
                p = Path(root) / name
                if is_supported(p):
                    yield p

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.volume))

    def reindex(self, full: bool = False, max_file_mb: int = 50,
                progress: Optional[ProgressFn] = None,
                should_abort: Optional[AbortFn] = None,
                commit_every: int = 25, commit_seconds: float = 3.0) -> IndexStats:
        """Walk the volume and bring the index up to date.

        :param full: When ``True``, re-extract every file regardless of mtime.
        :param max_file_mb: Skip files larger than this (prevents huge binaries
            from stalling the index).
        :param progress: Called as ``progress(stats, current_relpath)`` after
            each file, so a caller can report live progress.
        :param should_abort: Polled every few files; return a reason string to
            stop early (e.g. the drive went away). Work done so far is kept.
        :param commit_every / commit_seconds: Commit in batches so progress
            survives an unplug and readers see new files while the walk runs.
        """
        started = time.time()
        stats = IndexStats()
        with self._db(create=True) as conn:
            known = {row["path"]: (row["mtime"], row["size"], row["sha256"]) for row in
                     conn.execute("SELECT path, mtime, size, sha256 FROM files")}
            seen: set[str] = set()
            dirty = 0
            last_commit = time.monotonic()

            for path in self._iter_files(stats.walk_errors):
                if should_abort is not None and stats.scanned % 20 == 0:
                    reason = should_abort()
                    if reason:
                        stats.aborted = reason
                        break
                stats.scanned += 1
                rel = self._rel(path)
                seen.add(rel)
                try:
                    st = path.stat()
                except OSError as exc:
                    stats.failed += 1
                    stats.errors.append(f"{rel}: stat failed ({exc})")
                    continue
                if st.st_size > max_file_mb * 1024 * 1024:
                    stats.skipped_unchanged += 1
                    continue
                prev = known.get(rel)
                if not full and prev and abs(prev[0] - st.st_mtime) < 1 and prev[1] == st.st_size:
                    stats.skipped_unchanged += 1
                    if not prev[2]:  # indexed by an older version: add the hash
                        try:
                            conn.execute("UPDATE files SET sha256 = ? WHERE path = ?",
                                         (file_sha256(path), rel))
                            stats.hashed += 1
                            dirty += 1
                        except OSError as exc:
                            stats.errors.append(f"{rel}: hash failed ({exc})")
                else:
                    try:
                        self._index_file(conn, path, rel, st.st_mtime, st.st_size)
                        stats.indexed += 1
                        dirty += 1
                    except ExtractionError as exc:
                        stats.failed += 1
                        stats.errors.append(str(exc))
                    except sqlite3.Error:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        stats.failed += 1
                        stats.errors.append(f"{rel}: {exc}")
                if dirty and (dirty >= commit_every
                              or time.monotonic() - last_commit >= commit_seconds):
                    conn.commit()
                    dirty = 0
                    last_commit = time.monotonic()
                if progress is not None:
                    progress(stats, rel)

            # Remove rows for files that no longer exist on disk -- but only
            # when the walk really saw the whole drive. A walk that hit errors
            # (permission, I/O, unplug) would otherwise wipe the index.
            if stats.aborted:
                stats.removal_skipped = f"aborted: {stats.aborted}"
            elif stats.walk_errors:
                stats.removal_skipped = (
                    f"{len(stats.walk_errors)} folder(s) could not be read")
            else:
                for gone in set(known) - seen:
                    self._delete_path(conn, gone)
                    stats.removed += 1

        stats.seconds = round(time.time() - started, 2)
        return stats

    def _index_file(self, conn: sqlite3.Connection, path: Path, rel: str,
                    mtime: float, size: int) -> None:
        text = extract_text(path)
        sha = file_sha256(path)
        self._delete_path(conn, rel)
        chunks = _chunk(text)
        if chunks:
            conn.executemany(
                "INSERT INTO chunks(path, cid, content) VALUES (?, ?, ?)",
                [(rel, i, c) for i, c in enumerate(chunks)],
            )
            vectors = self._embed(chunks)
            if vectors is not None:
                conn.executemany(
                    "INSERT INTO embeddings(path, cid, dim, vec) VALUES (?, ?, ?, ?)",
                    [(rel, i, len(v), self._pack(v)) for i, v in enumerate(vectors)],
                )
        conn.execute(
            "INSERT OR REPLACE INTO files(path, mtime, size, n_chunks, indexed_at, sha256) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rel, mtime, size, len(chunks), time.time(), sha),
        )

    @staticmethod
    def _delete_path(conn: sqlite3.Connection, rel: str) -> None:
        conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        conn.execute("DELETE FROM embeddings WHERE path = ?", (rel,))
        conn.execute("DELETE FROM files WHERE path = ?", (rel,))

    def rename_in_index(self, old_rel: str, new_rel: str) -> None:
        """Keep the index consistent after a move/rename without re-extracting."""
        try:
            with self._db(create=False) as conn:
                for table in ("chunks", "embeddings", "files"):
                    conn.execute(f"UPDATE {table} SET path = ? WHERE path = ?",
                                 (new_rel, old_rel))
        except NoIndexYet:
            return

    def rename_prefix_in_index(self, old_dir: str, new_dir: str) -> int:
        """Re-point every indexed path under ``old_dir/`` to ``new_dir/``."""
        old_dir, new_dir = old_dir.strip("/"), new_dir.strip("/")
        esc = old_dir.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        try:
            with self._db(create=False) as conn:
                n = conn.execute("SELECT COUNT(*) AS n FROM files WHERE path LIKE ? ESCAPE '\\'",
                                 (esc + "/%",)).fetchone()["n"]
                for table in ("chunks", "embeddings", "files"):
                    conn.execute(
                        f"UPDATE {table} SET path = ? || substr(path, ?) "
                        f"WHERE path LIKE ? ESCAPE '\\'",
                        (new_dir, len(old_dir) + 1, esc + "/%"))
                return n
        except NoIndexYet:
            return 0

    def index_paths(self, rels: Sequence[str], force: bool = False,
                    max_file_mb: int = 50) -> IndexStats:
        """Index (or drop) specific files right away -- no full drive walk.

        Missing files are removed from the index; unchanged ones are skipped
        unless ``force``. Used after add/import/move so new material is
        searchable immediately.
        """
        started = time.time()
        stats = IndexStats()
        with self._db(create=True) as conn:
            for rel in rels:
                if rel in SKIP_ROOT_FILES:
                    continue
                path = self.volume / rel
                if not path.is_file():
                    if conn.execute("SELECT 1 FROM files WHERE path = ?", (rel,)).fetchone():
                        self._delete_path(conn, rel)
                        stats.removed += 1
                    continue
                if not is_supported(path):
                    continue
                stats.scanned += 1
                st = path.stat()
                if st.st_size > max_file_mb * 1024 * 1024:
                    stats.skipped_unchanged += 1
                    continue
                prev = conn.execute("SELECT mtime, size, sha256 FROM files WHERE path = ?",
                                    (rel,)).fetchone()
                if (not force and prev and prev["sha256"]
                        and abs(prev["mtime"] - st.st_mtime) < 1 and prev["size"] == st.st_size):
                    stats.skipped_unchanged += 1
                    continue
                try:
                    self._index_file(conn, path, rel, st.st_mtime, st.st_size)
                    stats.indexed += 1
                except ExtractionError as exc:
                    stats.failed += 1
                    stats.errors.append(str(exc))
        stats.seconds = round(time.time() - started, 2)
        return stats

    def delete_paths(self, rels: Sequence[str]) -> int:
        try:
            with self._db(create=False) as conn:
                for rel in rels:
                    self._delete_path(conn, rel)
        except NoIndexYet:
            return 0
        return len(rels)

    def is_indexed(self, rel: str) -> bool:
        try:
            conn = self._open_readonly()
        except NoIndexYet:
            return False
        try:
            return conn.execute("SELECT 1 FROM files WHERE path = ?", (rel,)).fetchone() is not None
        finally:
            conn.close()

    def paths_containing(self, text: str, limit: int = 5) -> List[str]:
        """Indexed files whose text contains ``text`` (exact substring)."""
        try:
            conn = self._open_readonly()
        except NoIndexYet:
            return []
        try:
            if self._detect_fts(conn) and len(text) >= 3:
                rows = conn.execute("SELECT DISTINCT path FROM chunks WHERE chunks MATCH ? LIMIT ?",
                                    (self._fts_phrase(text), limit)).fetchall()
            else:
                cond, arg = self._like_clause("content", text)
                rows = conn.execute(f"SELECT DISTINCT path FROM chunks WHERE {cond} LIMIT ?",
                                    (arg, limit)).fetchall()
            return [r["path"] for r in rows]
        finally:
            conn.close()

    def find_by_hash(self, sha: str) -> List[str]:
        try:
            with self._db(create=False) as conn:
                return [r["path"] for r in conn.execute(
                    "SELECT path FROM files WHERE sha256 = ? ORDER BY path", (sha,))]
        except NoIndexYet:
            return []

    def find_duplicates(self, limit: int = 50) -> dict:
        """Groups of indexed files with identical bytes, biggest first."""
        try:
            with self._db(create=False) as conn:
                groups = conn.execute(
                    "SELECT sha256, COUNT(*) AS n, MAX(size) AS size FROM files "
                    "WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING n > 1 "
                    "ORDER BY size DESC LIMIT ?", (limit,)).fetchall()
                out = []
                for g in groups:
                    paths = [r["path"] for r in conn.execute(
                        "SELECT path FROM files WHERE sha256 = ? ORDER BY indexed_at, path",
                        (g["sha256"],))]
                    out.append({"sha256": g["sha256"][:16], "size_kb": round(g["size"] / 1024, 1),
                                "paths": paths})
                unhashed = conn.execute(
                    "SELECT COUNT(*) AS n FROM files WHERE sha256 IS NULL").fetchone()["n"]
                return {"groups": out, "unhashed": unhashed}
        except NoIndexYet:
            return {"groups": [], "unhashed": 0}

    # ---- search --------------------------------------------------------- #
    def search(self, query: str, top_k: int = 8, mode: str = "hybrid",
               folder: Optional[str] = None) -> List[SearchHit]:
        """Return the ``top_k`` most relevant chunks for ``query``.

        ``mode`` is one of ``"keyword"``, ``"semantic"`` or ``"hybrid"``.
        Space-separated words must all appear (falls back to "any word" when
        nothing matches all). File names are matched too. ``folder`` limits
        results to one sub-folder, e.g. ``"01_Documents"``.
        """
        query = query.strip()
        if not query:
            return []
        mode = mode.lower()
        try:
            conn = self._open_readonly()
        except NoIndexYet:
            return []
        try:
            self.has_fts = self._detect_fts(conn) if conn else self.has_fts
            if mode == "keyword":
                return self._keyword_search(conn, query, top_k, folder)
            if mode == "semantic":
                return (self._semantic_search(conn, query, top_k, folder)
                        or self._keyword_search(conn, query, top_k, folder))
            kw = self._keyword_search(conn, query, top_k * 2, folder)
            sem = self._semantic_search(conn, query, top_k * 2, folder)
            if not sem:
                return kw[:top_k]
            return self._fuse(kw, sem, top_k)
        finally:
            conn.close()

    @staticmethod
    def _terms(query: str) -> List[str]:
        out: List[str] = []
        seen: set = set()
        for raw in re.split(r"\s+", query.strip()):
            term = raw.strip("\"'“”‘’，,。;；:：")
            if term and term.lower() not in seen:
                seen.add(term.lower())
                out.append(term)
        return out[:MAX_TERMS] or [query.strip()]

    @staticmethod
    def _like_clause(column: str, term: str) -> Tuple[str, str]:
        """LIKE condition + argument; only escapes when it has to (keeps the
        trigram index usable for the common case)."""
        if any(ch in term for ch in "%_\\"):
            esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            return f"{column} LIKE ? ESCAPE '\\'", f"%{esc}%"
        return f"{column} LIKE ?", f"%{term}%"

    @staticmethod
    def _folder_clause(folder: Optional[str]) -> Tuple[str, list]:
        folder = (folder or "").strip().strip("/")
        if not folder or folder == ".":
            return "", []
        esc = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return " AND path LIKE ? ESCAPE '\\'", [esc + "/%"]

    @staticmethod
    def _fts_phrase(term: str) -> str:
        return '"' + term.replace('"', '""') + '"'

    _fts_query = _fts_phrase  # backwards-compatible name

    def _keyword_search(self, conn: sqlite3.Connection, query: str, top_k: int,
                        folder: Optional[str] = None) -> List[SearchHit]:
        terms = self._terms(query)
        fsql, fargs = self._folder_clause(folder)
        hits: List[SearchHit] = []
        seen: set = set()

        def push(path: str, cid: int, content: str, score: float, source: str) -> None:
            if (path, cid) not in seen and len(hits) < top_k:
                seen.add((path, cid))
                hits.append(SearchHit(path, cid, self._snippet(content, terms), score, source))

        # 1) file names (not folder names) containing every word
        conds, args = zip(*(self._like_clause("path", t) for t in terms))
        name_hits = 0
        for r in conn.execute(
                "SELECT path FROM files WHERE " + " AND ".join(conds) + fsql +
                " ORDER BY indexed_at DESC LIMIT ?", [*args, *fargs, top_k * 5]):
            name = r["path"].rsplit("/", 1)[-1].lower()
            if all(t.lower() in name for t in terms) and name_hits < max(1, top_k // 2):
                name_hits += 1
                push(r["path"], 0, self._chunk_text(conn, r["path"], 0), 1.0, "filename")

        # 2) chunks containing every word (FTS5 for words >= 3 chars)
        long_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]
        rows: list = []
        if self.has_fts and long_terms:
            conds = [self._like_clause("content", t) for t in short_terms]
            try:
                rows = conn.execute(
                    "SELECT path, cid, content, bm25(chunks) AS score FROM chunks "
                    "WHERE chunks MATCH ?" + "".join(f" AND {c}" for c, _ in conds) + fsql +
                    " ORDER BY score LIMIT ?",
                    [" AND ".join(self._fts_phrase(t) for t in long_terms),
                     *(a for _, a in conds), *fargs, top_k]).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                rel = -float(r["score"])  # bm25: more negative == better
                push(r["path"], r["cid"], r["content"], round(rel / (1.0 + abs(rel)), 4), "keyword")
        if not rows:
            conds, args = zip(*(self._like_clause("content", t) for t in terms))
            rows = conn.execute(
                "SELECT path, cid, content FROM chunks WHERE " + " AND ".join(conds) + fsql +
                " LIMIT ?", [*args, *fargs, top_k]).fetchall()
            for rank, r in enumerate(rows):
                push(r["path"], r["cid"], r["content"], round(1.0 / (1.0 + rank), 4), "keyword")

        # 3) nothing matched every word: rank chunks by how many words they contain
        if not hits and len(terms) > 1:
            conds, args = zip(*(self._like_clause("content", t) for t in terms))
            rows = conn.execute(
                "SELECT path, cid, content FROM chunks WHERE (" + " OR ".join(conds) + ")" +
                fsql + " LIMIT ?", [*args, *fargs, top_k * 10]).fetchall()
            scored = []
            for r in rows:
                low = r["content"].lower()
                n = sum(1 for t in terms if t.lower() in low)
                scored.append((n / len(terms), r))
            scored.sort(key=lambda x: x[0], reverse=True)
            for score, r in scored:
                push(r["path"], r["cid"], r["content"], round(score, 4), "keyword-partial")
        return hits

    def _semantic_search(self, conn: sqlite3.Connection, query: str,
                         top_k: int, folder: Optional[str] = None) -> List[SearchHit]:
        vectors = self._embed([query])
        if not vectors:
            return []
        import numpy as np

        qv = vectors[0]
        qn = qv / (np.linalg.norm(qv) + 1e-9)
        rows = conn.execute("SELECT path, cid, dim, vec FROM embeddings").fetchall()
        prefix = (folder or "").strip().strip("/")
        if prefix and prefix != ".":
            rows = [r for r in rows if r["path"].startswith(prefix + "/")]
        if not rows:
            return []
        scored: List[Tuple[float, str, int]] = []
        for r in rows:
            v = self._unpack(r["vec"], r["dim"])
            v = v / (np.linalg.norm(v) + 1e-9)
            scored.append((float(np.dot(qn, v)), r["path"], r["cid"]))
        scored.sort(key=lambda t: t[0], reverse=True)
        hits: List[SearchHit] = []
        for score, path, cid in scored[:top_k]:
            content = self._chunk_text(conn, path, cid)
            hits.append(SearchHit(path, cid, self._snippet(content, self._terms(query)),
                                  score, "semantic"))
        return hits

    @staticmethod
    def _chunk_text(conn: sqlite3.Connection, path: str, cid: int) -> str:
        row = conn.execute(
            "SELECT content FROM chunks WHERE path = ? AND cid = ?", (path, cid)
        ).fetchone()
        return row["content"] if row else ""

    @staticmethod
    def _fuse(kw: List[SearchHit], sem: List[SearchHit], top_k: int, k: int = 60) -> List[SearchHit]:
        """Reciprocal Rank Fusion of two result lists keyed by (path, chunk)."""
        scores: dict = {}
        best: dict = {}
        for lst in (kw, sem):
            for rank, hit in enumerate(lst):
                key = (hit.path, hit.chunk_id)
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
                best.setdefault(key, hit)
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out: List[SearchHit] = []
        for (path, cid), score in ordered[:top_k]:
            hit = best[(path, cid)]
            out.append(SearchHit(path, cid, hit.snippet, round(score, 5), "hybrid"))
        return out

    _FRONT_KEY = re.compile(r"^[A-Za-z_][\w-]*:")

    @classmethod
    def _body_start(cls, content: str) -> int:
        """Offset after a leading YAML front-matter block (kb_add_note headers)."""
        if not content.startswith("---\n"):
            return 0
        end = content.find("\n---\n", 3)
        if end == -1:
            return 0
        lines = [ln for ln in content[4:end].splitlines() if ln.strip()]
        if not lines or len(lines) > 30 or not all(cls._FRONT_KEY.match(ln) for ln in lines):
            return 0
        body = end + 5
        while body < len(content) and content[body] == "\n":
            body += 1
        return body

    @classmethod
    def _snippet(cls, content: str, terms, width: int = 280) -> str:
        if not content:
            return ""
        if isinstance(terms, str):
            terms = terms.split()
        body = cls._body_start(content)
        lower = content.lower()
        positions = [p for p in (lower.find(t.lower(), body) for t in terms) if p >= 0]
        if not positions:  # matched only the header (title/tags) or not at all
            text = content[body:body + width].strip()
            return text + ("…" if len(content) > body + width else "")
        start = max(body, min(positions) - width // 3)
        return ("…" if start > body else "") + content[start:start + width].strip() + "…"

    # ---- stats ---------------------------------------------------------- #
    def stats(self) -> dict:
        """Cheap, read-only statistics. Never creates the database."""
        out = {
            "files_indexed": 0,
            "chunks": 0,
            "embeddings": 0,
            "fts5": self.has_fts,
            "semantic_enabled": self.enable_semantic,
            "db_path": str(self.db_path),
            "db_exists": self.db_path.exists(),
        }
        if not out["db_exists"]:
            return out
        try:
            out["db_size_mb"] = round(self.db_path.stat().st_size / 1024 ** 2, 2)
        except OSError:
            pass
        conn = self._open_readonly()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(n_chunks), 0) AS c, "
                "MAX(indexed_at) AS last FROM files").fetchone()
            out["files_indexed"] = row["n"]
            out["chunks"] = row["c"]
            out["last_indexed_at"] = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["last"]))
                if row["last"] else None)
            out["embeddings"] = conn.execute(
                "SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
            out["fts5"] = self.has_fts = self._detect_fts(conn)
            out["journal_mode"] = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        finally:
            conn.close()
        return out

    def indexed_paths(self) -> List[str]:
        try:
            conn = self._open_readonly()
        except NoIndexYet:
            return []
        try:
            return [r["path"] for r in conn.execute("SELECT path FROM files ORDER BY path")]
        finally:
            conn.close()
