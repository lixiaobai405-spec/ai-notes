"""A stdlib-only synthetic API: header projection, upload digest and SSE."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import time
from urllib.parse import parse_qs, urlsplit


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_arguments):
        pass

    def projection(self):
        names = ("Authorization", "Origin", "X-CSRF-Token", "X-Forwarded-For",
                 "X-Forwarded-Proto", "X-Forwarded-Host", "Host", "Last-Event-ID")
        return {name.lower(): self.headers.get(name) for name in names}

    def json_response(self, payload, status=200):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/fixture/upload":
            self.json_response({"code": "fixture_path_invalid"}, 404)
            return
        try:
            remaining = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            remaining = -1
        if not 0 <= remaining <= 50 * 1024 * 1024:
            self.json_response({"code": "fixture_length_invalid"}, 400)
            return
        size = remaining
        digest = hashlib.sha256()
        self.connection.settimeout(15)
        while remaining:
            chunk = self.rfile.read(min(64 * 1024, remaining))
            if not chunk:
                self.close_connection = True
                return
            digest.update(chunk)
            remaining -= len(chunk)
        self.json_response({"bytes": size, "sha256": digest.hexdigest(), "headers": self.projection()})

    def do_GET(self):
        target = urlsplit(self.path)
        if target.path == "/api/fixture/inspect":
            self.json_response({"headers": self.projection()})
            return
        if target.path != "/api/fixture/events":
            self.json_response({"code": "fixture_path_invalid"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        def send(value):
            self.wfile.write(value.encode("utf-8"))
            self.wfile.flush()
        try:
            if parse_qs(target.query).get("reconnect") == ["1"]:
                send("id: 3\nevent: resumed\ndata: " + json.dumps({"last_event_id": self.headers.get("Last-Event-ID")}) + "\n\n")
                return
            started = time.monotonic()
            send("retry: 1000\n\nid: 1\nevent: message\ndata: fixture-first\n\n")
            for number in range(1, 33):
                deadline = started + number * 2
                time.sleep(max(0, deadline - time.monotonic()))
                send(f": heartbeat {number}\n\n")
            send("id: 2\nevent: message\ndata: fixture-after-64-seconds\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
