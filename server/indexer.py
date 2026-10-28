"""Incremental hybrid index over the knowledge-base volume.

Storage lives in a single SQLite database at ``<volume>/.mcp_meta/index.db``:

* ``files``       - one row per indexed file (mtime/size for change detection).
* ``chunks``      - FTS5 virtual table (``trigram`` tokenizer so Chinese
                    substring search works) holding the chunked text.
* ``embeddings``  - optional per-chunk float32 vectors for semantic search.

The design goal is *robustness over cleverness*: a single file failing to
extract must never abort a full re-index, and the whole thing must run from a
plain ``pip install`` with no server process or container.
"""
from __future__ import annotations

import os
import re
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from extractors import ExtractionError, extract_text, is_supported

CHUNK_SIZE = 1000          # characters per chunk
CHUNK_OVERLAP = 150        # characters shared between neighbouring chunks
META_DIRNAME = ".mcp_meta"
DB_FILENAME = "index.db"

# Directories we never index (metadata, macOS/Spotlight cruft, VCS, tooling).
SKIP_DIRS = {
    META_DIRNAME, ".git", ".svn", "node_modules", "__pycache__", "mcp",
    ".Trashes", ".Spotlight-V100", ".fseventsd", ".DocumentRevisions-V100",
    ".TemporaryItems",
}


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
    errors: List[str] = field(default_factory=list)
    seconds: float = 0.0


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
    """Owns the SQLite connection and all read/write index operations."""

    def __init__(self, volume: Path, enable_semantic: bool = False,
                 embed_model: str = "intfloat/multilingual-e5-small") -> None:
        self.volume = Path(volume)
        self.meta_dir = self.volume / META_DIRNAME
        self.db_path = self.meta_dir / DB_FILENAME
        self.enable_semantic = enable_semantic
        self.embed_model_name = embed_model
        self._embedder = None  # lazily constructed
        self._conn: Optional[sqlite3.Connection] = None
        self.has_fts = True

    # ---- connection / schema ------------------------------------------- #
    def connect(self) -> sqlite3.Connection:
        """Open (and memoise) the SQLite connection, creating schema on first use."""
        if self._conn is not None:
            return self._conn
        if not self.meta_dir.exists():
            self.meta_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn = conn
        self._ensure_schema(conn)
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

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
        conn.commit()

    # ---- embedding helpers --------------------------------------------- #
    def _embedder_or_none(self):
        if not self.enable_semantic:
            return None
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
    def _iter_files(self) -> Iterable[Path]:
        for root, dirs, names in os.walk(self.volume):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in names:
                if name.startswith("."):
                    continue
                p = Path(root) / name
                if is_supported(p):
                    yield p

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.volume))

    def reindex(self, full: bool = False, max_file_mb: int = 50) -> IndexStats:
        """Walk the volume and bring the index up to date.

        :param full: When ``True``, re-extract every file regardless of mtime.
        :param max_file_mb: Skip files larger than this (prevents huge binaries
            from stalling the index).
        """
        started = time.time()
        conn = self.connect()
        stats = IndexStats()

        known = {row["path"]: (row["mtime"], row["size"]) for row in
                 conn.execute("SELECT path, mtime, size FROM files")}
        seen: set[str] = set()

        for path in self._iter_files():
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
                continue
            try:
                self._index_file(conn, path, rel, st.st_mtime, st.st_size)
                stats.indexed += 1
            except ExtractionError as exc:
                stats.failed += 1
                stats.errors.append(str(exc))
            except Exception as exc:  # noqa: BLE001
                stats.failed += 1
                stats.errors.append(f"{rel}: {exc}")

        # Remove rows for files that no longer exist on disk.
        for gone in set(known) - seen:
            self._delete_path(conn, gone)
            stats.removed += 1

        conn.commit()
        stats.seconds = round(time.time() - started, 2)
        return stats

    def _index_file(self, conn: sqlite3.Connection, path: Path, rel: str,
                    mtime: float, size: int) -> None:
        text = extract_text(path)
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
            "INSERT OR REPLACE INTO files(path, mtime, size, n_chunks, indexed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (rel, mtime, size, len(chunks), time.time()),
        )

    def _delete_path(self, conn: sqlite3.Connection, rel: str) -> None:
        conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
        conn.execute("DELETE FROM embeddings WHERE path = ?", (rel,))
        conn.execute("DELETE FROM files WHERE path = ?", (rel,))

    def rename_in_index(self, old_rel: str, new_rel: str) -> None:
        """Keep the index consistent after a move/rename without re-extracting."""
        conn = self.connect()
        for table in ("chunks", "embeddings", "files"):
            conn.execute(f"UPDATE {table} SET path = ? WHERE path = ?", (new_rel, old_rel))
        conn.commit()

    # ---- search --------------------------------------------------------- #
    def search(self, query: str, top_k: int = 8, mode: str = "hybrid") -> List[SearchHit]:
        """Return the ``top_k`` most relevant chunks for ``query``.

        ``mode`` is one of ``"keyword"``, ``"semantic"`` or ``"hybrid"``.
        """
        query = query.strip()
        if not query:
            return []
        mode = mode.lower()
        if mode == "keyword":
            return self._keyword_search(query, top_k)
        if mode == "semantic":
            return self._semantic_search(query, top_k) or self._keyword_search(query, top_k)
        # hybrid: reciprocal-rank fusion of both lists.
        kw = self._keyword_search(query, top_k * 2)
        sem = self._semantic_search(query, top_k * 2)
        return self._fuse(kw, sem, top_k)

    def _keyword_search(self, query: str, top_k: int) -> List[SearchHit]:
        conn = self.connect()
        hits: List[SearchHit] = []
        if self.has_fts:
            try:
                rows = conn.execute(
                    "SELECT path, cid, content, bm25(chunks) AS score "
                    "FROM chunks WHERE chunks MATCH ? ORDER BY score LIMIT ?",
                    (self._fts_query(query), top_k),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for r in rows:
                # bm25 is lower-is-better; convert to higher-is-better in (0,1].
                hits.append(SearchHit(r["path"], r["cid"], self._snippet(r["content"], query),
                                      1.0 / (1.0 + max(r["score"], 0.0)), "keyword"))
        if not hits:  # fallback / FTS disabled
            like = f"%{query}%"
            rows = conn.execute(
                "SELECT path, cid, content FROM chunks WHERE content LIKE ? LIMIT ?",
                (like, top_k),
            ).fetchall()
            for rank, r in enumerate(rows):
                hits.append(SearchHit(r["path"], r["cid"], self._snippet(r["content"], query),
                                      1.0 / (1.0 + rank), "keyword"))
        return hits

    @staticmethod
    def _fts_query(query: str) -> str:
        # Trigram tokenizer treats the string as a substring pattern; just
        # neutralise the FTS5 quote character.
        return '"' + query.replace('"', '""') + '"'

    def _semantic_search(self, query: str, top_k: int) -> List[SearchHit]:
        vectors = self._embed([query])
        if not vectors:
            return []
        import numpy as np

        qv = vectors[0]
        qn = qv / (np.linalg.norm(qv) + 1e-9)
        conn = self.connect()
        rows = conn.execute("SELECT path, cid, dim, vec FROM embeddings").fetchall()
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
            hits.append(SearchHit(path, cid, self._snippet(content, query), score, "semantic"))
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

    @staticmethod
    def _snippet(content: str, query: str, width: int = 280) -> str:
        if not content:
            return ""
        lower = content.lower()
        pos = lower.find(query.lower().split()[0]) if query.split() else -1
        if pos < 0:
            return content[:width].strip()
        start = max(0, pos - width // 3)
        return ("…" if start else "") + content[start:start + width].strip() + "…"

    # ---- stats ---------------------------------------------------------- #
    def stats(self) -> dict:
        conn = self.connect()
        files = conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        embeds = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
        return {
            "files_indexed": files,
            "chunks": chunks,
            "embeddings": embeds,
            "fts5": self.has_fts,
            "semantic_enabled": self.enable_semantic,
            "db_path": str(self.db_path),
        }

    def indexed_paths(self) -> List[str]:
        conn = self.connect()
        return [r["path"] for r in conn.execute("SELECT path FROM files ORDER BY path")]
