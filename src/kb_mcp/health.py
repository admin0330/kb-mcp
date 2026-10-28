"""Mount / health probing for the knowledge-base volume.

Every status question ("is the drive there?") goes through :class:`Prober`.
It answers with a precise *state* instead of a bare boolean, and it never
hangs the caller: the filesystem checks run in a helper thread with a hard
timeout, so a sleeping or half-disconnected USB drive is reported as
``unresponsive`` instead of freezing the MCP server.

States (``result["state"]``)::

    ok                 mounted, readable and writable
    not_mounted        the path does not exist (drive unplugged / not mounted)
    phantom_dir        the path exists but lives on the system disk, not on the
                       external volume (leftover folder under /Volumes)
    kb_dir_missing     the volume is mounted but the knowledge-base folder is
                       missing (wrong drive, renamed folder, not initialised)
    not_a_directory    the path exists but is a file
    permission_denied  the OS refuses access (macOS privacy / TCC settings)
    read_only          mounted read-only, or the write test failed with EROFS
    disk_full          the write test failed with ENOSPC
    unresponsive       the checks did not finish within the timeout
    io_error           any other OS error (EIO etc.)
"""
from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

META_DIRNAME = ".mcp_meta"
MARKER_FILENAME = ".kb_root"

OK = "ok"
NOT_MOUNTED = "not_mounted"
PHANTOM_DIR = "phantom_dir"
KB_DIR_MISSING = "kb_dir_missing"
NOT_A_DIRECTORY = "not_a_directory"
PERMISSION_DENIED = "permission_denied"
READ_ONLY = "read_only"
DISK_FULL = "disk_full"
UNRESPONSIVE = "unresponsive"
IO_ERROR = "io_error"

HINTS: Dict[str, str] = {
    OK: "",
    NOT_MOUNTED: "硬盘未连接或未挂载：插上硬盘，等它出现在访达侧边栏后重试即可，无需重启 Claude。",
    PHANTOM_DIR: "该路径是系统盘上的残留文件夹，不是外置硬盘本身。请确认硬盘已插好；"
                 "若硬盘被挂到了其他路径（见 candidates），推出后删除残留文件夹再重新插入。",
    KB_DIR_MISSING: "硬盘已挂载，但找不到知识库文件夹：可能插错了盘、文件夹被改名，"
                    "或尚未运行 scripts/init_drive.sh 初始化。",
    NOT_A_DIRECTORY: "KB_VOLUME 指向的是一个文件而不是文件夹，请检查配置。",
    PERMISSION_DENIED: "系统拒绝访问：打开 系统设置 → 隐私与安全性 → 文件与文件夹，"
                       "允许 Claude 访问“可移除的宗卷”，然后重试。",
    READ_ONLY: "硬盘处于只读状态：检查是否以只读方式挂载，或用“磁盘工具”做急救后重新插入。",
    DISK_FULL: "硬盘空间已满，请清理空间后重试。",
    UNRESPONSIVE: "硬盘没有响应（可能在休眠、USB 接触不良或正在推出）：稍等几秒重试；"
                  "持续出现请重新插拔硬盘。",
    IO_ERROR: "读写硬盘时出现 I/O 错误：请重新插拔硬盘，必要时用“磁盘工具”急救。",
}


def _env_flag(name: str) -> Optional[bool]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def mount_root_of(volume: Path) -> Optional[Path]:
    """For ``/Volumes/<name>/...`` return ``/Volumes/<name>``; else ``None``."""
    parts = volume.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        return Path("/", "Volumes", parts[2])
    return None


class Prober:
    """Thread-safe, timeout-bounded health checks for one volume path."""

    def __init__(self, volume: Path, timeout: float = 4.0, cache_ttl: float = 0.0,
                 require_external: Optional[bool] = None) -> None:
        self.volume = Path(volume)
        self.timeout = float(timeout)
        self.cache_ttl = float(cache_ttl)
        self.mount_root = mount_root_of(self.volume)
        env_flag = _env_flag("KB_REQUIRE_EXTERNAL")
        if require_external is None:
            require_external = env_flag if env_flag is not None else self.mount_root is not None
        self.require_external = bool(require_external) and self.mount_root is not None
        self._lock = threading.Lock()
        self._inflight: Optional[threading.Thread] = None
        self._inflight_started = 0.0
        self._cache: Dict[bool, tuple] = {}
        self._last_ok_at: Optional[float] = None
        self._last_state: Optional[str] = None
        self._state_since = time.time()

    # ------------------------------------------------------------------ #
    def probe(self, write_test: bool = False, use_cache: bool = True) -> dict:
        """Return a fresh (or very recent) health report. Never raises."""
        now = time.monotonic()
        if use_cache:
            # Only healthy results are reused: right after the drive comes
            # back, the very next call must see it.
            for key in ((True, False) if not write_test else (True,)):
                hit = self._cache.get(key)
                if hit and hit[1]["state"] == OK and now - hit[0] < self.cache_ttl:
                    return dict(hit[1])
        result = self._run_bounded(lambda: self._probe_once(write_test))
        if result["state"] == OK:
            self._cache[write_test] = (time.monotonic(), result)
        else:
            self._cache.clear()  # a failure invalidates every earlier "ok"
        self._remember(result)
        return dict(result)

    def is_ok(self) -> bool:
        return self.probe()["state"] == OK

    # ------------------------------------------------------------------ #
    def _remember(self, result: dict) -> None:
        state = result["state"]
        if state != self._last_state:
            self._last_state = state
            self._state_since = time.time()
        if state == OK:
            self._last_ok_at = time.time()
        result["state_since"] = round(self._state_since, 3)
        result["last_ok_at"] = round(self._last_ok_at, 3) if self._last_ok_at else None

    def _base(self, state: str, **extra) -> dict:
        mounted = state in (OK, READ_ONLY, DISK_FULL, KB_DIR_MISSING)
        out = {
            "state": state,
            "volume": str(self.volume),
            "mounted": mounted,
            "readable": state in (OK, READ_ONLY, DISK_FULL),
            "writable": state == OK,
            "hint": HINTS.get(state, ""),
        }
        out.update(extra)
        return out

    def _run_bounded(self, fn: Callable[[], dict]) -> dict:
        started = time.monotonic()
        box: dict = {}

        def target() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001 - report, never raise
                box["error"] = exc

        while True:
            with self._lock:
                other = self._inflight
                if other is None or not other.is_alive():
                    thread = threading.Thread(target=target, name="kb-probe", daemon=True)
                    self._inflight = thread
                    self._inflight_started = started = time.monotonic()
                    thread.start()
                    break
                other_started = self._inflight_started
            # Another thread is checking right now: wait for it instead of
            # starting a second check, but never past its own deadline.
            remaining = other_started + self.timeout - time.monotonic()
            if remaining > 0:
                other.join(remaining)
            if other.is_alive():
                stuck = time.monotonic() - other_started
                return self._base(UNRESPONSIVE, reason=(
                    f"a previous check has been blocked for {stuck:.1f}s"))
        thread.join(self.timeout)
        if thread.is_alive():
            return self._base(UNRESPONSIVE, reason=(
                f"filesystem checks did not finish within {self.timeout:.1f}s"))
        with self._lock:
            if self._inflight is thread:
                self._inflight = None
        if "error" in box:
            exc = box["error"]
            return self._base(IO_ERROR, reason=f"{type(exc).__name__}: {exc}")
        result = box["result"]
        result.setdefault("checks", {})["latency_ms"] = round(
            (time.monotonic() - started) * 1000, 1)
        return result

    # ------------------------------------------------------------------ #
    def _probe_once(self, write_test: bool) -> dict:
        vol = self.volume
        checks: dict = {}
        try:
            st = os.stat(vol)
        except FileNotFoundError:
            return self._missing(checks)
        except PermissionError as exc:
            return self._base(PERMISSION_DENIED, reason=str(exc), checks=checks)
        except OSError as exc:
            return self._os_error(exc, checks)

        import stat as stat_mod
        if not stat_mod.S_ISDIR(st.st_mode):
            return self._base(NOT_A_DIRECTORY, reason=f"{vol} is not a directory",
                              checks=checks)

        if self.require_external and self.mount_root is not None:
            is_mount = os.path.ismount(self.mount_root)
            checks["mount_point"] = str(self.mount_root)
            checks["is_mount"] = is_mount
            if not is_mount:
                return self._base(PHANTOM_DIR, reason=(
                    f"{self.mount_root} is a plain folder on the system disk, "
                    "not a mounted volume"), checks=checks,
                    candidates=self._find_candidates())

        try:
            with os.scandir(vol) as it:
                for _ in it:
                    break
        except PermissionError as exc:
            return self._base(PERMISSION_DENIED, reason=str(exc), checks=checks)
        except OSError as exc:
            return self._os_error(exc, checks)

        checks["marker"] = os.path.isfile(vol / META_DIRNAME / MARKER_FILENAME)
        try:
            vfs = os.statvfs(vol)
            checks["free_gb"] = round(vfs.f_bavail * vfs.f_frsize / 1024 ** 3, 2)
            checks["total_gb"] = round(vfs.f_blocks * vfs.f_frsize / 1024 ** 3, 2)
            fs_readonly = bool(vfs.f_flag & getattr(os, "ST_RDONLY", 1))
        except OSError:
            fs_readonly = False
        if fs_readonly:
            return self._base(READ_ONLY, reason="filesystem is mounted read-only",
                              checks=checks)
        if not os.access(vol, os.W_OK):
            return self._base(READ_ONLY, reason=f"no write permission on {vol}",
                              checks=checks)

        if write_test:
            meta = vol / META_DIRNAME
            probe_dir = meta if meta.is_dir() else vol
            probe_file = probe_dir / f".kb_probe_{os.getpid()}"
            try:
                with open(probe_file, "w", encoding="utf-8") as fh:
                    fh.write("ok")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.unlink(probe_file)
                checks["write_test"] = "passed"
            except OSError as exc:
                checks["write_test"] = f"failed: {exc}"
                try:
                    os.unlink(probe_file)
                except OSError:
                    pass
                if exc.errno == errno.EROFS:
                    return self._base(READ_ONLY, reason=str(exc), checks=checks)
                if exc.errno == errno.ENOSPC:
                    return self._base(DISK_FULL, reason=str(exc), checks=checks)
                if exc.errno in (errno.EACCES, errno.EPERM):
                    return self._base(PERMISSION_DENIED, reason=str(exc), checks=checks)
                return self._os_error(exc, checks)

        return self._base(OK, checks=checks)

    def _missing(self, checks: dict) -> dict:
        root = self.mount_root
        if root is not None and root != self.volume:
            try:
                if os.path.ismount(root):
                    checks["mount_point"] = str(root)
                    return self._base(KB_DIR_MISSING, reason=(
                        f"{root} is mounted but {self.volume.name} was not found on it"),
                        checks=checks, candidates=self._find_candidates())
            except OSError:
                pass
        return self._base(NOT_MOUNTED, reason=f"{self.volume} does not exist",
                          checks=checks, candidates=self._find_candidates())

    def _os_error(self, exc: OSError, checks: dict) -> dict:
        code = errno.errorcode.get(exc.errno or 0, str(exc.errno))
        if exc.errno == errno.EROFS:
            return self._base(READ_ONLY, reason=str(exc), checks=checks)
        if exc.errno in (errno.EACCES, errno.EPERM):
            return self._base(PERMISSION_DENIED, reason=str(exc), checks=checks)
        if exc.errno in (errno.ENOENT, errno.ENXIO, errno.ENODEV):
            return self._base(NOT_MOUNTED, reason=f"{code}: {exc}", checks=checks)
        return self._base(IO_ERROR, reason=f"{code}: {exc}", checks=checks)

    @staticmethod
    def _marker_names(marker: Path, volume_name: str) -> bool:
        """True when a .kb_root marker was written for a volume of this name."""
        try:
            for line in marker.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("volume="):
                    return volume_name in Path(line.split("=", 1)[1].strip()).parts
        except OSError:
            pass
        return False

    def _find_candidates(self) -> List[str]:
        """Look for the same knowledge base mounted elsewhere (e.g. 'Name 1')."""
        root = self.mount_root
        if root is None:
            return []
        try:
            rest = self.volume.relative_to(root)
        except ValueError:
            return []
        found: List[str] = []
        try:
            entries = sorted(os.listdir("/Volumes"))
        except OSError:
            return []
        for name in entries:
            if name == root.name:
                continue
            candidate = Path("/Volumes", name) / rest
            try:
                if name.startswith(root.name) and candidate.is_dir():
                    found.append(str(candidate))  # e.g. "KnowledgeBase 1"
                    continue
                marker = candidate / META_DIRNAME / MARKER_FILENAME
                if marker.is_file() and self._marker_names(marker, root.name):
                    found.append(str(candidate))  # same KB, renamed volume
            except OSError:
                continue
            if len(found) >= 5:
                break
        return found
