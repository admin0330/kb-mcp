#!/usr/bin/env python3
"""End-to-end tests on macOS using a throw-away disk image as the "drive".

Run with the server venv:  server/.venv/bin/python tests/e2e_macos.py
It creates /Volumes/KBTEST from a temp .dmg, plugs/unplugs it with hdiutil,
and talks to the servers over stdio exactly like Claude Desktop does.
Also runs the previous release (git HEAD) for before/after comparison.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
from mcp_client import Client  # noqa: E402

PY = sys.executable
VOLNAME = "KBTEST"
MOUNT = Path("/Volumes") / VOLNAME
KB = MOUNT / "Database"
N_FILES = 1500
WORK = Path(tempfile.mkdtemp(prefix="kbe2e-"))
DMG = WORK / "kbtest.sparseimage"
LOGS = WORK / "logs"
LOGS.mkdir()
FS_CMD = ["npx", "-y", "@modelcontextprotocol/server-filesystem", str(KB)]

results: list = []
observations: list = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
    return bool(cond)


def observe(name: str, detail: str) -> None:
    observations.append((name, detail))
    print(f"[INFO] {name} -- {detail}", flush=True)


def sh(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def wait_for(cond, timeout: float = 15) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def attach(readonly: bool = False) -> None:
    args = ["hdiutil", "attach", "-nobrowse"] + (["-readonly"] if readonly else []) + [str(DMG)]
    r = sh(*args)
    if r.returncode != 0 or not wait_for(lambda: os.path.ismount(MOUNT)):
        raise SystemExit(f"attach failed: {r.stdout} {r.stderr}")


def detach() -> None:
    sh("hdiutil", "detach", "-force", str(MOUNT))
    if not wait_for(lambda: not MOUNT.exists()):
        raise SystemExit("detach failed")


def env(**extra: str) -> dict:
    e = dict(os.environ)
    e.update({"KB_VOLUME": str(KB), "KB_ENABLE_SEMANTIC": "0", "PYTHONFAULTHANDLER": "1",
              "KB_AUTO_REINDEX": "0",  # these tests drive reindex by hand
              "PATH": "/opt/homebrew/bin:/usr/local/bin:" + e.get("PATH", "")})
    e.update(extra)
    return e


def build_image() -> None:
    if MOUNT.exists():
        raise SystemExit(f"{MOUNT} already exists; eject it first")
    r = sh("hdiutil", "create", "-size", "1g", "-type", "SPARSE", "-fs", "APFS",
           "-volname", VOLNAME, "-ov", str(DMG))
    if r.returncode != 0:
        raise SystemExit(f"hdiutil create failed: {r.stderr}")
    attach()
    for d in ("_Inbox", "01_Documents", "02_Books", "03_Notes", "04_Archive", ".mcp_meta"):
        (KB / d).mkdir(parents=True, exist_ok=True)
    (KB / ".mcp_meta" / ".kb_root").write_text("schema=1\n")
    para = "外置硬盘知识库端到端测试段落，用来让索引有足够的工作量。" * 12 + "\n\n"
    for i in range(N_FILES):
        sub = KB / "01_Documents" / f"batch_{i // 100:02d}"
        sub.mkdir(exist_ok=True)
        extra = " 量子猫粮配方" if i % 250 == 0 else ""
        (sub / f"doc_{i:04d}.md").write_text(f"# 文档 {i}\n\n" + para * 30 + extra,
                                             encoding="utf-8")


def old_code_dir() -> Path:
    d = WORK / "old_server"
    d.mkdir()
    for f in ("kb_mcp_server.py", "indexer.py", "extractors.py", "placeholder_server.py"):
        r = sh("git", "-C", str(REPO), "show", f"HEAD:server/{f}")
        (d / f).write_text(r.stdout)
    return d


# --------------------------------------------------------------------------- #
def test_retrieval_new() -> None:
    print("\n=== kb-retrieval (new) ===", flush=True)
    c = Client([PY, str(REPO / "server" / "kb_mcp_server.py")], env=env(),
               stderr_path=str(LOGS / "retrieval_new.log"))
    c.initialize()
    s, _, t = c.tool("kb_status")
    check("status ok on fresh drive", s.get("state") == "ok" and s["mounted"], f"{t:.2f}s")
    check("status does not create the index db", s["index"].get("db_exists") is False)

    rid = c.tool_send("kb_reindex", {"full": True, "wait_seconds": 45})
    time.sleep(1.0)
    s, _, t = c.tool("kb_status", timeout=30)
    job = s.get("jobs", {}).get("reindex", {})
    check("status answers while reindex runs", t < 3.0, f"{t:.2f}s")
    check("status shows live reindex progress",
          job.get("state") == "running" and job.get("progress", {}).get("scanned", 0) > 0,
          str(job.get("progress")))
    r, _ = Client.parse(c.wait(rid, 180))
    check("reindex completes with all files", r.get("indexed") == N_FILES,
          f"indexed={r.get('indexed')} in {r.get('seconds')}s")

    s, _, _ = c.tool("kb_search", {"query": "量子猫粮配方", "mode": "keyword"})
    check("search finds keyword", s.get("count", 0) >= 1, f"count={s.get('count')}")

    detach()
    s, is_err, t = c.tool("kb_status")
    check("unplugged: status says not_mounted (no exception)",
          s.get("state") == "not_mounted" and not is_err and s.get("hint"), f"{t:.2f}s")
    s, _, _ = c.tool("kb_search", {"query": "量子"})
    check("unplugged: search returns drive_not_mounted", s.get("error") == "drive_not_mounted")

    attach()
    s, is_err, _ = c.tool("kb_status")
    check("replugged: status ok with index intact",
          s.get("state") == "ok" and s.get("index", {}).get("files_indexed") == N_FILES,
          str(s.get("index", {}).get("files_indexed") or s.get("index")))
    s, _, _ = c.tool("kb_search", {"query": "量子猫粮配方", "mode": "keyword"})
    check("replugged: search works without restart", s.get("count", 0) >= 1)

    r, _, _ = c.tool("kb_reindex", {"full": True, "wait_seconds": 0})
    check("reindex(wait_seconds=0) returns immediately as running", r.get("running") is True)
    time.sleep(1.0)
    detach()
    time.sleep(2.5)
    s, _, t = c.tool("kb_status")
    check("unplug mid-reindex: status still answers", s.get("state") == "not_mounted",
          f"{t:.2f}s job={s.get('jobs', {}).get('reindex', {}).get('state')}")
    attach()
    wait_for(lambda: c.tool("kb_status")[0].get("jobs", {}).get("reindex", {}).get("state")
             != "running", 30)
    s, _, _ = c.tool("kb_status")
    job = s.get("jobs", {}).get("reindex", {})
    check("unplug mid-reindex: job ended as aborted/failed, index not wiped",
          job.get("state") in ("aborted", "failed")
          and s.get("index", {}).get("files_indexed") == N_FILES,
          f"job={job.get('state')} files={s.get('index', {}).get('files_indexed')}")

    detach()
    attach(readonly=True)
    s, _, _ = c.tool("kb_status")
    check("read-only mount: state read_only", s.get("state") == "read_only" and s["mounted"],
          s.get("reason", ""))
    r, _, _ = c.tool("kb_search", {"query": "量子猫粮配方", "mode": "keyword"})
    check("read-only mount: search still works", r.get("count", 0) >= 1)
    r, _, _ = c.tool("kb_create_directory", {"relpath": "03_Notes/new"})
    check("read-only mount: writes refused with reason",
          r.get("error") == "drive_unavailable" and r.get("state") == "read_only")
    detach()
    attach()
    c.close()

    c = Client([PY, str(REPO / "server" / "kb_mcp_server.py")],
               env=env(KB_VOLUME=str(MOUNT / "NoSuchFolder")))
    c.initialize()
    s, _, _ = c.tool("kb_status")
    check("wrong folder on mounted drive: kb_dir_missing", s.get("state") == "kb_dir_missing")
    c.close()


def test_retrieval_old(old: Path) -> None:
    print("\n=== kb-retrieval (previous release, for comparison) ===", flush=True)
    c = Client([PY, str(old / "kb_mcp_server.py")], env=env(),
               stderr_path=str(LOGS / "retrieval_old.log"))
    c.initialize()
    rid = c.tool_send("kb_reindex", {"full": True})
    time.sleep(1.0)
    try:
        _, _, t = c.tool("kb_status", timeout=120)
        observe("old: kb_status latency during reindex", f"{t:.2f}s")
    except TimeoutError:
        observe("old: kb_status latency during reindex", "no answer within 120s")
    c.wait(rid, 180)
    detach()
    attach()
    try:
        s, is_err, _ = c.tool("kb_status", timeout=30)
        observe("old: kb_status after unplug+replug",
                (str(s)[:160] + (" [isError]" if is_err else "")))
    except (TimeoutError, BrokenPipeError, OSError) as exc:
        time.sleep(0.5)
        rc = c.proc.poll()
        how = (f"server process died, signal {signal.Signals(-rc).name}" if rc and rc < 0
               else f"server exited rc={rc}" if rc is not None else "server alive but silent")
        observe("old: kb_status after unplug+replug", f"no answer ({type(exc).__name__}); {how}")
    c.close()


def test_gateway_new() -> None:
    print("\n=== kb-files gateway (new) ===", flush=True)
    cache = WORK / "cache"
    detach()  # the drive is absent when Claude Desktop starts
    g = Client([PY, str(REPO / "server" / "fs_gateway.py"), "--", *FS_CMD],
               env=env(KB_CACHE_DIR=str(cache)), stderr_path=str(LOGS / "gateway.log"))
    _, t = g.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "kb-test", "version": "1"}})
    g.send("notifications/initialized", notify=True)
    check("gateway initializes with drive absent", t < 3, f"{t:.2f}s")
    msg, t = g.request("tools/list", {}, timeout=60)
    names = {x["name"] for x in msg["result"]["tools"]}
    check("drive absent: full filesystem tool list advertised",
          {"list_directory", "write_file", "read_text_file", "kb_status"} <= names,
          f"{len(names)} tools in {t:.1f}s")
    s, _, _ = g.tool("kb_status")
    check("drive absent: kb_status not_mounted",
          s.get("state") == "not_mounted" and s["filesystem_server"]["ready"] is False)
    s, is_err, _ = g.tool("list_directory", {"path": str(KB)})
    check("drive absent: file call fails with drive_not_mounted",
          is_err and s.get("error") == "drive_not_mounted" and s.get("hint"))

    attach()  # plugged in later -- no restart
    s, is_err, t = g.tool("list_directory", {"path": str(KB)}, timeout=90)
    check("drive plugged in later: same session lists the folder",
          not is_err and "_Inbox" in str(s), f"{t:.2f}s (lazy start)")
    note = KB / "_Inbox" / "gw-test.md"
    s, is_err, _ = g.tool("write_file", {"path": str(note), "content": "hello 小橘\n"})
    check("write_file works through gateway", not is_err and note.exists())
    s, is_err, _ = g.tool("read_text_file", {"path": str(note)})
    check("read_text_file works through gateway", not is_err and "小橘" in str(s))

    detach()
    s, is_err, _ = g.tool("list_directory", {"path": str(KB)})
    check("unplugged again: structured error", is_err and s.get("state") == "not_mounted")
    attach()
    s, is_err, _ = g.tool("list_directory", {"path": str(KB)})
    check("replugged: works again, no restart", not is_err and "_Inbox" in str(s))

    st, _, _ = g.tool("kb_status")
    pid = st["filesystem_server"].get("pid")
    kids = sh("pgrep", "-P", str(pid)).stdout.split() if pid else []
    for k in kids:
        try:
            os.kill(int(k), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(1.5)
    s, is_err, t = g.tool("list_directory", {"path": str(KB)}, timeout=90)
    st, _, _ = g.tool("kb_status")
    check("filesystem server crash: restarted on next call",
          not is_err and st["filesystem_server"]["starts"] >= 2, f"{t:.2f}s")
    msg, _ = g.request("tools/list", {})
    names2 = {x["name"] for x in msg["result"]["tools"]}
    check("tools/list stable across attach/detach", names2 == names)
    g.close()

    detach()
    g = Client([PY, str(REPO / "server" / "fs_gateway.py"), "--", *FS_CMD],
               env=env(KB_CACHE_DIR=str(cache)), stderr_path=str(LOGS / "gateway2.log"))
    g.initialize()
    msg, t = g.request("tools/list", {})
    check("second start uses cached tool list", t < 1.0 and
          {x["name"] for x in msg["result"]["tools"]} == names, f"{t:.2f}s")
    g.close()
    attach()


def test_gateway_old(old: Path) -> None:
    print("\n=== kb-files placeholder (previous release, for comparison) ===", flush=True)
    detach()
    c = Client([PY, str(old / "placeholder_server.py")], env=env())
    c.initialize()
    msg, _ = c.request("tools/list", {})
    before = [x["name"] for x in msg["result"]["tools"]]
    attach()
    msg, _ = c.request("tools/list", {})
    after = [x["name"] for x in msg["result"]["tools"]]
    observe("old: tools when started without drive", str(before))
    observe("old: tools after plugging the drive in", str(after) + " (stuck until restart)")
    c.close()


def test_launchers() -> None:
    print("\n=== launch scripts ===", flush=True)
    for script, name in (("mcp-launch-retrieval.sh", "kb-retrieval"),
                         ("mcp-launch-fs.sh", "kb-files")):
        c = Client([str(REPO / "scripts" / script)], env=env(KB_CACHE_DIR=str(WORK / "cache")),
                   stderr_path=str(LOGS / f"{script}.log"))
        try:
            c.initialize()
            s, is_err, t = c.tool("kb_status", timeout=60)
            check(f"{script} starts and answers kb_status", not is_err and s.get("state") == "ok",
                  f"{t:.2f}s")
        finally:
            c.close()


def main() -> int:
    print(f"work dir: {WORK}", flush=True)
    build_image()
    old = old_code_dir()
    try:
        test_retrieval_new()
        test_retrieval_old(old)
        test_gateway_new()
        test_gateway_old(old)
        test_launchers()
    finally:
        if MOUNT.exists():
            detach()
        shutil.rmtree(WORK / "old_server", ignore_errors=True)
        try:
            DMG.unlink()
        except OSError:
            pass
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nSUMMARY: {passed}/{len(results)} passed; logs in {LOGS}", flush=True)
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
