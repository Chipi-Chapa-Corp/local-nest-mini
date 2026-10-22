"""Task monitor UI served separately from the assistant API."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

PAGE = (Path(__file__).with_name("monitor.html")).read_bytes()


# Events worth showing; routine noise (rejected log uploads, heartbeats,
# per-frame traces) stays in the log file only.
WANTED = {
    "local_bootstrap", "upstream_blocked",
    "s3_local_route", "s3_control_reply", "s3_endpoint", "s3_segments",
    "s3_response", "s3_quiet_close", "local_wake_only", "local_follow_on_noise",
    "s3_query_error", "s3_send_error", "s3_reset", "s3_tls_error", "s3_connection_error",
    "local_assistant_error",
}


def _recent_requests(log_path: Path, limit: int = 80, tail: int = 1024 * 1024) -> list[dict]:
    """Newest first, read from the end of the (large, append-only) event log."""
    try:
        with log_path.open("rb") as source:
            source.seek(0, 2)
            source.seek(max(0, source.tell() - tail))
            lines = source.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("kind") in WANTED:
            out.append(item)
            if len(out) >= limit:
                break
    return out


class MonitorHandler(BaseHTTPRequestHandler):
    store = None
    log_path = Path("data/logs/proxy-events.jsonl")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if path == "/api/tasks":
            self._json(200, {"tasks": self.store.snapshot()})
            return
        if path == "/api/requests":
            self._json(200, {"requests": _recent_requests(self.log_path)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        prefix = "/api/tasks/"
        suffix = "/interrupt"
        if path.startswith(prefix) and path.endswith(suffix):
            task_id = unquote(path[len(prefix):-len(suffix)])
            ok = self.store.cancel(task_id)
            self._json(200 if ok else 404, {"interrupted": ok, "id": task_id})
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args) -> None:
        return


def start_monitor(store, *, host: str, port: int, log_path: Path) -> ThreadingHTTPServer:
    handler = type("BoundMonitorHandler", (MonitorHandler,), {"store": store, "log_path": log_path})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, name="task-monitor", daemon=True)
    thread.start()
    return server
