#!/usr/bin/env python3
"""End-to-end tests for the convenience tools (macOS, throw-away disk image).

Run with the server venv:  server/.venv/bin/python tests/e2e_features_macos.py
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
from mcp_client import Client  # noqa: E402

PY = sys.executable
VOLNAME = "KBFEAT"
MOUNT = Path("/Volumes") / VOLNAME
KB = MOUNT / "Database"
WORK = Path(tempfile.mkdtemp(prefix="kbfeat-"))
IMG = WORK / "kbfeat.sparseimage"
SRC = WORK / "import_src"
TODAY = datetime.now().strftime("%Y-%m-%d")

results: list = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
    return bool(cond)


def sh(*cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def wait_for(cond, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.3)
    return False


def attach() -> None:
    sh("hdiutil", "attach", "-nobrowse", str(IMG))
    if not wait_for(lambda: os.path.ismount(MOUNT)):
        raise SystemExit("attach failed")


def detach() -> None:
    sh("hdiutil", "detach", "-force", str(MOUNT))
    if not wait_for(lambda: not MOUNT.exists()):
        raise SystemExit("detach failed")


def paths(c: Client, query: str, **kw) -> list:
    s, _, _ = c.tool("kb_search", {"query": query, "mode": "keyword", **kw})
    return [r["path"] for r in s.get("results", [])]


def job(c: Client) -> dict:
    return c.tool("kb_status")[0].get("jobs", {}).get("reindex", {})


def main() -> int:
    if MOUNT.exists():
        raise SystemExit(f"{MOUNT} exists; eject it first")
    sh("hdiutil", "create", "-size", "512m", "-type", "SPARSE", "-fs", "APFS",
       "-volname", VOLNAME, "-ov", str(IMG))
    attach()
    for d in ("_Inbox", "01_Documents", "02_Books", "03_Notes", "04_Archive", ".mcp_meta"):
        (KB / d).mkdir(parents=True, exist_ok=True)
    (KB / ".mcp_meta" / ".kb_root").write_text("schema=1\nvolume=/Volumes/KBFEAT\n")
    for i, text in enumerate(["关于固态硬盘的选购笔记", "预算表：显示器两千", "旅行计划：新加坡"]):
        (KB / "01_Documents" / f"seed_{i}.md").write_text(text, encoding="utf-8")
    SRC.mkdir()
    env = dict(os.environ, KB_VOLUME=str(KB), KB_IMPORT_DIRS=str(SRC),
               PYTHONFAULTHANDLER="1", KB_AUTO_REINDEX="1")
    c = Client([PY, str(REPO / "server" / "kb_mcp_server.py")], env=env,
               stderr_path=str(WORK / "server.log"))
    try:
        init = c.initialize()
        ok = wait_for(lambda: job(c).get("state") == "done", 20)
        s, _, _ = c.tool("kb_status")
        check("auto reindex on startup", ok and s["index"]["files_indexed"] == 3,
              f"trigger={job(c).get('trigger')} files={s['index']['files_indexed']}")

        # --- kb_add_note ----------------------------------------------------- #
        s, _, _ = c.tool("kb_add_note", {"title": "会议纪要 2026/10: 硬盘", "content":
                                         "讨论了硬盘采购和猫粮预算。", "category": "03_Notes",
                                         "tags": ["会议"], "source": "微信"})
        note = s.get("path", "")
        check("add_note writes a clean, dated name",
              note.startswith(f"03_Notes/{TODAY}_会议纪要 2026_10_ 硬盘") and note.endswith(".md"),
              note)
        check("add_note is searchable immediately", note in paths(c, "猫粮预算"))
        text = (KB / note).read_text(encoding="utf-8")
        check("add_note header", text.startswith("---\ntitle: ") and 'source: "微信"' in text)
        s, _, _ = c.tool("kb_add_note", {"title": "另一个标题", "content": "讨论了硬盘采购和猫粮预算。\n"})
        check("add_note catches duplicate content", s.get("duplicate") is True
              and s.get("existing") == [note], str(s.get("existing")))
        s, _, _ = c.tool("kb_add_note", {"title": "v1.2 说明", "content": "版本说明"})
        check("dot in title keeps .md", s.get("path", "").endswith("v1.2 说明.md"), s.get("path", ""))
        s, _, _ = c.tool("kb_add_note", {"title": "x", "content": "y", "category": ".mcp_meta"})
        check("add_note refuses system folders", s.get("ok") is False, s.get("error", ""))

        # --- search -------------------------------------------------------- #
        check("multi-word search", note in paths(c, "硬盘 猫粮"))
        check("folder filter", note not in paths(c, "硬盘", folder="01_Documents")
              and "01_Documents/seed_0.md" in paths(c, "硬盘", folder="01_Documents"))
        s, _, _ = c.tool("kb_search", {"query": "会议纪要"})
        check("filename match", s["results"] and s["results"][0]["source"] == "filename")

        # --- kb_import_file -------------------------------------------------- #
        (SRC / "季度报告.txt").write_text("钛合金外壳的成本分析", encoding="utf-8")
        s, _, _ = c.tool("kb_import_file", {"source_path": str(SRC / "季度报告.txt"),
                                            "category": "01_Documents"})
        check("import file", s.get("imported") == ["01_Documents/季度报告.txt"], str(s.get("imported")))
        check("imported file searchable immediately",
              "01_Documents/季度报告.txt" in paths(c, "钛合金外壳"))
        check("source kept by default", (SRC / "季度报告.txt").exists())
        s, _, _ = c.tool("kb_import_file", {"source_path": str(SRC / "季度报告.txt")})
        check("import skips duplicates", s.get("count") == 0 and s.get("skipped_duplicates"),
              str(s.get("skipped_duplicates")))
        folder = SRC / "旅行资料"
        (folder / "照片").mkdir(parents=True)
        (folder / "行程.md").write_text("第一天去滨海湾花园", encoding="utf-8")
        (folder / "照片" / "a.jpg").write_bytes(b"\xff\xd8\xff" + os.urandom(2048))
        (folder / ".DS_Store").write_bytes(b"junk")
        s, _, _ = c.tool("kb_import_file", {"source_path": str(folder), "move": True})
        check("import folder (hidden files skipped)", s.get("count") == 2
              and s.get("stored_not_searchable") == ["_Inbox/旅行资料/照片/a.jpg"], str(s.get("imported")))
        check("import with move removes source", not (folder / "行程.md").exists())
        s, _, _ = c.tool("kb_import_file", {"source_path": "/etc/hosts"})
        check("import refuses folders outside the allow-list", s.get("error") == "source_not_allowed")

        # --- kb_organize + INDEX.md auto refresh --------------------------------- #
        (KB / "_Inbox" / "scan1.txt").write_text("发票：键盘三百元", encoding="utf-8")
        (KB / "_Inbox" / "scan2.txt").write_text("说明书：显示器支架", encoding="utf-8")
        s, _, _ = c.tool("kb_organize", {"moves": [
            {"src": "_Inbox/scan1.txt", "dest_dir": "01_Documents/票据", "new_name": "2026-10-01_发票_键盘"},
            {"src": "_Inbox/scan2.txt", "dest_dir": "02_Books", "new_name": "2026-10-02_说明书_显示器支架.txt"},
            {"src": "_Inbox/nope.txt", "dest_dir": "02_Books"},
        ]})
        check("organize moves many in one call", s.get("moved") == 2 and s.get("failed") == 1,
              str([r.get("to") for r in s.get("results", [])]))
        check("organize keeps extension & indexes moved files",
              "01_Documents/票据/2026-10-01_发票_键盘.txt" in paths(c, "键盘三百元"))
        ok = wait_for(lambda: "2026-10-02_说明书_显示器支架.txt" in
                      (KB / "INDEX.md").read_text(encoding="utf-8")
                      if (KB / "INDEX.md").exists() else False, 10)
        check("INDEX.md refreshes by itself", ok)

        # --- trash & restore ---------------------------------------------------- #
        s, _, _ = c.tool("kb_trash", {"relpath": "01_Documents/seed_2.md", "reason": "test"})
        trashed = s.get("to", "")
        check("trash moves to _Trash/<date>", trashed == f"_Trash/{TODAY}/seed_2.md", trashed)
        check("trashed file leaves search", "01_Documents/seed_2.md" not in paths(c, "新加坡"))
        s, _, _ = c.tool("kb_move_file", {"src_rel": trashed, "dest_dir_rel": "01_Documents"})
        check("restore from trash is searchable again", "01_Documents/seed_2.md" in paths(c, "新加坡"))
        s, _, _ = c.tool("kb_trash", {"relpath": "01_Documents"})
        check("trash refuses category folders", s.get("error") == "protected")

        # --- duplicates ---------------------------------------------------------- #
        c.tool("kb_add_note", {"title": "副本", "content": "讨论了硬盘采购和猫粮预算。",
                               "category": "03_Notes", "allow_duplicate": True})
        shutil.copy(KB / "01_Documents" / "seed_1.md", KB / "03_Notes" / "seed_1_copy.md")
        c.tool("kb_health_check", {"fix": True})
        s, _, _ = c.tool("kb_find_duplicates")
        groups = [sorted(g["paths"]) for g in s.get("groups", [])]
        check("find_duplicates", ["01_Documents/seed_1.md", "03_Notes/seed_1_copy.md"] in groups,
              str(groups))

        # --- read_document paging ------------------------------------------------- #
        long_text = "".join(f"第{i}段。" * 20 + "\n" for i in range(200))
        s, _, _ = c.tool("kb_add_note", {"title": "长文", "content": long_text})
        p1, _, _ = c.tool("kb_read_document", {"relpath": s["path"], "max_chars": 6000})
        p2, _, _ = c.tool("kb_read_document", {"relpath": s["path"], "max_chars": 6000,
                                               "start_char": p1.get("next_start_char", 0)})
        check("read_document pages", p1.get("next_start_char") == 6000 and p2.get("start_char") == 6000
              and p1["total_chars"] > 12000, f"total={p1.get('total_chars')}")

        # --- health check fix ------------------------------------------------------ #
        (KB / "04_Archive" / "finder_drop.md").write_text("访达直接拖进来的文件", encoding="utf-8")
        before, _, _ = c.tool("kb_health_check")
        after, _, _ = c.tool("kb_health_check", {"fix": True})
        check("health_check(fix) indexes + rewrites INDEX.md",
              before["summary"]["unindexed"] >= 1 and after["summary"]["unindexed"] == 0
              and after["summary"]["orphans"] == 0 and after["fixed"]["indexed"] >= 1,
              f"before={before['summary']} after={after['summary']}")

        # --- re-plug: catch-up reindex ---------------------------------------------- #
        (KB / "03_Notes" / "plugged_later.md").write_text("拔盘期间在别的电脑写的笔记", encoding="utf-8")
        detach()
        c.tool("kb_status")
        attach()
        c.tool("kb_status")  # first call after re-plug notices the transition
        ok = wait_for(lambda: job(c).get("trigger") == "remount" and job(c).get("state") == "done", 20)
        check("re-plug triggers catch-up reindex",
              ok and "03_Notes/plugged_later.md" in paths(c, "别的电脑"), str(job(c).get("trigger")))

        # --- cross-process reindex lock ------------------------------------------------ #
        lock = open(KB / ".mcp_meta" / "reindex.lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s, _, _ = c.tool("kb_reindex", {"wait_seconds": 0})
        check("reindex defers to another instance", "Another kb-retrieval instance" in s.get("message", ""))
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

        # --- prompts ---------------------------------------------------------------- #
        m, _ = c.request("prompts/list", {})
        names = {p["name"]: p.get("title") for p in m["result"]["prompts"]}
        check("prompts in the + menu", names.get("organize_inbox") == "一键整理收件箱"
              and {"add_to_kb", "kb_checkup", "ask_kb"} <= set(names), str(names))
        m, _ = c.request("prompts/get", {"name": "add_to_kb", "arguments": {"material": "测试"}})
        check("prompt renders", "kb_add_note" in m["result"]["messages"][0]["content"]["text"])
        check("server instructions sent", "kb_add_note" in (init["result"].get("instructions") or ""))
    finally:
        c.close()
        if MOUNT.exists():
            detach()
        shutil.rmtree(WORK, ignore_errors=True)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\nSUMMARY: {passed}/{len(results)} passed", flush=True)
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
