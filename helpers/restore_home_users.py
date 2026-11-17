#!/usr/bin/env python3
"""Re-create the OS user behind each persistent /home/<name> (open-terminal-docker#89).

Run by entrypoint.sh before the server starts. /home is a persistent NFS
volume but /etc/passwd resets with the container, so after every restart
upstream's ensure_os_user() sees no passwd entry, runs `useradd -m` (which
may hand out a different uid) and then `chown -R` over the whole home. Over
NFS that takes minutes, and the user's first request waits for it.

This re-adds each missing owner with the uid and gid already on disk, so
getpwnam() succeeds and ensure_os_user() returns before its chown. It adds
the server user to the new group as ensure_os_user() would. That only
updates /etc/group; the running server can't refresh its groups (no
CAP_SETGID), the same as before.

Anything it can't map cleanly is skipped and left to ensure_os_user():
root-owned dirs, invalid usernames, a uid or gid already owned by a
different name. It never fails startup.
"""
from __future__ import annotations

import argparse
import grp
import os
import pwd
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# useradd's default NAME_REGEX, and its 32-char limit
VALID_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Home:
    name: str
    uid: int
    gid: int


def scan(root: Path) -> list[Home]:
    homes = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and not p.is_symlink():
            st = p.stat()
            homes.append(Home(p.name, st.st_uid, st.st_gid))
    return homes


def plan(homes: list[Home], *, users: dict[str, int], groups: dict[str, int],
         server_user: str) -> list[list[str]]:
    """Commands that re-create each missing owner. `users`/`groups`: name -> id."""
    users, groups = dict(users), dict(groups)
    cmds: list[list[str]] = []
    for h in homes:
        if h.name in users or h.uid == 0 or not VALID_NAME.match(h.name):
            continue
        if h.uid in users.values():
            continue                    # another name owns this uid
        gname = next((g for g, i in groups.items() if i == h.gid), None)
        if gname not in (None, h.name):
            continue                    # another group owns this gid
        if gname is None:
            cmds.append(["groupadd", "-g", str(h.gid), h.name])
            groups[h.name] = h.gid
        cmds.append(["useradd", "-M", "-u", str(h.uid), "-g", str(h.gid),
                     "-d", f"/home/{h.name}", "-s", "/bin/bash", h.name])
        cmds.append(["usermod", "-aG", h.name, server_user])
        users[h.name] = h.uid
    return cmds


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home-root", default="/home")
    a = ap.parse_args(argv)
    try:
        cmds = plan(scan(Path(a.home_root)),
                    users={p.pw_name: p.pw_uid for p in pwd.getpwall()},
                    groups={g.gr_name: g.gr_gid for g in grp.getgrall()},
                    server_user=os.getenv("USER", "user"))
    except OSError as e:
        print(f"restore-home-users: WARNING: cannot scan {a.home_root}: {e}",
              file=sys.stderr)
        return 0
    for cmd in cmds:
        try:
            subprocess.run(["sudo", "-n", *cmd], check=True,
                           capture_output=True, text=True)
            print(f"restore-home-users: {' '.join(cmd)}")
        except (OSError, subprocess.CalledProcessError) as e:
            err = getattr(e, "stderr", "") or str(e)
            print(f"restore-home-users: WARNING: {' '.join(cmd)} failed: "
                  f"{err.strip()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
