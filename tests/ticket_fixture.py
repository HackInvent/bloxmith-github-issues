"""Synthetic HTTP boundary for approval/outcome tests; no real account or repository."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.parse import parse_qs, urlsplit


TOKEN = "synthetic-github-ticket-credential"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PATCH(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()

    def handle_request(self):
        address = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        entry = {"method": self.command, "path": address.path, "query": parse_qs(address.query),
                 "payload": json.loads(body) if body else None, "authorization": self.headers.get("Authorization", "")}
        self.server.calls.append(entry)
        mutation = self.command != "GET"
        mode = self.server.mode if mutation or self.server.fail_reads else "ok"
        if mode == "drop":
            self.close_connection = True
            return
        if mode == "redirect":
            self.send_response(307)
            self.send_header("Location", self.server.base_url + "/redirect-target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode in {"400", "429", "503"}:
            return self.send(int(mode), {"message": "test failure " + TOKEN})
        if mode == "invalid":
            return self.send(200, b"not json", raw=True)
        if mode == "oversize":
            return self.send(200, b"x" * (2 * 1024 * 1024 + 1), raw=True)
        if self.command == "GET":
            if address.path.endswith("/issues"):
                return self.send(200, [dict(self.server.issue)], {"Link": '<' + self.server.base_url + '/repos/fixture/project/issues?page=3>; rel="next"'})
            if address.path.endswith("/issues/7"):
                return self.send(200, dict(self.server.issue))
            if address.path.endswith("/comments"):
                return self.send(200, [])
            return self.send(404, {"message": "not found"})
        self.server.writes.append(entry)
        if self.command == "PATCH":
            self.server.issue.update(entry["payload"])
            return self.send(200, dict(self.server.issue))
        return self.send(201, {"id": 123 + len(self.server.writes), "body": (entry["payload"] or {}).get("body", "")})

    def send(self, status, value, headers=None, raw=False):
        data = value if raw else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-GitHub-Request-Id", "fixture-request-123")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextmanager
def github():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.base_url = "http://127.0.0.1:" + str(server.server_port)
    server.calls, server.writes = [], []
    server.mode, server.fail_reads = "ok", False
    server.issue = {"id": 101, "number": 7, "title": "Synthetic ticket", "body": "Fixture only",
                    "state": "open", "updated_at": "2026-01-01T00:00:00Z", "comments": 0}
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
