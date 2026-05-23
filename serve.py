#!/usr/bin/env python3
"""
serve.py — HTTP server for the THM/OPS HUD.

Endpoints:
  GET  /                            → scan.html
  GET  /api/sessions                → reports ops.jsonl as the only session
  GET  /api/events?path=&offset=    → newline events from ops.jsonl
  GET  /api/monitors                → recent Claude background-task .output files
  GET  /api/monitor-tail?path=...   → tail one monitor .output file
  GET  /api/loot-files              → recent loot.jsonl files anywhere claude is running
  GET  /api/loot?path=&offset=      → tail one loot.jsonl, return parsed records
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

MONITOR_ROOT = Path("/private/tmp")

# Where to search for loot.jsonl files. We scan a few common roots where
# Claude Code typically runs from, and surface any loot.jsonl whose mtime
# is recent.
LOOT_SEARCH_ROOTS = [
    HOME / "Desktop",
    HOME / "thm",
    HOME / "Documents",
    HOME / "code",
    HOME / "Projects",
    Path("/private/tmp"),
    Path("/tmp"),
]
LOOT_STALE_SEC   = 24 * 3600   # surface loot files touched in last 24h
LOOT_MAX_DEPTH   = 4           # don't descend too deep when searching

ALLOWED_ROOTS = [
    THM_DIR.resolve(),
    MONITOR_ROOT.resolve(),
    HOME.resolve(),
    Path("/tmp").resolve(),
]
MONITOR_STALE_SEC = 600


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


def list_monitors():
    out = []
    if not MONITOR_ROOT.exists():
        return out
    now = time.time()
    for entry in MONITOR_ROOT.glob("claude-*"):
        if not entry.is_dir():
            continue
        for f in entry.rglob("*.output"):
            try:
                st = f.stat()
            except OSError:
                continue
            if now - st.st_mtime > MONITOR_STALE_SEC:
                continue
            out.append({
                "path":  str(f),
                "name":  f.name,
                "size":  st.st_size,
                "mtime": st.st_mtime,
            })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _walk_for_loot(root, depth, found, now):
    """Bounded-depth walk looking for files named loot.jsonl."""
    if depth > LOOT_MAX_DEPTH:
        return
    try:
        for entry in root.iterdir():
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file() and entry.name == "loot.jsonl":
                    st = entry.stat()
                    if now - st.st_mtime <= LOOT_STALE_SEC:
                        found.append({
                            "path":  str(entry.resolve()),
                            "size":  st.st_size,
                            "mtime": st.st_mtime,
                        })
                elif entry.is_dir():
                    # Skip noisy directories
                    if entry.name in (".git", "node_modules", "__pycache__",
                                       "venv", ".venv", "Library", ".cache",
                                       ".npm", ".cargo"):
                        continue
                    _walk_for_loot(entry, depth + 1, found, now)
            except (OSError, PermissionError):
                continue
    except (OSError, PermissionError):
        return


def list_loot_files():
    out = []
    seen = set()
    now = time.time()
    for root in LOOT_SEARCH_ROOTS:
        if not root.exists():
            continue
        _walk_for_loot(root, 0, out, now)
    # dedupe by resolved path
    uniq = []
    for f in out:
        if f["path"] in seen:
            continue
        seen.add(f["path"])
        uniq.append(f)
    uniq.sort(key=lambda x: x["mtime"], reverse=True)
    return uniq


class Handler(BaseHTTPRequestHandler):
    server_version = "THM-OPS/3.0"

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

        if p == "/api/monitors":
            return self._send(200, {"monitors": list_monitors()})

        if p == "/api/monitor-tail":
            path = (qs.get("path") or [""])[0]
            try:
                offset = int((qs.get("offset") or ["0"])[0])
            except ValueError:
                offset = 0
            if not path or not path_is_allowed(path):
                return self._send(403, {"error": "path not allowed"})
            if not path.endswith(".output"):
                return self._send(400, {"error": "not a monitor output file"})
            return self._send(200, tail_text(path, offset))

        if p == "/api/loot-files":
            return self._send(200, {"files": list_loot_files()})

        if p == "/api/loot":
            path = (qs.get("path") or [""])[0]
            try:
                offset = int((qs.get("offset") or ["0"])[0])
            except ValueError:
                offset = 0
            if not path or not path_is_allowed(path):
                return self._send(403, {"error": "path not allowed"})
            if not path.endswith("loot.jsonl"):
                return self._send(400, {"error": "not a loot file"})
            return self._send(200, tail_events(path, offset))

        return self._send(404, {"error": "not found"})


def main():
    THM_DIR.mkdir(parents=True, exist_ok=True)
    OPS_FILE.touch(exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[serve] THM/OPS HUD on http://127.0.0.1:{PORT}/", file=sys.stderr)
    print(f"[serve] tailing {OPS_FILE}", file=sys.stderr)
    print(f"[serve] auto-discovering loot.jsonl under: {', '.join(str(r) for r in LOOT_SEARCH_ROOTS if r.exists())}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
