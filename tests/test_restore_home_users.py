"""Tests for helpers/restore_home_users.py (open-terminal-docker#89).

/home is a persistent NFS volume; /etc/passwd lives in the container and
resets on every restart. Upstream's ensure_os_user() treats a missing passwd
entry as a new user: `useradd -m` (next free uid) and then `chown -R` of the
whole home over NFS -- 11k files, minutes, while the user's first request
waits. The entrypoint now re-creates each home's owner with the uid and gid
already on disk, before the server starts, so getpwnam() succeeds and the
chown never runs.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "helpers"))

import restore_home_users as r  # noqa: E402

PASSWD = {"root": 0, "user": 1000}
GROUPS = {"root": 0, "user": 1000, "docker": 999}


def plan(homes, users=PASSWD, groups=GROUPS):
    return r.plan(homes, users=users, groups=groups, server_user="user")


def test_missing_owner_is_recreated_with_its_uid_and_never_chowned():
    cmds = plan([r.Home("u3aa02715", uid=1001, gid=1001)])
    assert cmds == [
        ["groupadd", "-g", "1001", "u3aa02715"],
        ["useradd", "-M", "-u", "1001", "-g", "1001", "-d", "/home/u3aa02715",
         "-s", "/bin/bash", "u3aa02715"],
        ["usermod", "-aG", "u3aa02715", "user"],
    ]
    assert not any(c[0] == "chown" for c in cmds)


def test_existing_user_is_left_alone():
    assert plan([r.Home("user", uid=1000, gid=1000)]) == []


def test_root_owned_home_is_not_a_user():
    # live: /home/topology-mapper is 0:0, mode 777
    assert plan([r.Home("topology-mapper", uid=0, gid=0)]) == []


def test_uid_taken_by_another_name_is_skipped():
    # recreating would give two names one uid; leave it to ensure_os_user
    assert plan([r.Home("u12345678", uid=1000, gid=1000)]) == []


def test_existing_group_with_that_gid_is_reused():
    cmds = plan([r.Home("u3aa02715", uid=1001, gid=1001)],
                groups={**GROUPS, "u3aa02715": 1001})
    assert cmds[0][0] == "useradd"


def test_gid_owned_by_a_different_group_is_skipped():
    assert plan([r.Home("u3aa02715", uid=1001, gid=999)]) == []


def test_invalid_username_is_skipped():
    for name in ("lost+found", "Bad", "1abc", "-x", "a" * 40):
        assert plan([r.Home(name, uid=1001, gid=1001)]) == [], name


def test_two_homes_are_each_recreated():
    cmds = plan([r.Home("ua", uid=1001, gid=1001),
                 r.Home("ub", uid=1002, gid=1002)])
    assert [c[-1] for c in cmds if c[0] == "useradd"] == ["ua", "ub"]


def test_scan_reads_owner_from_disk(tmp_path):
    (tmp_path / "ua").mkdir()
    (tmp_path / "file.txt").write_text("not a home")
    homes = r.scan(tmp_path)
    assert [h.name for h in homes] == ["ua"]
    st = (tmp_path / "ua").stat()
    assert (homes[0].uid, homes[0].gid) == (st.st_uid, st.st_gid)


def test_main_never_fails_startup(monkeypatch, tmp_path, capsys):
    (tmp_path / "ua").mkdir()
    monkeypatch.setattr(r, "plan", lambda *a, **k: [["false"]])
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        raise OSError("sudo missing")
    monkeypatch.setattr(r.subprocess, "run", fake_run)
    assert r.main(["--home-root", str(tmp_path)]) == 0
    assert calls == [["sudo", "-n", "false"]]
    assert "WARNING" in capsys.readouterr().err


def test_entrypoint_restores_users_before_anything_starts():
    text = (REPO / "entrypoint.sh").read_text()
    call = text.index("restore_home_users.py")
    first_bg = min(m.start() for m in re.finditer(r"\) &\n", text))
    first_exec = text.index("exec open-terminal")
    assert call < first_bg and call < first_exec
