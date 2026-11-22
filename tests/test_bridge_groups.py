"""The bible bridge must start with the groups restore_home_users just added.

BIBLE_ROOT is inside a user's 2770 home (/home/u3aa02715/fiction). The
entrypoint adds the server user to that user's group with `usermod`, but a
running shell keeps the groups it started with, and the bridge has no
CAP_SETGID to refresh them (the server does, via setcap). On NFS the export
mapped every uid to root, so this never showed. On Longhorn the bridge got
`bible_root_exists: false` (open-terminal-docker#89 follow-up, 2026-11-22).

`sudo -u <self>` re-reads /etc/group (initgroups), so the bridge starts with
the new group. The image-level check is tests/bridge-groups-test.sh.
"""

from __future__ import annotations

import re
from pathlib import Path

ENTRYPOINT = (Path(__file__).resolve().parent.parent / "entrypoint.sh").read_text()
LAUNCH = re.compile(r"python3 /app/helpers/bible_bridge\.py")


def function_body(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", ENTRYPOINT, re.S | re.M)
    assert m, f"{name}() not defined in entrypoint.sh"
    return m.group(1)


def test_bridge_is_launched_only_through_the_helper():
    body = function_body("start_bible_bridge")
    outside = LAUNCH.findall(ENTRYPOINT.replace(body, ""))
    assert outside == [], "a bridge launch bypasses start_bible_bridge"
    assert len(LAUNCH.findall(body)) == 1


def test_every_exec_path_starts_the_bridge():
    # three exec paths: no iptables, egress firewall, default
    calls = re.findall(r"^\s*start_bible_bridge\s*$", ENTRYPOINT, re.M)
    assert len(calls) == 3


def test_helper_refreshes_groups_and_keeps_env():
    body = function_body("start_bible_bridge")
    assert re.search(r'sudo --preserve-env -u "\$\(id -un\)"', body), body
    # group-writable files, so the home's owner can edit what the bridge writes
    assert "umask 002" in body


def test_helper_is_defined_before_first_use():
    first_call = re.search(r"^\s*start_bible_bridge\s*$", ENTRYPOINT, re.M).start()
    assert ENTRYPOINT.index("start_bible_bridge() {") < first_call
