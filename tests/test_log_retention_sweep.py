"""
Tests for the filesystem-based process-log retention sweep (homelab#720).

Bug: _cleanup_expired() only deletes a log file when it *also* has a
matching in-memory BackgroundProcess record. _processes is in-memory and
doesn't survive a pod restart, but the log files live on a persistent
volume that does -- so any log file older than the last restart was
permanently unreachable by that path regardless of age. Surfaced live: a
process log from ~2 months prior was still present, containing a
plaintext GITHUB_APP_PRIVATE_KEY from a command that had echoed it.

Fix: _sweep_expired_log_files() reads mtimes directly off disk instead,
independent of any in-memory process record.
"""

from __future__ import annotations

import os
import time

import open_terminal.main as main


def _touch(path: str, mtime: float) -> None:
    with open(path, "w") as f:
        f.write("{}")
    os.utime(path, (mtime, mtime))


def test_deletes_files_older_than_retention(tmp_path):
    now = time.time()
    old = tmp_path / "old.jsonl"
    _touch(str(old), now - (main.PROCESS_LOG_RETENTION + 10))

    deleted = main._sweep_expired_log_files(str(tmp_path), now=now)

    assert str(old) in deleted
    assert not old.exists()


def test_keeps_files_within_retention(tmp_path):
    now = time.time()
    recent = tmp_path / "recent.jsonl"
    _touch(str(recent), now - 10)

    deleted = main._sweep_expired_log_files(str(tmp_path), now=now)

    assert deleted == []
    assert recent.exists()


def test_survives_across_a_reset_process_registry(tmp_path):
    """The exact bug: an old file with no in-memory record must still go."""
    now = time.time()
    orphaned = tmp_path / "orphaned-no-in-memory-record.jsonl"
    _touch(str(orphaned), now - (main.PROCESS_LOG_RETENTION + 1))
    main._processes.clear()  # simulates a pod restart wiping the registry

    deleted = main._sweep_expired_log_files(str(tmp_path), now=now)

    assert str(orphaned) in deleted
    assert not orphaned.exists()


def test_ignores_non_jsonl_files(tmp_path):
    now = time.time()
    other = tmp_path / "old.txt"
    _touch(str(other), now - (main.PROCESS_LOG_RETENTION + 10))

    deleted = main._sweep_expired_log_files(str(tmp_path), now=now)

    assert deleted == []
    assert other.exists()


def test_missing_directory_returns_empty_without_raising(tmp_path):
    missing = tmp_path / "does-not-exist"

    deleted = main._sweep_expired_log_files(str(missing))

    assert deleted == []
