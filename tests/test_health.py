"""Unit tests for kb_mcp.health (stdlib only: python -m unittest tests/test_health.py)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "kb_mcp"))
import health  # noqa: E402


class ProberTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="kbh-"))
        self.vol = self.tmp / "Database"
        (self.vol / ".mcp_meta").mkdir(parents=True)
        (self.vol / ".mcp_meta" / ".kb_root").write_text("schema=1\n")

    def tearDown(self) -> None:
        for p in sorted(self.tmp.rglob("*"), reverse=True):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ok_with_write_test(self) -> None:
        r = health.Prober(self.vol, require_external=False).probe(write_test=True)
        self.assertEqual(r["state"], health.OK)
        self.assertTrue(r["mounted"] and r["readable"] and r["writable"])
        self.assertEqual(r["checks"]["write_test"], "passed")
        self.assertTrue(r["checks"]["marker"])
        self.assertEqual(list((self.vol / ".mcp_meta").glob(".kb_probe_*")), [])

    def test_not_mounted(self) -> None:
        r = health.Prober(self.tmp / "missing", require_external=False).probe()
        self.assertEqual(r["state"], health.NOT_MOUNTED)
        self.assertFalse(r["mounted"])
        self.assertTrue(r["hint"])

    def test_not_a_directory(self) -> None:
        f = self.tmp / "file"
        f.write_text("x")
        self.assertEqual(health.Prober(f, require_external=False).probe()["state"],
                         health.NOT_A_DIRECTORY)

    def test_phantom_dir(self) -> None:
        p = health.Prober(self.vol, require_external=False)
        p.mount_root, p.require_external = self.tmp, True  # tmp is not a mount point
        r = p.probe()
        self.assertEqual(r["state"], health.PHANTOM_DIR)
        self.assertFalse(r["mounted"])

    def test_kb_dir_missing(self) -> None:
        p = health.Prober(Path("/kb-test-definitely-missing/Database"), require_external=False)
        p.mount_root = Path("/")  # "/" is always a mount point
        self.assertEqual(p.probe()["state"], health.KB_DIR_MISSING)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_permission_denied(self) -> None:
        os.chmod(self.vol, 0o000)
        r = health.Prober(self.vol, require_external=False).probe()
        self.assertEqual(r["state"], health.PERMISSION_DENIED)

    def test_unresponsive_is_bounded(self) -> None:
        p = health.Prober(self.vol, timeout=0.5, require_external=False)
        p._probe_once = lambda write_test: time.sleep(3) or {}  # type: ignore[assignment]
        t0 = time.monotonic()
        r = p.probe(use_cache=False)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(r["state"], health.UNRESPONSIVE)
        # A second call must not pile up another blocked thread.
        t0 = time.monotonic()
        r2 = p.probe(use_cache=False)
        self.assertLess(time.monotonic() - t0, 0.2)
        self.assertEqual(r2["state"], health.UNRESPONSIVE)
        self.assertIn("previous check", r2["reason"])

    def test_exception_becomes_io_error(self) -> None:
        p = health.Prober(self.vol, require_external=False)

        def boom(write_test: bool) -> dict:
            raise RuntimeError("kaboom")
        p._probe_once = boom  # type: ignore[assignment]
        self.assertEqual(p.probe(use_cache=False)["state"], health.IO_ERROR)

    def test_cache(self) -> None:
        p = health.Prober(self.vol, cache_ttl=5, require_external=False)  # opt-in cache
        first = p.probe()
        calls = []
        p._probe_once = lambda write_test: calls.append(1) or {}  # type: ignore[assignment]
        self.assertEqual(p.probe()["state"], first["state"])
        self.assertEqual(calls, [])

    def test_failures_are_not_cached(self) -> None:
        p = health.Prober(self.tmp / "later", cache_ttl=60, require_external=False)
        self.assertEqual(p.probe()["state"], health.NOT_MOUNTED)
        (self.tmp / "later").mkdir()
        self.assertEqual(p.probe()["state"], health.OK)  # no stale "not_mounted"

    def test_failure_invalidates_cached_ok(self) -> None:
        p = health.Prober(self.vol, cache_ttl=60, require_external=False)
        self.assertEqual(p.probe()["state"], health.OK)              # cached ok
        self.vol.rename(self.tmp / "unplugged")
        self.assertEqual(p.probe(write_test=True, use_cache=False)["state"],
                         health.NOT_MOUNTED)                          # e.g. kb_status
        self.assertEqual(p.probe()["state"], health.NOT_MOUNTED)     # e.g. kb_search

    def test_concurrent_probes_share_instead_of_reporting_unresponsive(self) -> None:
        import threading
        p = health.Prober(self.vol, timeout=2.0, cache_ttl=0, require_external=False)
        real = p._probe_once

        def slow(write_test: bool) -> dict:
            time.sleep(0.4)
            return real(write_test)
        p._probe_once = slow  # type: ignore[assignment]
        states = []
        threads = [threading.Thread(target=lambda: states.append(p.probe()["state"]))
                   for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(states, [health.OK] * 4)

    def test_mount_root_of(self) -> None:
        self.assertEqual(health.mount_root_of(Path("/Volumes/KB/Database")), Path("/Volumes/KB"))
        self.assertEqual(health.mount_root_of(Path("/Volumes/KB")), Path("/Volumes/KB"))
        self.assertIsNone(health.mount_root_of(Path("/Users/x/kb")))

    def test_state_since_tracks_transitions(self) -> None:
        p = health.Prober(self.vol, cache_ttl=0, require_external=False)
        a = p.probe()
        time.sleep(0.05)
        b = p.probe()
        self.assertEqual(a["state_since"], b["state_since"])
        p.volume = self.tmp / "gone"
        c = p.probe()
        self.assertGreater(c["state_since"], b["state_since"])
        self.assertIsNotNone(c["last_ok_at"])


if __name__ == "__main__":
    unittest.main()
