#!/usr/bin/env python3
"""
serve.py — minimal HTTP server for the telemetry-only HUD.

Endpoints:
  GET  /                          → scan.html
  GET  /api/sessions              → reports ops.jsonl as the only session
  GET  /api/events?path=&offset=  → newline events from ops.jsonl
"""

import json
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT     = int(os.environ.get("THM_OPS_PORT", "8766"))
HOME     = Path.home()
THM_DIR  = HOME / "thm"
OPS_FILE = THM_DIR / "ops.jsonl"
HUD_FILE = Path(__file__).resolve().parent / "scan.html"

ALLOWED_ROOTS = [THM_DIR.resolve()]


def path_is_allowed(p):
    try:
        rp = Path(p).resolve()
    except Exception:
        return False
    for root in ALLOWED_ROOTS:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def tail_text(path, offset, max_bytes=128 * 1024):
    p = Path(path)
    if not p.exists():
        return {"lines": [], "offset": offset}
    size = p.stat().st_size
    if offset > size:
        offset = 0
    if offset == size:
        return {"lines": [], "offset": size}
    with open(p, "rb") as f:
        f.seek(offset)
        data = f.read(max_bytes)
    new_offset = offset + len(data)
    if new_offset < size and not data.endswith(b"\n"):
        nl = data.rfind(b"\n")
        if nl >= 0:
            data = data[: nl + 1]
            new_offset = offset + len(data)
    text = data.decode("utf-8", errors="replace")
    lines = [ln for ln in text.split("\n") if ln]
    return {"lines": lines, "offset": new_offset}


def tail_events(path, offset, max_bytes=128 * 1024):
    out = tail_text(path, offset, max_bytes=max_bytes)
    events = []
    for ln in out["lines"]:
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return {"events": events, "offset": out["offset"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "THM-OPS/2.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[serve] " + (fmt % args) + "\n")

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype.endswith("json") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        p = url.path

        if p in ("/", "/scan.html", "/index.html"):
            if not HUD_FILE.exists():
                return self._send(404, "scan.html missing", "text/plain")
            return self._send(200, HUD_FILE.read_bytes(), "text/html")

        if p == "/api/sessions":
            if OPS_FILE.exists():
                st = OPS_FILE.stat()
                return self._send(200, {"sessions": [{
                    "path": str(OPS_FILE),
                    "mtime": st.st_mtime,
                }]})
            return self._send(200, {"sessions": []})

        if p == "/api/events":
            path = (qs.get("path") or [str(OPS_FILE)])[0]
            try:
                offset = int((qs.get("offset") or ["0"])[0])
            except ValueError:
                offset = 0
            if not path_is_allowed(path):
                return self._send(403, {"error": "path not allowed"})
            return self._send(200, tail_events(path, offset))

        return self._send(404, {"error": "not found"})


def main():
    THM_DIR.mkdir(parents=True, exist_ok=True)
    OPS_FILE.touch(exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[serve] telemetry HUD on http://127.0.0.1:{PORT}/", file=sys.stderr)
    print(f"[serve] tailing {OPS_FILE}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
