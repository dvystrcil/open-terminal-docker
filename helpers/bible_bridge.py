"""
bible_bridge.py — Story Bible HTTP Bridge
==========================================
Runs in the open-terminal pod. Exposes git-cloned story bible projects
over HTTP so the Fiction Writing Filter (in the pipelines pod) can read
and write bible files across namespace boundaries.

Supports multiple simultaneous projects. Each project is a separate git
repo cloned under BIBLE_ROOT:

  BIBLE_ROOT/
    project-alpha/      ← git repo for book 1
      bible/
      fragments/
    project-beta/       ← git repo for book 2
      bible/
      fragments/

The calling filter specifies which project to use via a `project` parameter
on every request.

Endpoints:
  GET  /health                                   — list available projects
  GET  /projects                                 — list all project names
  GET  /bible?project=alpha&task_type=CONTINUITY — read bible files
  POST /bible/write                              — write/append + git commit
  POST /bible/pull                               — trigger git pull on a project

Start:
  python3 bible_bridge.py

Environment variables:
  BIBLE_ROOT      — parent directory containing project repos
                    (default: /home/u3aa02715/fiction)
  BRIDGE_PORT     — port to listen on (default: 8765)
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

BIBLE_ROOT   = os.environ.get("BIBLE_ROOT", "/home/u3aa02715/fiction")
BRIDGE_PORT  = int(os.environ.get("BRIDGE_PORT", "8765"))
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
GIT_REMOTE   = os.environ.get("GIT_REMOTE", "origin")
GIT_BRANCH   = os.environ.get("GIT_BRANCH", "main")

FRAGMENT_TASKS = {"FRAGMENT", "BIBLE_UPDATE"}


# ── Project resolution ────────────────────────────────────────────────────────

def resolve_project(project: str) -> tuple[str | None, str | None]:
    """
    Resolve a project name to its absolute path under BIBLE_ROOT.
    Returns (path, None) on success or (None, error_message) on failure.

    Security: prevents path traversal by ensuring the resolved path
    stays within BIBLE_ROOT.
    """
    if not project or not project.strip():
        return None, "project parameter is required"

    # Strip any path separators to prevent traversal
    name = os.path.basename(project.strip())
    if not name or name.startswith("."):
        return None, f"invalid project name: {project!r}"

    path = os.path.join(BIBLE_ROOT, name)

    # Confirm the resolved path is still inside BIBLE_ROOT
    if not os.path.realpath(path).startswith(os.path.realpath(BIBLE_ROOT)):
        return None, f"path traversal attempt blocked: {project!r}"

    if not os.path.isdir(path):
        return None, f"project not found: {name!r} (looked in {BIBLE_ROOT})"

    return path, None


def list_projects() -> list[str]:
    """Return all subdirectories of BIBLE_ROOT that look like git repos."""
    if not os.path.isdir(BIBLE_ROOT):
        return []
    projects = []
    for entry in sorted(os.listdir(BIBLE_ROOT)):
        full = os.path.join(BIBLE_ROOT, entry)
        if os.path.isdir(full) and not entry.startswith("."):
            # Only include dirs that are git repos
            if os.path.isdir(os.path.join(full, ".git")):
                projects.append(entry)
    return projects


# ── Git helpers ───────────────────────────────────────────────────────────────

def git(project_path: str, *args) -> tuple[bool, str]:
    """Run a git command in the given project path."""
    cmd = ["git", "-C", project_path] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log.warning(f"git {' '.join(args)} in {project_path} failed: {result.stderr.strip()}")
            return False, result.stderr.strip()
        return True, result.stdout.strip()
    except Exception as e:
        log.warning(f"git {' '.join(args)} exception: {e}")
        return False, str(e)


def git_pull(project_path: str) -> tuple[bool, str]:
    log.info(f"git pull — {project_path}")
    ok, out = git(project_path, "pull", "--ff-only", GIT_REMOTE, GIT_BRANCH)
    if ok:
        log.info(f"git pull: {out or 'already up to date'}")
    return ok, out


# ── Bible read ────────────────────────────────────────────────────────────────

def read_bible(project_path: str, task_type: str = None) -> dict:
    """
    Read bible files from the project path.
    Core bible/ files always included.
    fragments/ included only for FRAGMENT and BIBLE_UPDATE tasks.
    Returns {relpath: content} dict.
    """
    files = {}
    bible_dir = os.path.join(project_path, "bible")

    if os.path.isdir(bible_dir):
        for f in sorted(os.listdir(bible_dir)):
            if f.endswith(".md") and not f.startswith("."):
                _load_file(project_path, os.path.join("bible", f), files)
    else:
        log.warning(f"bible/ subdir not found in {project_path} — falling back to top-level .md files")
        for f in sorted(os.listdir(project_path)):
            fp = os.path.join(project_path, f)
            if f.endswith(".md") and os.path.isfile(fp) and not f.startswith("."):
                _load_file(project_path, f, files)

    if task_type in FRAGMENT_TASKS:
        fragments_dir = os.path.join(project_path, "fragments")
        if os.path.isdir(fragments_dir):
            for f in sorted(os.listdir(fragments_dir)):
                if f.endswith(".md") and not f.startswith("."):
                    _load_file(project_path, os.path.join("fragments", f), files)

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

def write_bible(
    project_path: str,
    filename: str,
    content: str,
    append: bool,
    commit_message: str = None,
) -> tuple[bool, str]:
    filepath = os.path.join(project_path, filename)
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
    ok, _ = git(project_path, "add", filename)
    if not ok:
        return False, f"git add failed for {filename}"

    ok, out = git(project_path, "commit", "-m", msg)
    if not ok:
        if "nothing to commit" in out.lower():
            return True, f"{filename} unchanged — nothing to commit"
        return False, f"git commit failed: {out}"

    ok, out = git(project_path, "push", GIT_REMOTE, GIT_BRANCH)
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
        return self.headers.get("Authorization", "") == f"Bearer {BRIDGE_TOKEN}"

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

    def _require_project(self, params: dict) -> tuple[str | None, str | None]:
        """Resolve project from query params. Returns (path, None) or (None, error)."""
        project = params.get("project", [None])[0]
        return resolve_project(project)

    def do_GET(self):
        if not self._auth_ok():
            self._send_json(401, {"error": "Unauthorized"})
            return

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/health":
            projects = list_projects()
            self._send_json(200, {
                "status": "ok",
                "bible_root": BIBLE_ROOT,
                "bible_root_exists": os.path.exists(BIBLE_ROOT),
                "projects": projects,
            })

        elif parsed.path == "/projects":
            self._send_json(200, {"projects": list_projects()})

        elif parsed.path == "/bible":
            project_path, err = self._require_project(params)
            if err:
                self._send_json(400, {"error": err})
                return
            task_type = params.get("task_type", [None])[0]
            result = read_bible(project_path, task_type)
            self._send_json(200, result)

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._auth_ok():
            self._send_json(401, {"error": "Unauthorized"})
            return

        parsed = urlparse(self.path)
        body = self._read_body()

        if parsed.path == "/bible/write":
            project_path, err = resolve_project(body.get("project"))
            if err:
                self._send_json(400, {"error": err})
                return

            filename = body.get("filename")
            content  = body.get("content")
            append   = body.get("append", True)
            commit_message = body.get("commit_message")

            if not filename or content is None:
                self._send_json(400, {"error": "filename and content are required"})
                return

            ok, status = write_bible(project_path, filename, content, append, commit_message)
            self._send_json(200 if ok else 500, {"ok": ok, "status": status})

        elif parsed.path == "/bible/pull":
            project_path, err = resolve_project(body.get("project"))
            if err:
                self._send_json(400, {"error": err})
                return
            ok, out = git_pull(project_path)
            self._send_json(200 if ok else 500, {"ok": ok, "output": out})

        else:
            self._send_json(404, {"error": "Not found"})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not os.path.exists(BIBLE_ROOT):
        log.warning(f"BIBLE_ROOT does not exist: {BIBLE_ROOT}")
        log.warning("Create it and clone your project repos before making requests.")
    else:
        projects = list_projects()
        if projects:
            log.info(f"Found {len(projects)} project(s): {', '.join(projects)}")
        else:
            log.warning(f"No git repos found under {BIBLE_ROOT}")

    log.info(f"Bible bridge starting on port {BRIDGE_PORT}")
    log.info(f"Bible root: {BIBLE_ROOT}")
    log.info(f"Auth: {'enabled' if BRIDGE_TOKEN else 'disabled (set BRIDGE_TOKEN to enable)'}")

    server = HTTPServer(("0.0.0.0", BRIDGE_PORT), BridgeHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Bridge stopped")
