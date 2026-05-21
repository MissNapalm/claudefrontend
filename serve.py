#!/usr/bin/env python3
"""
serve.py — minimal HTTP server on :8765 that backs scan.html.

It does NOT know anything about Claude Code. It just serves the HUD and
exposes endpoints that read from flat files capture.py writes:

  ~/thm/ops.jsonl    — append-only event stream from capture.py
  ~/thm/creds.json   — captured credentials (this server owns the file)
  ~/thm/*.log        — any other tool logs (nmap output etc.)

Endpoints:
  GET  /                          → scan.html
  GET  /api/logs                  → list candidate tool-output logs
  GET  /api/logtail?path=&offset= → tail any log file by byte offset
  GET  /api/sessions              → tells the HUD which ops.jsonl to follow
  GET  /api/events?path=&offset=  → newline events from ops.jsonl
  GET  /api/creds                 → list creds
  POST /api/creds                 → append cred
  DELETE /api/creds?idx=N         → remove cred
  POST /api/spawn-terminal {cmd}  → open Terminal.app with a command

Stdlib only.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT       = int(os.environ.get("THM_OPS_PORT", "8766"))
HOME       = Path.home()
THM_DIR    = HOME / "thm"
OPS_FILE   = THM_DIR / "ops.jsonl"
CREDS_FILE = THM_DIR / "creds.json"
HUD_FILE   = Path(__file__).resolve().parent / "scan.html"

# Only allow tailing files under these roots — keeps the API from becoming
# an arbitrary-file-read.
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

def list_logs():
    out = []
    if not THM_DIR.exists():
        return out
    for f in THM_DIR.rglob("*"):
        if not f.is_file():
            continue
        name = f.name
        # ops.jsonl + anything matching common tool-output patterns
        if name == "ops.jsonl" or name.endswith(".log") or name.endswith(".txt") or name.endswith(".out"):
            try:
                st = f.stat()
            except OSError:
                continue
            out.append({
                "name": str(f.relative_to(THM_DIR)),
                "path": str(f),
                "mtime": st.st_mtime,
                "size": st.st_size,
            })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out

def tail_text(path, offset, max_bytes=64 * 1024):
    """Return up to max_bytes of new lines from path starting at byte offset."""
    p = Path(path)
    if not p.exists():
        return {"lines": [], "offset": offset}
    size = p.stat().st_size
    if offset > size:  # file rotated / truncated
        offset = 0
    if offset == size:
        return {"lines": [], "offset": size}
    with open(p, "rb") as f:
        f.seek(offset)
        data = f.read(max_bytes)
    new_offset = offset + len(data)
    # if we cut mid-line, back up to the last newline so we don't split a line
    if new_offset < size and not data.endswith(b"\n"):
        nl = data.rfind(b"\n")
        if nl >= 0:
            data = data[: nl + 1]
            new_offset = offset + len(data)
    text = data.decode("utf-8", errors="replace")
    lines = [ln for ln in text.split("\n") if ln]
    return {"lines": lines, "offset": new_offset}

def tail_events(path, offset, max_bytes=128 * 1024):
    """Same as tail_text but parses each line as JSON, filtering bad lines."""
    out = tail_text(path, offset, max_bytes=max_bytes)
    events = []
    for ln in out["lines"]:
        try:
            events.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return {"events": events, "offset": out["offset"]}

def load_creds():
    if not CREDS_FILE.exists():
        return []
    try:
        data = json.loads(CREDS_FILE.read_text() or "[]")
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []

def save_creds(creds):
    THM_DIR.mkdir(parents=True, exist_ok=True)
    CREDS_FILE.write_text(json.dumps(creds, indent=2))

def spawn_terminal(cmd):
    """Open Terminal.app on macOS with `cmd` already typed and executing."""
    # escape any " and \ for AppleScript
    safe = cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Terminal"\n  activate\n  do script "{safe}"\nend tell'
    subprocess.Popen(["osascript", "-e", script])

def focus_terminal():
    """Bring Terminal.app to the foreground without opening a new window."""
    subprocess.Popen(["osascript", "-e", 'tell application "Terminal" to activate'])

# ---------- HTTP plumbing ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "THM-OPS/1.0"

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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        p = url.path

        if p in ("/", "/scan.html", "/index.html"):
            if not HUD_FILE.exists():
                return self._send(404, "scan.html missing", "text/plain")
            return self._send(200, HUD_FILE.read_bytes(), "text/html")

        if p == "/api/logs":
            return self._send(200, {"logs": list_logs()})

        if p == "/api/logtail":
            path = (qs.get("path") or [""])[0]
            offset = int((qs.get("offset") or ["0"])[0])
            if not path_is_allowed(path):
                return self._send(403, {"error": "path not allowed"})
            return self._send(200, tail_text(path, offset))

        if p == "/api/sessions":
            # The HUD treats whatever was modified <60s ago as the active session.
            # ops.jsonl is the only thing we hand it.
            if OPS_FILE.exists():
                st = OPS_FILE.stat()
                return self._send(200, {"sessions": [{
                    "path": str(OPS_FILE),
                    "mtime": st.st_mtime,
                }]})
            return self._send(200, {"sessions": []})

        if p == "/api/events":
            path = (qs.get("path") or [str(OPS_FILE)])[0]
            offset = int((qs.get("offset") or ["0"])[0])
            if not path_is_allowed(path):
                return self._send(403, {"error": "path not allowed"})
            return self._send(200, tail_events(path, offset))

        if p == "/api/creds":
            return self._send(200, {"creds": load_creds()})

        return self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        p = url.path

        if p == "/api/creds":
            body = self._read_json()
            if not isinstance(body, dict):
                return self._send(400, {"error": "invalid body"})
            # Sanitize fields: a real cred has no whitespace, no JSON escape
            # leakage, no embedded newlines. Truncate at the first such char.
            def clean(v):
                v = str(v or "")
                # cut at first whitespace, literal "\n", quote, or NUL
                for sep in ("\n", "\r", "\t", " ", "\\n", '"'):
                    i = v.find(sep)
                    if i >= 0:
                        v = v[:i]
                return v.strip()
            cred = {
                "service":  str(body.get("service", "") or ""),
                "host":     str(body.get("host", "") or ""),
                "username": clean(body.get("username", "")),
                "password": clean(body.get("password", "")),
                "source":   str(body.get("source", "manual") or "manual"),
                "notes":    str(body.get("notes", "") or ""),
                "ts":       time.time(),
            }
            # Reject obviously broken hits
            if not cred["username"] or not cred["password"]:
                return self._send(400, {"error": "username/password required"})
            if len(cred["password"]) > 80 or len(cred["username"]) > 80:
                return self._send(400, {"error": "field too long; likely a parser garble"})

            creds = load_creds()
            # Dedupe on (username, password) — same secret = same fact,
            # regardless of which tool tagged the service string.
            key = (cred["username"].lower(), cred["password"])
            for existing in creds:
                ek = (str(existing.get("username","")).strip().lower(),
                      str(existing.get("password","")).strip())
                if ek == key and key != ("",""):
                    # merge: keep earliest entry but enrich missing fields
                    for f in ("service","host","source","notes"):
                        if not existing.get(f) and cred.get(f):
                            existing[f] = cred[f]
                    save_creds(creds)
                    return self._send(200, {"ok": True, "count": len(creds), "deduped": True})
            creds.append(cred)
            save_creds(creds)
            return self._send(200, {"ok": True, "count": len(creds)})

        if p == "/api/spawn-terminal":
            body = self._read_json()
            cmd = str(body.get("cmd", "") or "").strip()
            if not cmd:
                return self._send(400, {"error": "no cmd"})
            try:
                spawn_terminal(cmd)
            except Exception as e:
                return self._send(500, {"error": str(e)})
            return self._send(200, {"ok": True})

        if p == "/api/creds-dedupe":
            creds = load_creds()
            # Clean garbled passwords/usernames in-place first.
            def clean(v):
                v = str(v or "")
                for sep in ("\n", "\r", "\t", " ", "\\n", '"'):
                    i = v.find(sep)
                    if i >= 0:
                        v = v[:i]
                return v.strip()
            for c in creds:
                c["username"] = clean(c.get("username",""))
                c["password"] = clean(c.get("password",""))
            # Drop entries with empty user or password after cleaning.
            creds = [c for c in creds if c.get("username") and c.get("password")]
            # Now dedupe on (username, password), keeping first occurrence + merging fields.
            new_list = []
            seen = {}
            for c in creds:
                k = (c["username"].lower(), c["password"])
                if k in seen:
                    earliest = seen[k]
                    for f in ("service","host","source","notes"):
                        if not earliest.get(f) and c.get(f):
                            earliest[f] = c[f]
                    continue
                seen[k] = c
                new_list.append(c)
            before = len(load_creds())
            save_creds(new_list)
            return self._send(200, {"ok": True, "before": before, "after": len(new_list), "removed": before - len(new_list)})

        if p == "/api/focus-terminal":
            try:
                focus_terminal()
            except Exception as e:
                return self._send(500, {"error": str(e)})
            return self._send(200, {"ok": True})

        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        p = url.path

        if p == "/api/creds":
            try:
                idx = int((qs.get("idx") or ["-1"])[0])
            except ValueError:
                return self._send(400, {"error": "bad idx"})
            creds = load_creds()
            if 0 <= idx < len(creds):
                creds.pop(idx)
                save_creds(creds)
                return self._send(200, {"ok": True, "count": len(creds)})
            return self._send(404, {"error": "out of range"})

        return self._send(404, {"error": "not found"})

def main():
    THM_DIR.mkdir(parents=True, exist_ok=True)
    OPS_FILE.touch(exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[serve] THM/OPS console on http://127.0.0.1:{PORT}/", file=sys.stderr)
    print(f"[serve] tailing {OPS_FILE}", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] stopped", file=sys.stderr)

if __name__ == "__main__":
    main()
