#!/usr/bin/env python3
"""kb-files gateway: a stable stdio front for the official filesystem server.

Why this exists
---------------
Claude Desktop reads each MCP server's tool list once, when the server starts,
and ignores ``notifications/tools/list_changed``. The old launcher decided at
start-up: drive mounted -> run ``@modelcontextprotocol/server-filesystem``;
drive missing -> run a one-tool placeholder. If the drive was plugged in a
minute late, kb-files stayed a placeholder until Claude Desktop was restarted.

The gateway removes that start-up decision:

* It always advertises the full filesystem tool list (cached from the last
  real run, or discovered once against an empty temp dir) plus ``kb_status``.
* It starts the real filesystem server lazily, the first time a call arrives
  while the drive is usable, and starts it again if it ever exits.
* While the drive is unusable, calls get a structured error with the drive
  state and a hint (not mounted / read-only / no permission / unresponsive)
  instead of a dead server.

Usage (see scripts/mcp-launch-fs.sh)::

    fs_gateway.py -- npx -y @modelcontextprotocol/server-filesystem /Volumes/X/Database
"""
from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from . import health  # noqa: E402
except ImportError:
    import health  # type: ignore  # noqa: E402

__version__ = "0.3.0"

VOLUME = Path(os.environ.get("KB_VOLUME", "/Volumes/KnowledgeBase/Database"))
CACHE_DIR = Path(os.environ.get("KB_CACHE_DIR", str(Path.home() / ".cache" / "kb-mcp")))
CACHE_FILE = CACHE_DIR / "fs_tools.json"
START_TIMEOUT = float(os.environ.get("KB_FS_START_TIMEOUT", "45"))
RESTART_BACKOFF = 5.0
DEFAULT_PROTOCOL = "2025-06-18"

STATUS_TOOL = {
    "name": "kb_status",
    "description": (
        "Report whether the knowledge-base drive is usable for file access and "
        "whether the underlying filesystem server is running. Always answers. "
        "state: ok | not_mounted | phantom_dir | kb_dir_missing | permission_denied "
        "| read_only | disk_full | unresponsive | io_error."),
    "inputSchema": {"type": "object", "properties": {}},
}
# Tools that only read: still allowed while the drive is mounted read-only.
READ_ONLY_TOOLS = {
    "read_file", "read_text_file", "read_media_file", "read_multiple_files",
    "list_directory", "list_directory_with_sizes", "directory_tree",
    "search_files", "get_file_info", "list_allowed_directories",
}
EMPTY_LISTS = {
    "resources/list": {"resources": []},
    "resources/templates/list": {"resourceTemplates": []},
    "prompts/list": {"prompts": []},
}


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[kb-files-gateway {stamp}] {message}", file=sys.stderr, flush=True)


def _dump(msg: Any) -> bytes:
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class Child:
    """One running instance of the real filesystem server."""

    def __init__(self, cmd: List[str], on_message, on_exit) -> None:
        self.cmd = cmd
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.write_lock = threading.Lock()
        self.stderr_tail: Deque[str] = collections.deque(maxlen=20)
        self._on_message = on_message
        self._on_exit = on_exit
        threading.Thread(target=self._read_stdout, name="child-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="child-stderr", daemon=True).start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, data: bytes) -> bool:
        try:
            with self.write_lock:
                assert self.proc.stdin is not None
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            if raw.strip():
                self._on_message(self, raw)
        code = self.proc.wait()
        self._on_exit(self, code)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self.stderr_tail.append(line)
                print(f"[filesystem] {line}", file=sys.stderr, flush=True)

    def kill(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


class Gateway:
    def __init__(self, child_cmd: List[str]) -> None:
        self.child_cmd = child_cmd
        self.prober = health.Prober(VOLUME, timeout=float(os.environ.get("KB_PROBE_TIMEOUT", "4")))
        self.out_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.child: Optional[Child] = None
        self.ready = False
        self.client_init: Optional[dict] = None
        self.pending: Dict[Any, str] = {}        # forwarded client request id -> method
        self.internal: Dict[str, dict] = {}      # our own requests to the child
        self.seq = 0
        self.starts = 0
        self.last_error: Optional[str] = None
        self.spawn_error: Optional[str] = None
        self.spawn_failed_at = 0.0
        self.last_state: Optional[str] = None
        self.started_at = time.time()
        self.cache = self._load_cache()

    # ---- output -------------------------------------------------------- #
    def write(self, data: bytes) -> None:
        with self.out_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def reply(self, msg_id: Any, result: dict) -> None:
        self.write(_dump({"jsonrpc": "2.0", "id": msg_id, "result": result}))

    def reply_error(self, msg_id: Any, code: int, message: str) -> None:
        self.write(_dump({"jsonrpc": "2.0", "id": msg_id,
                          "error": {"code": code, "message": message}}))

    def tool_text(self, msg_id: Any, payload: dict, is_error: bool) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self.reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})

    # ---- cache --------------------------------------------------------- #
    def _load_cache(self) -> dict:
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data.get("tools"), list) and data["tools"]:
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _save_cache(self) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, CACHE_FILE)
        except OSError as exc:
            log(f"could not write tool cache: {exc}")

    # ---- drive --------------------------------------------------------- #
    def probe(self, **kw) -> dict:
        result = self.prober.probe(**kw)
        if result["state"] != self.last_state:
            log(f"drive state: {self.last_state or 'unknown'} -> {result['state']}")
            self.last_state = result["state"]
        return result

    # ---- child management ---------------------------------------------- #
    def _internal_request(self, child: Child, method: str, params: dict,
                          timeout: float) -> dict:
        with self.state_lock:
            self.seq += 1
            req_id = f"kbgw-{self.seq}"
            slot = {"event": threading.Event(), "msg": None, "child": child}
            self.internal[req_id] = slot
        if not child.alive() or not child.send(
                _dump({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})):
            with self.state_lock:
                self.internal.pop(req_id, None)
            raise RuntimeError(f"filesystem server exited before {method}: "
                               + " | ".join(list(child.stderr_tail)[-3:]))
        if not slot["event"].wait(timeout):
            with self.state_lock:
                self.internal.pop(req_id, None)
            raise TimeoutError(f"{method} did not answer within {timeout:.0f}s")
        msg = slot["msg"]
        if msg is None:
            raise RuntimeError(f"filesystem server exited during {method}: "
                               + " | ".join(list(child.stderr_tail)[-3:]))
        if "error" in msg:
            raise RuntimeError(f"{method} failed: {msg['error']}")
        return msg.get("result") or {}

    @staticmethod
    def _child_init_params(params: dict) -> dict:
        # The gateway pins the allowed directory to the drive via argv. Hide
        # client "roots" support so the child never swaps it for client roots
        # (and never sends roots/list requests we would have to route).
        out = dict(params)
        caps = dict(out.get("capabilities") or {})
        caps.pop("roots", None)
        out["capabilities"] = caps
        return out

    def _spawn(self, cmd: List[str], init_params: dict) -> Child:
        init_params = self._child_init_params(init_params)
        child = Child(cmd, self._on_child_message, self._on_child_exit)
        try:
            init = self._internal_request(child, "initialize", init_params, START_TIMEOUT)
            child.send(_dump({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            tools = self._internal_request(child, "tools/list", {}, 15).get("tools") or []
        except Exception:
            child.kill()
            raise
        if tools:
            self.cache = {
                "tools": tools,
                "init": {k: init.get(k) for k in
                         ("protocolVersion", "capabilities", "serverInfo", "instructions")},
                "updated": datetime.now().isoformat(timespec="seconds"),
            }
            self._save_cache()
        return child

    def ensure_child(self) -> Optional[str]:
        """Start the real server if needed. Returns an error string or None."""
        with self.start_lock:
            if self.ready and self.child is not None and self.child.alive():
                return None
            since = time.time() - self.spawn_failed_at
            if self.spawn_error and since < RESTART_BACKOFF:
                return f"filesystem server failed to start {since:.0f}s ago: {self.spawn_error}"
            params = self.client_init or {
                "protocolVersion": DEFAULT_PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "kb-files-gateway", "version": __version__}}
            try:
                self.starts += 1
                log(f"starting filesystem server (#{self.starts}): {' '.join(self.child_cmd)}")
                child = self._spawn(self.child_cmd, params)
            except Exception as exc:  # noqa: BLE001
                self.spawn_error = self.last_error = f"{type(exc).__name__}: {exc}"
                self.spawn_failed_at = time.time()
                log(f"filesystem server did not start: {self.last_error}")
                return self.last_error
            with self.state_lock:
                self.child, self.ready = child, True
                self.last_error = self.spawn_error = None
            log("filesystem server ready")
            return None

    def discover_tools(self) -> None:
        """No cache yet and no drive: learn the tool list against an empty dir."""
        with self.start_lock:
            if self.cache.get("tools"):
                return
            tmp = tempfile.mkdtemp(prefix="kb-fs-discover-")
            cmd = list(self.child_cmd[:-1]) + [tmp] if self.child_cmd else []
            params = self.client_init or {
                "protocolVersion": DEFAULT_PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "kb-files-gateway", "version": __version__}}
            try:
                log("discovering filesystem tool list (no cache yet)")
                child = self._spawn(cmd, params)
                child.kill()
            except Exception as exc:  # noqa: BLE001
                log(f"tool discovery failed: {exc}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def _on_child_message(self, child: Child, raw: bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            log(f"non-JSON line from filesystem server dropped: {raw[:200]!r}")
            return
        if isinstance(msg, dict) and "method" not in msg and "id" in msg:
            with self.state_lock:
                slot = self.internal.pop(msg["id"], None) if isinstance(msg["id"], str) else None
                method = None if slot else self.pending.pop(msg["id"], None)
            if slot is not None:
                slot["msg"] = msg
                slot["event"].set()
                return
            if child is not self.child:
                return  # late answer from an old child; the client was already answered
            if method == "tools/call" and isinstance(msg.get("result"), dict) \
                    and msg["result"].get("isError"):
                # The drive may have gone away after our pre-check.
                probe = self.probe(use_cache=False)
                if probe["state"] not in (health.OK, health.READ_ONLY, health.DISK_FULL):
                    self.tool_text(msg["id"], self._drive_payload(probe, "file call"), True)
                    return
            if method == "tools/list" and isinstance(msg.get("result"), dict):
                tools = [t for t in msg["result"].get("tools", []) if t.get("name") != "kb_status"]
                if tools:
                    self.cache["tools"] = tools
                    self._save_cache()
                msg["result"]["tools"] = tools + [STATUS_TOOL]
                self.write(_dump(msg))
                return
        if child is self.child:
            self.write(raw if raw.endswith(b"\n") else raw + b"\n")
        elif isinstance(msg, dict) and "method" in msg and "id" in msg:
            child.send(_dump({"jsonrpc": "2.0", "id": msg["id"],
                              "error": {"code": -32601, "message": "not available"}}))

    def _on_child_exit(self, child: Child, code: int) -> None:
        with self.state_lock:
            for slot in list(self.internal.values()):
                if slot.get("child") is child:
                    slot["event"].set()
            if child is not self.child:
                return
            self.child, self.ready = None, False
            orphaned = dict(self.pending)
            self.pending.clear()
        tail = " | ".join(list(child.stderr_tail)[-3:])
        self.last_error = f"exited with code {code}" + (f": {tail}" if tail else "")
        log(f"filesystem server {self.last_error}")
        for msg_id, method in orphaned.items():
            if method == "tools/call":
                self.tool_text(msg_id, {
                    "ok": False, "error": "filesystem_server_exited",
                    "message": "The filesystem server stopped while handling this call. "
                               "Retry; it will be restarted automatically.",
                    "drive": self.probe(use_cache=False)}, True)
            else:
                self.reply_error(msg_id, -32603, "filesystem server exited")

    def forward(self, msg: dict, raw: bytes) -> bool:
        with self.state_lock:
            child = self.child if self.ready else None
            if child is None:
                return False
            if "id" in msg and "method" in msg:
                self.pending[msg["id"]] = msg["method"]
        if child.send(raw):
            return True
        with self.state_lock:
            self.pending.pop(msg.get("id"), None)
        return False

    # ---- local answers ------------------------------------------------- #
    def status_payload(self) -> dict:
        probe = self.probe(write_test=True, use_cache=False)
        child = self.child
        payload = {
            "ok": True,
            "server": "kb-files",
            "version": __version__,
            "mounted": probe["mounted"],
            "state": probe["state"],
            "writable": probe["writable"],
            "volume": str(VOLUME),
            "hint": probe.get("hint") or None,
            "reason": probe.get("reason"),
            "candidates": probe.get("candidates") or None,
            "checks": probe.get("checks"),
            "filesystem_server": {
                "running": bool(child and child.alive()),
                "ready": self.ready,
                "pid": child.proc.pid if child and child.alive() else None,
                "starts": self.starts,
                "last_error": self.last_error,
                "command": " ".join(self.child_cmd),
                "note": None if self.ready else
                "Started automatically on the first file call while the drive is usable.",
            },
            "tools_advertised": len(self.cache.get("tools") or []),
            "tool_cache_updated": self.cache.get("updated"),
            "uptime_s": round(time.time() - self.started_at),
        }
        payload["filesystem_server"] = {k: v for k, v in payload["filesystem_server"].items()
                                        if v is not None}
        return {k: v for k, v in payload.items() if v is not None}

    @staticmethod
    def _drive_payload(probe: dict, what: str) -> dict:
        payload = {
            "ok": False,
            "error": "drive_not_mounted" if not probe["mounted"] else "drive_unavailable",
            "state": probe["state"],
            "message": f"Knowledge-base drive is not usable for {what} "
                       f"(state={probe['state']}). {probe.get('reason', '')}".strip(),
            "hint": probe.get("hint") or None,
            "candidates": probe.get("candidates") or None,
            "volume": str(VOLUME),
            "note": "No restart needed: the call works as soon as the drive is back.",
        }
        return {k: v for k, v in payload.items() if v is not None}

    def handle_call(self, msg: dict, raw: bytes) -> None:
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        name = params.get("name")
        if name == "kb_status":
            self.tool_text(msg_id, self.status_payload(), False)
            return
        probe = self.probe()
        allowed = (health.OK, health.READ_ONLY, health.DISK_FULL) if name in READ_ONLY_TOOLS \
            else (health.OK,)
        if probe["state"] not in allowed:
            self.tool_text(msg_id, self._drive_payload(probe, f"'{name}'"), True)
            return
        err = self.ensure_child()
        if err is None and self.forward(msg, raw):
            return
        self.tool_text(msg_id, {
            "ok": False, "error": "filesystem_server_unavailable",
            "message": err or "could not reach the filesystem server",
            "hint": "官方文件服务没能启动：检查 Node/npx 是否可用（终端运行 npx -v），"
                    "首次使用需要联网下载 @modelcontextprotocol/server-filesystem。",
        }, True)

    def handle_request(self, msg: dict, raw: bytes) -> None:
        method, msg_id = msg.get("method"), msg.get("id")
        if method == "initialize":
            self.client_init = msg.get("params") or {}
            cached = self.cache.get("init") or {}
            capabilities = dict(cached.get("capabilities") or {})
            capabilities["tools"] = dict(capabilities.get("tools") or {})
            server_info = {"name": "kb-files",
                           "version": f"{__version__} (gateway)"}
            if cached.get("serverInfo"):
                inner = cached["serverInfo"]
                server_info["version"] += f" + {inner.get('name')} {inner.get('version')}"
            result = {
                "protocolVersion": self.client_init.get("protocolVersion") or DEFAULT_PROTOCOL,
                "capabilities": capabilities,
                "serverInfo": server_info,
            }
            if cached.get("instructions"):
                result["instructions"] = cached["instructions"]
            self.reply(msg_id, result)
            # Warm up in the background so the first real call is fast.
            threading.Thread(target=self._warm_up, name="warm-up", daemon=True).start()
            return
        if method == "ping":
            self.reply(msg_id, {})
            return
        if method == "tools/list":
            if self.forward(msg, raw):
                return
            if not self.cache.get("tools"):
                self.discover_tools()
            self.reply(msg_id, {"tools": list(self.cache.get("tools") or []) + [STATUS_TOOL]})
            return
        if method == "tools/call":
            # May block while the child starts; keep reading other messages.
            threading.Thread(target=self.handle_call, args=(msg, raw),
                             name="tools-call", daemon=True).start()
            return
        if self.forward(msg, raw):
            return
        if method in EMPTY_LISTS:
            self.reply(msg_id, EMPTY_LISTS[method])
            return
        self.reply_error(msg_id, -32601, f"method not available while the drive is "
                                         f"not attached: {method}")

    def _warm_up(self) -> None:
        if self.probe()["state"] == health.OK:
            self.ensure_child()
        elif not self.cache.get("tools"):
            self.discover_tools()

    def handle(self, msg: Any, raw: bytes) -> None:
        if isinstance(msg, list):  # JSON-RPC batch (older protocol revisions)
            for item in msg:
                self.handle(item, _dump(item))
            return
        if not isinstance(msg, dict):
            return
        if "method" in msg and "id" in msg:
            self.handle_request(msg, raw)
        elif "method" in msg:  # notification
            if msg["method"] == "notifications/initialized":
                return  # the child gets its own during our handshake
            if msg["method"] == "notifications/cancelled":
                with self.state_lock:
                    self.pending.pop((msg.get("params") or {}).get("requestId"), None)
            self.forward(msg, raw)
        else:  # response to a request the child sent the client (e.g. roots/list)
            self.forward(msg, raw)

    def run(self) -> None:
        log(f"v{__version__} starting; volume={VOLUME}; "
            f"cached tools={len(self.cache.get('tools') or [])}")
        stdin = sys.stdin.buffer
        for raw in iter(stdin.readline, b""):
            if not raw.strip():
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                self.write(_dump({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "parse error"}}))
                continue
            try:
                self.handle(msg, raw if raw.endswith(b"\n") else raw + b"\n")
            except Exception as exc:  # noqa: BLE001 - never die on one message
                log(f"error handling message: {type(exc).__name__}: {exc}")
                if isinstance(msg, dict) and "id" in msg and "method" in msg:
                    self.reply_error(msg["id"], -32603, f"gateway error: {exc}")
        log("client closed stdin; shutting down")
        if self.child is not None:
            self.child.kill()
        # Daemon reader threads may still hold stdout/stderr; skip interpreter
        # finalisation so shutdown never trips over them.
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os._exit(0)


def main(argv: Optional[List[str]] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    if not argv:
        argv = ["npx", "-y", "@modelcontextprotocol/server-filesystem", str(VOLUME)]
    Gateway(argv).run()


if __name__ == "__main__":
    main()
