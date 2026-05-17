"""
Tests for the _actor_env() helper in bible_bridge (homelab#125 —
git author aliases).

The helper is pure: it takes an actor name and returns a git env dict
(or None when no actor is set, preserving pod default config). These
tests verify both branches plus sanitisation.

Integration testing (does an actual `git commit` end up with the
expected author?) happens via the bin/tests/test_dmf_validate_*.sh
shape elsewhere or via manual smoke after a real /sync_up call. This
file just locks the contract of the helper itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

# bible_bridge.py is a standalone script (not a package), so we add its
# parent to sys.path and import the module by file. Avoids needing
# helpers/__init__.py just for tests.
HELPERS_DIR = Path(__file__).resolve().parent.parent / "helpers"
sys.path.insert(0, str(HELPERS_DIR))

import bible_bridge  # noqa: E402


def test_actor_env_none_for_empty_actor():
    """No actor set → returns None → caller falls back to pod default git config."""
    assert bible_bridge._actor_env(None) is None
    assert bible_bridge._actor_env("") is None
    assert bible_bridge._actor_env("   ") is None


def test_actor_env_basic_actor():
    """Plain actor name → returns env dict with dvystrcil-<actor> identity."""
    env = bible_bridge._actor_env("fiction")
    assert env is not None
    assert env["GIT_AUTHOR_NAME"] == "dvystrcil-fiction"
    assert env["GIT_AUTHOR_EMAIL"] == "fiction@dvystrcil.local"
    assert env["GIT_COMMITTER_NAME"] == "dvystrcil-fiction"
    assert env["GIT_COMMITTER_EMAIL"] == "fiction@dvystrcil.local"


def test_actor_env_preserves_parent_environment():
    """The returned env should inherit os.environ (so PATH, HOME, etc.
    are still available for git's own subprocess needs)."""
    import os
    os.environ["TEST_ACTOR_ENV_SENTINEL"] = "sentinel-value-12345"
    try:
        env = bible_bridge._actor_env("claude")
        assert env is not None
        assert env.get("TEST_ACTOR_ENV_SENTINEL") == "sentinel-value-12345"
    finally:
        del os.environ["TEST_ACTOR_ENV_SENTINEL"]


def test_actor_env_lowercases_actor():
    """ACTOR is normalised to lowercase to match the dvystrcil-<actor>
    pattern and avoid case-sensitive author-mismatch surprises."""
    env = bible_bridge._actor_env("Claude")
    assert env["GIT_AUTHOR_NAME"] == "dvystrcil-claude"
    assert env["GIT_AUTHOR_EMAIL"] == "claude@dvystrcil.local"


def test_actor_env_sanitises_dangerous_chars():
    """Shell-meaningful chars must be stripped — env vars get used in
    git subprocess invocations, can't pass arbitrary user input."""
    # Path-separator, semicolon, quotes, backticks — all stripped.
    env = bible_bridge._actor_env("evil/../actor;`whoami`'\"$(rm -rf)")
    assert env is not None
    # Only [a-z0-9-] survives. The result should still be a valid
    # identifier — just nothing dangerous remains.
    assert "/" not in env["GIT_AUTHOR_NAME"]
    assert ";" not in env["GIT_AUTHOR_NAME"]
    assert "`" not in env["GIT_AUTHOR_NAME"]
    assert "$" not in env["GIT_AUTHOR_NAME"]
    assert env["GIT_AUTHOR_NAME"].startswith("dvystrcil-")


def test_actor_env_empty_after_sanitisation_returns_none():
    """If sanitisation strips everything (e.g., actor was all special chars),
    return None rather than a malformed dvystrcil- identity."""
    assert bible_bridge._actor_env("!!!@@@###") is None
    assert bible_bridge._actor_env("$$$") is None


def test_actor_env_hyphenated_actor_preserved():
    """Hyphens are allowed for compound names like 'fiction-bg-sync'."""
    env = bible_bridge._actor_env("fiction-bg-sync")
    assert env["GIT_AUTHOR_NAME"] == "dvystrcil-fiction-bg-sync"
    assert env["GIT_AUTHOR_EMAIL"] == "fiction-bg-sync@dvystrcil.local"
