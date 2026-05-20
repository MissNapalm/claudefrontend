#!/usr/bin/env python3
"""
capture.py — tail the active Claude Code session JSONL and write a flat
normalized event stream to ~/thm/ops.jsonl.

Watches ~/.claude/projects/<slug>/<session>.jsonl. The currently active
session is whichever JSONL was modified most recently. When you start a
new conversation, that file changes — capture.py follows the new one.

Output (~/thm/ops.jsonl, append-only, one JSON event per line):
  { "ts": 1716230400.123,
    "session": "<sid>",
    "src":  "YOU" | "CLAUDE" | "TOOL" | "RESULT",
    "type": "user_text" | "assistant_text" | "tool_use" | "tool_result",
    "text": "...",
    "tool": "Bash" | "Read" | ... (only for tool_use / tool_result),
    "input_preview": "<short summary>" }

Also writes ~/thm/state.json with {active_session, active_path, last_event_ts}.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

HOME       = Path.home()
PROJECTS   = HOME / ".claude" / "projects"
OUT_DIR    = HOME / "thm"
OUT_FILE   = OUT_DIR / "ops.jsonl"
STATE_FILE = OUT_DIR / "state.json"

POLL_NEW_SESSION_SEC = 2.0      # how often we look for a newer session
POLL_TAIL_SEC        = 0.15     # how often we read appended lines
STALE_AFTER_SEC      = 120      # don't follow sessions that haven't moved in 2min

META_TAGS = re.compile(
    r"<(system-reminder|task-notification|command-name|command-message|"
    r"command-args|local-command-stdout|ide_selection)>[\s\S]*?</\1>",
    re.IGNORECASE,
)

def strip_meta(s):
    if not isinstance(s, str):
        return ""
    return META_TAGS.sub("", s).strip()

def newest_session():
    """Most-recently-modified JSONL across all projects; None if all stale."""
    best = None
    best_mtime = 0
    if not PROJECTS.exists():
        return None
    for proj in PROJECTS.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m > best_mtime:
                best_mtime = m
                best = f
    if best is None:
        return None
    if time.time() - best_mtime > STALE_AFTER_SEC:
        return None
    return best

def write_state(path):
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({
            "active_session": path.stem if path else None,
            "active_path":    str(path) if path else None,
            "last_event_ts":  time.time(),
        }))
    except OSError:
        pass

def emit(out_fp, ev):
    out_fp.write(json.dumps(ev, ensure_ascii=False) + "\n")
    out_fp.flush()

def short(s, n=200):
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n] + "…"

def input_preview(name, inp):
    """One-line summary of a tool_use's input block."""
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return short(inp.get("command", ""), 160)
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return short(inp.get("file_path", ""), 160)
    if name == "Grep":
        return short(f'{inp.get("pattern","")} in {inp.get("path","")}', 160)
    if name == "WebFetch":
        return short(inp.get("url", ""), 160)
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        return f"{len(todos)} todo(s)"
    if name == "Task" or name == "Agent":
        return short(inp.get("description") or inp.get("prompt", ""), 160)
    # generic fallback — first string-ish field
    for k, v in inp.items():
        if isinstance(v, str) and v:
            return short(f"{k}={v}", 160)
    return ""

def normalize(raw, session_id):
    """
    Convert one Claude Code session JSONL record into 0+ flat events.
    Yields dicts ready to append to ops.jsonl.
    """
    if not isinstance(raw, dict):
        return
    rtype = raw.get("type")
    ts_raw = raw.get("timestamp")
    # session JSONL timestamps are ISO; convert to epoch for the frontend
    ts = time.time()
    if isinstance(ts_raw, str):
        try:
            # python's fromisoformat handles "...Z" only in 3.11+, so be liberal
            iso = ts_raw.replace("Z", "+00:00")
            from datetime import datetime
            ts = datetime.fromisoformat(iso).timestamp()
        except Exception:
            pass

    msg = raw.get("message")

    # plain user_text / assistant_text live inside message.content blocks
    if rtype in ("user", "assistant") and isinstance(msg, dict):
        role = msg.get("role") or rtype
        content = msg.get("content")
        # content may be a bare string (rare) or a list of blocks
        if isinstance(content, str):
            text = strip_meta(content)
            if text:
                yield {
                    "ts": ts, "session": session_id,
                    "src": "YOU" if role == "user" else "CLAUDE",
                    "type": "user_text" if role == "user" else "assistant_text",
                    "text": text,
                }
            return
        if not isinstance(content, list):
            return
        for blk in content:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                text = strip_meta(blk.get("text") or "")
                if not text:
                    continue
                yield {
                    "ts": ts, "session": session_id,
                    "src": "YOU" if role == "user" else "CLAUDE",
                    "type": "user_text" if role == "user" else "assistant_text",
                    "text": text,
                }
            elif btype == "tool_use":
                name = blk.get("name") or "tool"
                yield {
                    "ts": ts, "session": session_id,
                    "src": "TOOL", "type": "tool_use",
                    "tool": name,
                    "input_preview": input_preview(name, blk.get("input")),
                    "text": "",
                }
            elif btype == "tool_result":
                tc = blk.get("content")
                # tool_result content can be a string or a list of {type:text, text:...}
                if isinstance(tc, list):
                    parts = []
                    for sub in tc:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(sub.get("text") or "")
                        elif isinstance(sub, str):
                            parts.append(sub)
                    tc = "\n".join(parts)
                text = strip_meta(tc or "")
                if not text:
                    continue
                yield {
                    "ts": ts, "session": session_id,
                    "src": "RESULT", "type": "tool_result",
                    "text": short(text, 1200),
                }
    # everything else (queue-operation, ai-title, attachment, file-history-snapshot…)
    # is internal bookkeeping — skip it.

def tail_session(path, stop_when_changed):
    """
    Tail one JSONL file from its current end-of-file. Returns when the
    file we should be watching has changed (a newer session has appeared).
    """
    session_id = path.stem
    print(f"[capture] tailing {path}", file=sys.stderr)
    write_state(path)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_fp = open(OUT_FILE, "a", buffering=1, encoding="utf-8")

    # Start from the current end — we only care about events that happen
    # from now on. (If you want backfill, change this to 0.)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        last_check = 0
        buf = ""
        while True:
            chunk = f.read()
            if chunk:
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for ev in normalize(raw, session_id):
                        emit(out_fp, ev)
                        write_state(path)
            else:
                time.sleep(POLL_TAIL_SEC)

            now = time.time()
            if now - last_check >= POLL_NEW_SESSION_SEC:
                last_check = now
                if stop_when_changed(path):
                    out_fp.close()
                    return

def main():
    print(f"[capture] watching {PROJECTS}", file=sys.stderr)
    print(f"[capture] writing  {OUT_FILE}", file=sys.stderr)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # touch the output file so the server has something to tail immediately
    OUT_FILE.touch(exist_ok=True)

    current = None
    while True:
        latest = newest_session()
        if latest is None:
            write_state(None)
            time.sleep(POLL_NEW_SESSION_SEC)
            continue
        if latest != current:
            current = latest
            def changed(now_watching, _latest_ref=[latest]):
                fresh = newest_session()
                if fresh is None:
                    return False
                if fresh != now_watching:
                    # only switch if the new one is actually fresher
                    try:
                        return fresh.stat().st_mtime > now_watching.stat().st_mtime
                    except OSError:
                        return True
                return False
            try:
                tail_session(current, changed)
            except FileNotFoundError:
                current = None
                time.sleep(POLL_NEW_SESSION_SEC)
        else:
            time.sleep(POLL_NEW_SESSION_SEC)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[capture] stopped", file=sys.stderr)
