"""
Tests for git_recover() in bible_bridge (homelab#176 — /bible/recover
endpoint, the destructive-reset auto-recovery path for a local main that
has diverged from origin/main at the SHA level).

Uses REAL git repos (a local bare repo as "origin", a real clone as the
working tree) rather than mocking subprocess calls — git_recover's whole
job is orchestrating real git commands correctly, so a mock would just
encode our assumptions about git's behavior instead of testing it
(feedback_mocks_encode_assumed_contracts).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HELPERS_DIR = Path(__file__).resolve().parent.parent / "helpers"
sys.path.insert(0, str(HELPERS_DIR))

import bible_bridge  # noqa: E402


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True, text=True, check=True,
    )


def _write_commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content)
    _run(repo, "add", filename)
    _run(repo, "commit", "-m", message)
    sha = _run(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    return sha


@pytest.fixture
def repo_pair(tmp_path, monkeypatch):
    """Real bare 'origin' repo + a real clone as the working tree, both
    with git identity configured so commits succeed hermetically."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                    capture_output=True, text=True, check=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(seed)],
                    capture_output=True, text=True, check=True)
    _run(seed, "config", "user.email", "test@example.com")
    _run(seed, "config", "user.name", "Test")
    _write_commit(seed, "bible.md", "seed content\n", "seed commit")
    _run(seed, "remote", "add", "origin", str(origin))
    _run(seed, "push", "origin", "main")

    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)],
                    capture_output=True, text=True, check=True)
    _run(work, "config", "user.email", "test@example.com")
    _run(work, "config", "user.name", "Test")

    monkeypatch.setattr(bible_bridge, "GIT_REMOTE", "origin")
    monkeypatch.setattr(bible_bridge, "GIT_BRANCH", "main")

    return origin, work


def test_recover_requires_confirm(repo_pair):
    """Without confirm=True, refuse outright — no git commands run at all."""
    _origin, work = repo_pair
    ok, payload = bible_bridge.git_recover(str(work), confirm=False)
    assert ok is False
    assert payload == {"ok": False, "status": "confirm_required"}


def test_recover_refuses_uncommitted_changes(repo_pair):
    """A dirty working tree is never auto-reset — no data-loss surprise."""
    _origin, work = repo_pair
    (work / "bible.md").write_text("uncommitted local edit\n")
    ok, payload = bible_bridge.git_recover(str(work), confirm=True)
    assert ok is False
    assert payload["status"] == "uncommitted_changes_present"
    assert "bible.md" in payload["porcelain"]


def test_recover_happy_path_diverged_main(repo_pair):
    """The core scenario: local main has a commit origin doesn't (e.g.
    post-squash-merge). Recovery resets to origin/main and reports what
    was abandoned."""
    origin, work = repo_pair

    # Simulate origin moving forward (as if merged elsewhere)...
    seed2 = origin.parent / "seed2"
    subprocess.run(["git", "clone", str(origin), str(seed2)],
                    capture_output=True, text=True, check=True)
    _run(seed2, "config", "user.email", "test@example.com")
    _run(seed2, "config", "user.name", "Test")
    upstream_sha = _write_commit(seed2, "other.md", "upstream content\n", "upstream commit")
    _run(seed2, "push", "origin", "main")

    # ...while the local work clone independently commits something that
    # never made it to origin (the exact "diverged main" wedge).
    local_sha = _write_commit(work, "local.md", "local-only content\n", "local-only commit")

    ok, payload = bible_bridge.git_recover(str(work), confirm=True)

    assert ok is True
    assert payload["ok"] is True
    assert payload["abandoned"]["branch"] == "main"
    assert payload["abandoned"]["head_sha"] == local_sha
    dropped_shas = [c["sha"] for c in payload["abandoned"]["commits_dropped"]]
    assert local_sha in dropped_shas
    assert payload["now_at"]["branch"] == "main"
    assert payload["now_at"]["head_sha"] == upstream_sha

    # Real filesystem state matches the report — the dropped file is gone,
    # the upstream-only file is present.
    assert not (work / "local.md").exists()
    assert (work / "other.md").exists()

    # Working tree is genuinely clean after the reset.
    status = _run(work, "status", "--porcelain").stdout
    assert status.strip() == ""


def test_recover_on_a_non_default_branch(repo_pair):
    """Divergence can also be discovered while sitting on a feature branch
    (not just main itself) — recovery should still land on GIT_BRANCH."""
    origin, work = repo_pair
    _run(work, "checkout", "-b", "some-feature-branch")
    local_sha = _write_commit(work, "scratch.md", "scratch\n", "scratch commit")

    ok, payload = bible_bridge.git_recover(str(work), confirm=True)

    assert ok is True
    assert payload["abandoned"]["branch"] == "some-feature-branch"
    assert payload["abandoned"]["head_sha"] == local_sha
    assert payload["now_at"]["branch"] == "main"
    current_branch = _run(work, "branch", "--show-current").stdout.strip()
    assert current_branch == "main"


def test_recover_no_divergence_is_a_noop_success(repo_pair):
    """If main already matches origin/main exactly, recovery still
    succeeds — just with an empty commits_dropped list."""
    _origin, work = repo_pair
    ok, payload = bible_bridge.git_recover(str(work), confirm=True)
    assert ok is True
    assert payload["abandoned"]["commits_dropped"] == []
    assert payload["now_at"]["head_sha"] == payload["abandoned"]["head_sha"]
