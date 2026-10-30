"""Read-only native project catalog over the official App Server JSONL port."""

import json
import os
import selectors
import subprocess
import time

from ..projects.local_engine import SharedCodexEngine, resolve_binary


class CatalogUnavailable(Exception):
    pass


class CodexProjects:
    def __init__(self, settings, timeout=10):
        self.settings, self.timeout = settings, timeout

    def list_projects(self):
        engine = SharedCodexEngine(self.settings)
        if engine.readiness() != "ready":
            raise CatalogUnavailable("Codex 登录或执行入口未就绪")
        with subprocess.Popen(
            [resolve_binary(self.settings.codex_binary), "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=self.settings.workspace, env=engine.environment(), bufsize=0,
        ) as process:
            try:
                return self._read_catalog(process)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

    def _read_catalog(self, process):
        deadline, buffer, size = time.monotonic() + self.timeout, b"", 0

        def send(value):
            process.stdin.write((json.dumps(value) + "\n").encode())
            process.stdin.flush()

        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)

            def receive(request_id):
                nonlocal buffer, size
                while time.monotonic() < deadline:
                    if b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        message = json.loads(line)
                        if not isinstance(message, dict):
                            raise CatalogUnavailable("项目接口返回格式无效")
                        if "method" in message:
                            if "id" in message:
                                # Never grant permissions or execute server-side tool requests.
                                send({"id": message["id"], "error": {
                                    "code": -32601, "message": "Read-only project catalog client"
                                }})
                            continue
                        if message.get("id") == request_id:
                            if "error" in message or not isinstance(message.get("result"), dict):
                                raise CatalogUnavailable("当前 Codex 不支持项目列表接口")
                            return message["result"]
                    elif selector.select(max(0, deadline - time.monotonic())):
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            raise CatalogUnavailable("项目连接已断开")
                        size += len(chunk)
                        if size > 4 * 1024 * 1024:
                            raise CatalogUnavailable("项目接口返回过大")
                        buffer += chunk
                raise CatalogUnavailable("项目刷新超时")

            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "wechat_assistant_catalog", "version": "0.5.0"},
                "capabilities": {"experimentalApi": True},
            }})
            receive(1)
            send({"method": "initialized"})
            pages, cursor, seen = [], None, set()
            for request_id in range(2, 102):
                send({"id": request_id, "method": "project/list", "params": {
                    "limit": 100, "cursor": cursor,
                }})
                page = receive(request_id)
                if not isinstance(page.get("data"), list):
                    raise CatalogUnavailable("项目接口返回格式无效")
                pages.append(page)
                cursor = page.get("nextCursor")
                if cursor is None:
                    return pages
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise CatalogUnavailable("项目分页游标无效")
                seen.add(cursor)
            raise CatalogUnavailable("项目列表分页过多")
