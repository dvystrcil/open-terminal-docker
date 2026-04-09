"""
bible_bridge.py — Story Bible HTTP Bridge
==========================================
Runs in the open-terminal pod. Exposes the local git-cloned story bible
over HTTP so the Fiction Writing Filter (in the pipelines pod) can read
and write bible files across namespace boundaries.

Endpoints:
  GET  /health                        — liveness check
  GET  /bible?task_type=CONTINUITY    — read bible files (returns JSON)
  POST /bible/write                   — write/append a file + git commit
  POST /bible/pull                    — trigger a git pull

Start:
  python3 bible_bridge.py

Environment variables (all optional, have defaults):
  BIBLE_PATH      — path to git-cloned story bible (default: /data/story-bible)
  BRIDGE_PORT     — port to listen on (default: 80)
  BRIDGE_TOKEN    — shared secret token for auth (default: no auth)
  GIT_REMOTE      — git remote name (default: origin)
  GIT_BRANCH      — git branch (default: main)
"""

import json
import logging
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bible_bridge")

BIBLE_PATH  = os.environ.get("BIBLE_PATH", "/data/story-bible")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "80"))
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
GIT_REMOTE  = os.environ.get("GIT_REMOTE", "origin")
GIT_BRANCH  = os.environ.get("GIT_BRANCH", "main")

FRAGMENT_TASKS = {"FRAGMENT", "BIBLE_UPDATE"}


# ── Git helpers ───────────────────────────────────────────────────────────────

def git(*args) -> tuple[bool, str]:
    cmd = ["git", "-C", BIBLE_PATH] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.warning(f"git {' '.join(args)} failed: {result.stderr.strip()}")
            return False, result.stderr.strip()
        return True, result.stdout.strip()
    except Exception as e:
        log.warning(f"git {' '.join(args)} exception: {e}")
        return False, str(e)


def git_pull() -> tuple[bool, str]:
    log.info("git pull — syncing from remote")
    ok, out = git("pull", "--ff-only", GIT_REMOTE, GIT_BRANCH)
    if ok:
        log.info(f"git pull: {out or 'already up to date'}")
    return ok, out


# ── Bible read ────────────────────────────────────────────────────────────────

def read_bible(task_type: str = None) -> dict:
    """
    Read bible files and return as a dict of {relpath: content}.
    Core bible/ files always included.
    fragments/ included only for FRAGMENT and BIBLE_UPDATE tasks.
    """
    root = BIBLE_PATH
    if not os.path.exists(root):
        log.warning(f"Bible path not found: {root}")
        return {"error": f"Bible path not found: {root}", "files": {}}

    files = {}
    bible_dir = os.path.join(root, "bible")

    if os.path.isdir(bible_dir):
        for f in sorted(os.listdir(bible_dir)):
            if f.endswith(".md") and not f.startswith("."):
                relpath = os.path.join("bible", f)
                _load_file(root, relpath, files)
    else:
        log.warning("bible/ subdir not found — falling back to top-level .md files")
        for f in sorted(os.listdir(root)):
            filepath = os.path.join(root, f)
            if f.endswith(".md") and os.path.isfile(filepath) and not f.startswith("."):
                _load_file(root, f, files)

    if task_type in FRAGMENT_TASKS:
        fragments_dir = os.path.join(root, "fragments")
        if os.path.isdir(fragments_dir):
            for f in sorted(os.listdir(fragments_dir)):
                if f.endswith(".md") and not f.startswith("."):
                    relpath = os.path.join("fragments", f)
                    _load_file(root, relpath, files)

    total_chars = sum(len(v) for v in files.values())
    log.info(
        f"Bible read: {len(files)} files ({total_chars} chars) "
        f"[fragments={'yes' if task_type in FRAGMENT_TASKS else 'no'}]"
    )
    return {"files": files, "task_type": task_type}


def _load_file(root: str, relpath: str, files: dict):
    filepath = os.path.join(root, relpath)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            files[relpath] = content
    except Exception as e:
        log.warning(f"Could not read {relpath}: {e}")


# ── Bible write ───────────────────────────────────────────────────────────────

def write_bible(filename: str, content: str, append: bool, commit_message: str = None) -> tuple[bool, str]:
    root = BIBLE_PATH
    if not os.path.exists(root):
        return False, f"Bible path not found: {root}"

    filepath = os.path.join(root, filename)
    parent = os.path.dirname(filepath)
    if parent and not os.path.exists(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            return False, f"Could not create directory {parent}: {e}"

    try:
        if append and os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                existing = f.read()
            separator = "\n\n---\n\n" if existing.strip() else ""
            content = existing + separator + content.strip() + "\n"

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        log.info(f"Wrote {filename} ({len(content)} chars) append={append}")
    except Exception as e:
        return False, f"File write failed: {e}"

    msg = commit_message or f"FWF: update {filename}"
    ok, _ = git("add", filename)
    if not ok:
        return False, f"git add failed for {filename}"

    ok, out = git("commit", "-m", msg)
    if not ok:
        if "nothing to commit" in out.lower():
            return True, f"{filename} unchanged — nothing to commit"
        return False, f"git commit failed: {out}"

    ok, out = git("push", GIT_REMOTE, GIT_BRANCH)
    if not ok:
        return False, f"git push failed: {out}"

    log.info(f"git commit+push: {msg}")
    return True, f"Written, committed, and pushed — '{msg}'"


# ── HTTP handler ──────────────────────────────────────────────────────────────

class BridgeHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        log.info(f"{self.address_string()} {format % args}")

    def _auth_ok(self) -> bool:
        if not BRIDGE_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {BRIDGE_TOKEN}"

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        if not self._auth_ok():
            self._send_json(401, {"error": "Unauthorized"})
            return

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "bible_path": BIBLE_PATH,
                "bible_exists": os.path.exists(BIBLE_PATH),
            })

        elif parsed.path == "/bible":
            task_type = params.get("task_type", [None])[0]
            result = read_bible(task_type)
            self._send_json(200, result)

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._auth_ok():
            self._send_json(401, {"error": "Unauthorized"})
            return

        parsed = urlparse(self.path)

        if parsed.path == "/bible/write":
            body = self._read_body()
            filename = body.get("filename")
            content  = body.get("content")
            append   = body.get("append", True)
            commit_message = body.get("commit_message")

            if not filename or content is None:
                self._send_json(400, {"error": "filename and content are required"})
                return

            ok, status = write_bible(filename, content, append, commit_message)
            self._send_json(200 if ok else 500, {"ok": ok, "status": status})

        elif parsed.path == "/bible/pull":
            ok, out = git_pull()
            self._send_json(200 if ok else 500, {"ok": ok, "output": out})

        else:
            self._send_json(404, {"error": "Not found"})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not os.path.exists(BIBLE_PATH):
        log.warning(f"Bible path does not exist yet: {BIBLE_PATH}")
        log.warning("Clone your story-bible repo before the filter makes requests.")

    log.info(f"Bible bridge starting on port {BRIDGE_PORT}")
    log.info(f"Bible path: {BIBLE_PATH}")
    log.info(f"Auth: {'enabled' if BRIDGE_TOKEN else 'disabled (set BRIDGE_TOKEN to enable)'}")

    server = HTTPServer(("0.0.0.0", BRIDGE_PORT), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Bridge stopped")
