"""Unit tests for kb_mcp.indexer (stdlib only: python -m unittest tests/test_indexer.py)."""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kb_mcp"))
from indexer import Indexer, NoIndexYet  # noqa: E402


class IndexerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vol = Path(tempfile.mkdtemp(prefix="kbi-"))
        docs = self.vol / "01_Documents"
        docs.mkdir()
        for i in range(60):
            (docs / f"note_{i:03d}.md").write_text(
                f"# 笔记 {i}\n\n" + ("知识库测试段落。" * 40) + (" 量子猫粮" if i == 7 else ""),
                encoding="utf-8")
        self.idx = Indexer(self.vol)

    def tearDown(self) -> None:
        shutil.rmtree(self.vol, ignore_errors=True)

    def test_stats_do_not_create_db(self) -> None:
        s = self.idx.stats()
        self.assertFalse(s["db_exists"])
        self.assertEqual(s["files_indexed"], 0)
        self.assertFalse(self.idx.db_path.exists())
        self.assertEqual(self.idx.search("知识库"), [])
        self.assertEqual(self.idx.indexed_paths(), [])

    def test_reindex_progress_and_search(self) -> None:
        seen = []
        st = self.idx.reindex(progress=lambda stats, rel: seen.append(rel), commit_every=10)
        self.assertEqual(st.indexed, 60)
        self.assertEqual(len(seen), 60)
        s = self.idx.stats()
        self.assertEqual(s["files_indexed"], 60)
        self.assertGreaterEqual(s["chunks"], 60)
        self.assertTrue(s["fts5"])
        hits = self.idx.search("量子猫粮", mode="keyword")
        self.assertEqual(hits[0].path, "01_Documents/note_007.md")
        again = self.idx.reindex()
        self.assertEqual(again.indexed, 0)
        self.assertEqual(again.skipped_unchanged, 60)

    def test_abort_keeps_existing_rows(self) -> None:
        self.idx.reindex()
        calls = {"n": 0}

        def abort():
            calls["n"] += 1
            return "drive not_mounted" if calls["n"] > 1 else None
        st = self.idx.reindex(full=True, should_abort=abort)
        self.assertEqual(st.aborted, "drive not_mounted")
        self.assertIsNotNone(st.removal_skipped)
        self.assertEqual(self.idx.stats()["files_indexed"], 60)

    def test_walk_errors_do_not_wipe_index(self) -> None:
        self.idx.reindex()
        idx = self.idx
        original = idx._iter_files

        def broken(walk_errors=None):
            if walk_errors is not None:
                walk_errors.append("01_Documents: Input/output error")
            return iter(())  # the walk "saw" nothing
        idx._iter_files = broken  # type: ignore[assignment]
        st = idx.reindex()
        idx._iter_files = original  # type: ignore[assignment]
        self.assertEqual(st.removed, 0)
        self.assertIn("could not be read", st.removal_skipped)
        self.assertEqual(idx.stats()["files_indexed"], 60)

    def test_removed_files_are_dropped_after_clean_walk(self) -> None:
        self.idx.reindex()
        (self.vol / "01_Documents" / "note_000.md").unlink()
        st = self.idx.reindex()
        self.assertEqual(st.removed, 1)
        self.assertEqual(self.idx.stats()["files_indexed"], 59)

    def test_journal_is_mmap_free_and_wal_is_converted(self) -> None:
        self.idx.reindex()
        self.assertEqual(self.idx.stats()["journal_mode"].lower(), "delete")
        # An index created by the previous release is in WAL mode: convert it.
        conn = sqlite3.connect(str(self.idx.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.close()
        fresh = Indexer(self.vol)
        self.assertEqual(fresh.prepare().lower(), "delete")
        self.assertFalse(Path(str(fresh.db_path) + "-shm").exists())
        self.assertEqual(fresh.stats()["files_indexed"], 60)

    def test_rename_in_index(self) -> None:
        self.idx.reindex()
        self.idx.rename_in_index("01_Documents/note_007.md", "03_Notes/cat.md")
        self.assertIn("03_Notes/cat.md", self.idx.indexed_paths())
        self.assertEqual(self.idx.search("量子猫粮", mode="keyword")[0].path, "03_Notes/cat.md")

    def test_db_replaced_underneath(self) -> None:
        """Simulates unplug/replug: the db file is swapped while the Indexer lives."""
        self.idx.reindex()
        backup = self.vol / "backup.db"
        shutil.copy(self.idx.db_path, backup)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.idx.db_path) + suffix)
            if p.exists():
                p.unlink()
        shutil.copy(backup, self.idx.db_path)
        self.assertEqual(self.idx.stats()["files_indexed"], 60)
        self.assertTrue(self.idx.search("量子猫粮"))

    def test_stats_while_writer_holds_transaction(self) -> None:
        self.idx.reindex()
        conn = sqlite3.connect(str(self.idx.db_path))
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM files WHERE path LIKE '%note_00%'")
        result = {}

        def read():
            t0 = time.monotonic()
            result["stats"] = self.idx.stats()
            result["t"] = time.monotonic() - t0
        th = threading.Thread(target=read)
        th.start()
        th.join(5)
        conn.rollback()
        conn.close()
        self.assertIn("stats", result)
        self.assertLess(result["t"], 1.0)
        self.assertEqual(result["stats"]["files_indexed"], 60)  # WAL: sees committed data


if __name__ == "__main__":
    unittest.main()
