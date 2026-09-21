"""Serve the visual frontend and bridge it to the existing structured workflow."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ui.app import run


ROOT = Path(__file__).resolve().parent / "frontend"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = "/index.html" if self.path == "/" else self.path
        files = {"/index.html": (ROOT / "index.html", "text/html; charset=utf-8"),
                 "/static/app.css": (ROOT / "app.css", "text/css; charset=utf-8"),
                 "/static/app.js": (ROOT / "app.js", "text/javascript; charset=utf-8")}
        item = files.get(path)
        if not item or not item[0].exists():
            self.send_error(404)
            return
        data = item[0].read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", item[1])
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path != "/api/run":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            result = run(json.loads(self.rfile.read(length)))
        except (ValueError, TypeError, KeyError) as exc:
            self.send_error(400, str(exc))
            return
        data = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("UI: http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
