"""Tiny MCP stdio client used by the end-to-end tests (no SDK needed)."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple


class Client:
    def __init__(self, cmd: List[str], env: Optional[dict] = None,
                 stderr_path: Optional[str] = None) -> None:
        self._stderr = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr, env=env)
        self._cv = threading.Condition()
        self._responses: Dict[Any, dict] = {}
        self.notifications: List[dict] = []
        self._next = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            with self._cv:
                if "id" in msg and "method" not in msg:
                    self._responses[msg["id"]] = msg
                else:
                    self.notifications.append(msg)
                self._cv.notify_all()

    def send(self, method: str, params: Optional[dict] = None, notify: bool = False) -> Any:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        msg_id = None
        if not notify:
            self._next += 1
            msg_id = msg["id"] = self._next
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()
        return msg_id

    def wait(self, msg_id: Any, timeout: float = 30) -> dict:
        deadline = time.time() + timeout
        with self._cv:
            while msg_id not in self._responses:
                left = deadline - time.time()
                if left <= 0:
                    raise TimeoutError(f"no response to id={msg_id} within {timeout}s")
                self._cv.wait(left)
            return self._responses.pop(msg_id)

    def request(self, method: str, params: Optional[dict] = None,
                timeout: float = 30) -> Tuple[dict, float]:
        t0 = time.time()
        msg = self.wait(self.send(method, params), timeout)
        return msg, time.time() - t0

    def initialize(self) -> dict:
        msg, _ = self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "kb-test", "version": "1"}})
        self.send("notifications/initialized", notify=True)
        return msg

    def tool_send(self, name: str, args: Optional[dict] = None) -> Any:
        return self.send("tools/call", {"name": name, "arguments": args or {}})

    @staticmethod
    def parse(msg: dict) -> Tuple[Any, bool]:
        if "error" in msg:
            return {"jsonrpc_error": msg["error"]}, True
        result = msg["result"]
        text = "".join(c.get("text", "") for c in result.get("content", [])
                       if c.get("type") == "text")
        try:
            data: Any = json.loads(text)
        except ValueError:
            data = {"_text": text}
        return data, bool(result.get("isError"))

    def tool(self, name: str, args: Optional[dict] = None,
             timeout: float = 60) -> Tuple[Any, bool, float]:
        t0 = time.time()
        msg = self.wait(self.tool_send(name, args), timeout)
        data, is_error = self.parse(msg)
        return data, is_error, time.time() - t0

    def close(self) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()
