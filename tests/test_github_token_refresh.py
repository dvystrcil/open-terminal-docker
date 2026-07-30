import os

import pytest

from open_terminal.utils.github_token import (
    TOKEN_FILE_CANDIDATES,
    refresh_github_token_env,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def test_refresh_applies_token_from_first_candidate(monkeypatch, tmp_path):
    token_file = tmp_path / "github_token"
    token_file.write_text("ghs_freshtoken123\n")
    monkeypatch.setattr(
        "open_terminal.utils.github_token.TOKEN_FILE_CANDIDATES",
        (str(token_file), "/tmp/does-not-exist-github-token"),
    )

    result = refresh_github_token_env()

    assert result is True
    assert os.environ["GH_TOKEN"] == "ghs_freshtoken123"
    assert os.environ["GITHUB_TOKEN"] == "ghs_freshtoken123"


def test_refresh_falls_back_to_second_candidate(monkeypatch, tmp_path):
    token_file = tmp_path / "github_token"
    token_file.write_text("ghs_secondcandidate\n")
    monkeypatch.setattr(
        "open_terminal.utils.github_token.TOKEN_FILE_CANDIDATES",
        ("/tmp/does-not-exist-github-token", str(token_file)),
    )

    result = refresh_github_token_env()

    assert result is True
    assert os.environ["GH_TOKEN"] == "ghs_secondcandidate"


def test_refresh_overwrites_stale_env_value(monkeypatch, tmp_path):
    os.environ["GH_TOKEN"] = "ghs_staletoken_from_boot"
    token_file = tmp_path / "github_token"
    token_file.write_text("ghs_brandnew\n")
    monkeypatch.setattr(
        "open_terminal.utils.github_token.TOKEN_FILE_CANDIDATES",
        (str(token_file),),
    )

    refresh_github_token_env()

    assert os.environ["GH_TOKEN"] == "ghs_brandnew"


def test_refresh_noop_when_no_candidate_exists(monkeypatch):
    monkeypatch.setattr(
        "open_terminal.utils.github_token.TOKEN_FILE_CANDIDATES",
        ("/tmp/does-not-exist-a", "/tmp/does-not-exist-b"),
    )

    result = refresh_github_token_env()

    assert result is False
    assert "GH_TOKEN" not in os.environ


def test_default_candidates_are_secrets_then_tmp():
    assert TOKEN_FILE_CANDIDATES == ("/run/secrets/github_token", "/tmp/github_token")
