#!/usr/bin/env python3
"""
Generate a GitHub App installation token from environment variables.

Required env vars:
  GITHUB_APP_ID              — numeric App ID
  GITHUB_APP_PRIVATE_KEY     — PEM private key (supports \\n-escaped or real newlines)
  GITHUB_APP_INSTALLATION_ID — installation ID for your account/org

Outputs the raw token to stdout (no trailing newline).
Exit code 1 on any failure so the caller can detect and warn.
"""

import os
import sys
import time
import json
import urllib.request
import urllib.error

try:
    import jwt  # PyJWT
except ImportError:
    print("ERROR: PyJWT not installed. Run: pip install PyJWT cryptography", file=sys.stderr)
    sys.exit(1)

APP_ID              = os.environ.get("GITHUB_APP_ID", "").strip()
PRIVATE_KEY_RAW     = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
INSTALLATION_ID     = os.environ.get("GITHUB_APP_INSTALLATION_ID", "").strip()

if not APP_ID or not PRIVATE_KEY_RAW or not INSTALLATION_ID:
    print("ERROR: GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, and GITHUB_APP_INSTALLATION_ID must all be set", file=sys.stderr)
    sys.exit(1)

# Support both real newlines and \n-escaped strings (common when injected via env)
PRIVATE_KEY = PRIVATE_KEY_RAW.replace("\\n", "\n")


def generate_jwt() -> str:
    now = int(time.time())
    payload = {
        "iat": now - 60,   # backdate 60s to account for clock skew
        "exp": now + 540,  # 9 minutes (GitHub max is 10)
        "iss": APP_ID,
    }
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")


def get_installation_token(app_jwt: str) -> str:
    url = f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "open-terminal-github-app",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return data["token"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: GitHub API returned {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR: Could not reach GitHub API: {e.reason}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    try:
        app_jwt = generate_jwt()
        token   = get_installation_token(app_jwt)
        print(token, end="")  # no trailing newline — safe for $() capture
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)