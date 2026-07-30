"""Keep GH_TOKEN/GITHUB_TOKEN fresh in this process's own environment.

entrypoint.sh mints a GitHub App installation token at container start and
refreshes it on disk every 50 minutes (tokens live 60 minutes), but that
refresh happens in a *separate* bash process. A long-lived Python process's
``os.environ`` is a one-time snapshot taken at interpreter start — nothing
external can mutate it, so any subprocess built from ``os.environ`` (directly,
or via ``{**os.environ, ...}``) silently inherits whatever token was valid at
boot, forever. Command execution that uses the plain-shell path (``shell=True``
→ ``/bin/sh``, which is ``dash`` here, not bash) never sources
``/etc/profile.d`` either, so it can't self-correct that way.

Re-reading the token file into this process's own ``os.environ`` right before
building a subprocess environment closes that gap for every execution path,
regardless of shell or ``run_as_user``. See dvystrcil/homelab#701.
"""

import os

TOKEN_FILE_CANDIDATES = ("/run/secrets/github_token", "/tmp/github_token")


def refresh_github_token_env() -> bool:
    """Re-read the current GitHub App token from disk into os.environ.

    Returns True if a token file was found and applied, False if neither
    candidate path exists (e.g. GH_TOKEN not in use in this deployment) —
    in that case os.environ is left untouched.
    """
    for path in TOKEN_FILE_CANDIDATES:
        try:
            with open(path, "r") as f:
                token = f.read().strip()
        except OSError:
            continue
        if not token:
            continue
        os.environ["GH_TOKEN"] = token
        os.environ["GITHUB_TOKEN"] = token
        return True
    return False
