"""
Tests for _build_isolated_command() in utils/runner.py (open-terminal-docker#47).

The helper is pure: given a command, cwd, and sandbox user, it returns the
`sudo -u … bash -c …` string that PtyRunner spawns. The load-bearing behavior
is that it sources /etc/profile.d/open-terminal.sh INSIDE the sudo shell — so
GH_TOKEN/GITHUB_TOKEN (and the gh() wrapper) are set as the sandbox user, which
a non-login `bash -c` otherwise never gets and `sudo` otherwise strips.

Live end-to-end verification (does `gh auth status` actually succeed through the
new path?) is AC4/AC5 in the issue — checked in-pod. This file locks the
construction contract.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from open_terminal.utils.runner import _build_isolated_command, PROFILE_SCRIPT  # noqa: E402


def test_sources_profile_before_command():
    """The profile is sourced before the user command so GH_TOKEN is exported
    into the environment the command runs in."""
    out = _build_isolated_command("gh auth status", None, "u3aa02715")
    assert PROFILE_SCRIPT in out
    # source appears before the command in the inner script
    assert out.index(PROFILE_SCRIPT) < out.index("gh auth status")


def test_runs_as_the_sandbox_user_via_bash_c():
    out = _build_isolated_command("echo hi", None, "u3aa02715")
    assert "sudo -u u3aa02715 -- bash -c " in out


def test_source_is_non_fatal_when_profile_absent():
    """A missing profile (e.g. dev/test image) must not abort the command."""
    out = _build_isolated_command("echo hi", None, "user")
    # tolerate absence: redirect stderr + `|| true`
    assert f". {PROFILE_SCRIPT} 2>/dev/null || true" in out


def test_cwd_is_cd_into_with_short_circuit_preserved():
    """With a cwd, the original `cd <dir> && <cmd>` short-circuit is kept, so a
    failed chdir still prevents the command from running."""
    out = _build_isolated_command("make build", "/work/proj", "user")
    inner = _extract_inner(out)
    assert f"cd {shlex.quote('/work/proj')} && make build" in inner
    # and the profile source precedes the cd
    assert inner.index(PROFILE_SCRIPT) < inner.index("cd ")


def test_run_as_user_is_shell_quoted():
    """A hostile username can't break out of the sudo argument."""
    out = _build_isolated_command("echo hi", None, "evil; rm -rf /")
    assert "sudo -u 'evil; rm -rf /' --" in out


def test_inner_is_shell_quoted_as_one_arg():
    """The whole inner script is a single shlex-quoted argument to bash -c, so
    metacharacters in the command don't leak into the outer shell."""
    out = _build_isolated_command("echo $HOME; whoami", None, "user")
    # everything after `bash -c ` is one quoted token
    after = out.split("bash -c ", 1)[1]
    # shlex can round-trip it back to a single element
    assert len(shlex.split(after)) == 1


def _extract_inner(full: str) -> str:
    """Unquote the single arg passed to `bash -c` for content assertions."""
    after = full.split("bash -c ", 1)[1]
    return shlex.split(after)[0]
