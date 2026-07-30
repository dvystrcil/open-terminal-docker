"""
Tests for the two-tier process-expiry fix (open-terminal#13 / homelab#391).

Bug: a finished process's in-memory record was auto-deleted 300s after
completion regardless of whether anyone had actually retrieved its
result yet. A caller whose own dispatch loop stalls past that window
(the OWUI tool-dispatch hang tracked in homelab#391 is reportedly
unbounded, not just ≥300s) would come back to "Process not found" —
permanent, silent loss of a command that actually succeeded.

Fix: track delivered_at separately from finished_at. Undelivered
results get PROCESS_UNDELIVERED_EXPIRY (long); delivered results get
the original short PROCESS_EXPIRY, since the caller already has what
it needs.
"""

from __future__ import annotations

import time

import open_terminal.main as main


def _fake_process(process_id: str, *, finished_at: float | None,
                  delivered_at: float | None = None) -> main.BackgroundProcess:
    return main.BackgroundProcess(
        id=process_id,
        command="echo hi",
        runner=None,  # not touched by _cleanup_expired
        status="done" if finished_at else "running",
        finished_at=finished_at,
        delivered_at=delivered_at,
    )


def setup_function(_):
    main._processes.clear()


def test_undelivered_process_survives_past_short_expiry():
    now = time.time()
    main._processes["p1"] = _fake_process(
        "p1", finished_at=now - (main.PROCESS_EXPIRY + 5))
    main._cleanup_expired()
    assert "p1" in main._processes


def test_undelivered_process_expires_after_long_window():
    now = time.time()
    main._processes["p1"] = _fake_process(
        "p1", finished_at=now - (main.PROCESS_UNDELIVERED_EXPIRY + 5))
    main._cleanup_expired()
    assert "p1" not in main._processes


def test_delivered_process_expires_on_short_window_not_long_one():
    now = time.time()
    main._processes["p1"] = _fake_process(
        "p1",
        finished_at=now - (main.PROCESS_UNDELIVERED_EXPIRY - 5),
        delivered_at=now - (main.PROCESS_EXPIRY + 5),
    )
    main._cleanup_expired()
    assert "p1" not in main._processes


def test_delivered_process_survives_within_short_window():
    now = time.time()
    main._processes["p1"] = _fake_process(
        "p1", finished_at=now - 10, delivered_at=now - 10)
    main._cleanup_expired()
    assert "p1" in main._processes


def test_running_process_never_expires():
    main._processes["p1"] = _fake_process("p1", finished_at=None)
    main._cleanup_expired()
    assert "p1" in main._processes
