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


class SearchAndDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vol = Path(tempfile.mkdtemp(prefix="kbs-"))
        (self.vol / "01_Documents").mkdir()
        (self.vol / "03_Notes").mkdir()
        (self.vol / "04_Archive").mkdir()
        (self.vol / "01_Documents" / "2026-10-25_财务预算_硬件采购清单.txt").write_text(
            "第三季度采购：服务器、显示器、键盘。总预算十二万。", encoding="utf-8")
        (self.vol / "03_Notes" / "猫粮笔记.md").write_text(
            "小橘喜欢的猫粮品牌和硬件无关，预算每月两百。", encoding="utf-8")
        (self.vol / "03_Notes" / "copy.md").write_text(
            "小橘喜欢的猫粮品牌和硬件无关，预算每月两百。", encoding="utf-8")
        self.idx = Indexer(self.vol)
        self.idx.reindex()

    def tearDown(self) -> None:
        shutil.rmtree(self.vol, ignore_errors=True)

    def paths(self, query: str, **kw) -> list:
        return [h.path for h in self.idx.search(query, mode="keyword", **kw)]

    def test_multi_word_query_matches_all_words(self) -> None:
        got = self.paths("服务器 显示器")
        self.assertEqual(got[0], "01_Documents/2026-10-25_财务预算_硬件采购清单.txt")

    def test_short_words_and_any_word_fallback(self) -> None:
        self.assertTrue(self.paths("预算"))                       # 2-char word
        got = self.idx.search("显示器 不存在的词", mode="keyword")  # nothing has both
        self.assertTrue(got)
        self.assertEqual(got[0].source, "keyword-partial")

    def test_filename_match(self) -> None:
        hits = self.idx.search("采购清单", mode="keyword")
        self.assertEqual(hits[0].source, "filename")
        self.assertFalse([h for h in self.idx.search("Documents", mode="keyword")
                          if h.source == "filename"])   # folder names don't count

    def test_folder_filter(self) -> None:
        self.assertTrue(all(p.startswith("03_Notes/") for p in self.paths("预算", folder="03_Notes")))
        self.assertTrue(all(p.startswith("01_Documents/")
                            for p in self.paths("预算", folder="01_Documents/")))

    def test_duplicates_and_hash_lookup(self) -> None:
        d = self.idx.find_duplicates()
        self.assertEqual(len(d["groups"]), 1)
        self.assertEqual(sorted(d["groups"][0]["paths"]), ["03_Notes/copy.md", "03_Notes/猫粮笔记.md"])
        from indexer import file_sha256
        sha = file_sha256(self.vol / "03_Notes" / "copy.md")
        self.assertEqual(len(self.idx.find_by_hash(sha)), 2)

    def test_index_paths_is_immediate_and_handles_removal(self) -> None:
        new = self.vol / "03_Notes" / "新笔记.md"
        new.write_text("量子纠缠的猫", encoding="utf-8")
        st = self.idx.index_paths(["03_Notes/新笔记.md"])
        self.assertEqual(st.indexed, 1)
        self.assertEqual(self.paths("量子纠缠")[0], "03_Notes/新笔记.md")
        new.unlink()
        self.assertEqual(self.idx.index_paths(["03_Notes/新笔记.md"]).removed, 1)
        self.assertFalse(self.paths("量子纠缠"))

    def test_root_index_md_is_not_indexed(self) -> None:
        (self.vol / "INDEX.md").write_text("- 2026-10-25_财务预算_硬件采购清单.txt", encoding="utf-8")
        (self.vol / "03_Notes" / "INDEX.md").write_text("子目录里的同名文件照常索引", encoding="utf-8")
        self.idx.reindex()
        self.assertNotIn("INDEX.md", self.idx.indexed_paths())
        self.assertIn("03_Notes/INDEX.md", self.idx.indexed_paths())

    def test_snippet_skips_front_matter(self) -> None:
        note = self.vol / "03_Notes" / "带头部.md"
        note.write_text('---\ntitle: "带头部"\ntags: ["猫"]\ncontent_sha256: abcdef0123456789\n---\n\n'
                        "正文第一句。这里写着钛合金猫爬架。", encoding="utf-8")
        self.idx.index_paths(["03_Notes/带头部.md"])
        for q in ("钛合金", "带头部"):           # body match / file-name (header-only) match
            snip = self.idx.search(q, mode="keyword")[0].snippet
            self.assertNotIn("content_sha256", snip, q)
            self.assertTrue(snip.startswith("正文第一句"), snip)
        plain = Indexer._snippet("---\n普通分隔线\n---\n正文", ["正文"])
        self.assertIn("普通分隔线", plain)        # not front matter: left alone

    def test_rename_prefix_in_index(self) -> None:
        moved = self.vol / "04_Archive" / "Notes_old"
        shutil.move(str(self.vol / "03_Notes"), str(moved))
        n = self.idx.rename_prefix_in_index("03_Notes", "04_Archive/Notes_old")
        self.assertEqual(n, 2)
        self.assertIn("04_Archive/Notes_old/猫粮笔记.md", self.idx.indexed_paths())
        self.assertEqual(self.paths("猫粮品牌")[0].split("/")[0], "04_Archive")
        self.assertEqual(self.idx.rename_prefix_in_index("01_Documents_x", "y"), 0)  # "_" escaped

    def test_trash_is_not_indexed(self) -> None:
        (self.vol / "_Trash").mkdir()
        (self.vol / "_Trash" / "old.md").write_text("回收站里的内容", encoding="utf-8")
        self.idx.reindex()
        self.assertFalse(self.paths("回收站里的内容"))

    def test_old_schema_is_migrated_and_backfilled(self) -> None:
        conn = sqlite3.connect(str(self.idx.db_path))
        conn.execute("CREATE TABLE f2 AS SELECT path, mtime, size, n_chunks, indexed_at FROM files")
        conn.execute("DROP TABLE files")
        conn.execute("ALTER TABLE f2 RENAME TO files")
        conn.commit()
        conn.close()
        fresh = Indexer(self.vol)
        st = fresh.reindex()
        self.assertEqual(st.hashed, 3)
        self.assertEqual(fresh.find_duplicates()["unhashed"], 0)
        self.assertEqual(len(fresh.find_duplicates()["groups"]), 1)


if __name__ == "__main__":
    unittest.main()
