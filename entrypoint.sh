#!/bin/bash
set -e

# -----------------------------------------------------------------------
# Docker-secrets support: resolve <VAR>_FILE → <VAR>
# Follows the convention used by the official PostgreSQL image.
# -----------------------------------------------------------------------
file_env() {
    local var="$1"
    local fileVar="${var}_FILE"
    local def="${2:-}"
    if [ "${!var+set}" = "set" ] && [ "${!fileVar+set}" = "set" ]; then
        printf >&2 'error: both %s and %s are set (but are exclusive)\n' "$var" "$fileVar"
        exit 1
    fi
    local val="$def"
    if [ "${!var:-}" ]; then
        val="${!var}"
    elif [ "${!fileVar:-}" ]; then
        val="$(< "${!fileVar}")"
    fi
    export "$var"="$val"
    unset "$fileVar"
}

file_env 'OPEN_TERMINAL_API_KEY'

# Also support _FILE variants for GitHub App credentials
file_env 'GITHUB_APP_ID'
file_env 'GITHUB_APP_PRIVATE_KEY'
file_env 'GITHUB_APP_INSTALLATION_ID'

# -----------------------------------------------------------------------
# GitHub App Token — generate installation token and background-refresh
# Replaces static PAT with short-lived tokens (1hr) minted from the
# App private key. Runs before all exec paths so every code path gets
# a valid token regardless of egress firewall / capsh branching.
#
# Reads from env:
#   GITHUB_APP_ID             — numeric App ID
#   GITHUB_APP_PRIVATE_KEY    — PEM private key (newlines as \n or real)
#   GITHUB_APP_INSTALLATION_ID — installation ID for your account/org
#
# Writes to:
#   /tmp/github_token         — raw token, mode 600
#   GH_TOKEN / GITHUB_TOKEN   — exported for gh CLI and git credential helpers
# -----------------------------------------------------------------------
_refresh_github_token() {
    local token
    token=$(python3 /app/helpers/github_app_token.py 2>/dev/null) || {
        echo "WARNING: GitHub App token generation failed" >&2
        return 1
    }
    echo "$token" > /tmp/github_token
    chmod 600 /tmp/github_token
    export GH_TOKEN="$token"
    export GITHUB_TOKEN="$token"
}

if [ -n "${GITHUB_APP_ID:-}" ] && \
   [ -n "${GITHUB_APP_PRIVATE_KEY:-}" ] && \
   [ -n "${GITHUB_APP_INSTALLATION_ID:-}" ]; then

    echo "GitHub App: generating initial installation token..."
    _refresh_github_token && echo "GitHub App: token ready" || true

    # Background refresh every 50 minutes (tokens expire after 60 min).
    # Writes to /tmp/github_token so the capsh path can also benefit
    # even though it cannot receive exported env vars from this loop.
    (
        while true; do
            sleep 3000
            _refresh_github_token || true
        done
    ) &
    echo "GitHub App: refresh loop started (PID $!)"
fi

# Install a git credential helper that reads the current token from disk at
# authentication time so git operations in long-running shells never use a
# stale token from a frozen env var.
sudo tee /usr/local/bin/git-credential-github-token > /dev/null << 'CRED'
#!/bin/bash
TOKEN_FILE="/run/secrets/github_token"
[ -f "$TOKEN_FILE" ] || TOKEN_FILE="/tmp/github_token"
[ -f "$TOKEN_FILE" ] || exit 1
printf 'username=x-access-token\npassword=%s\n' "$(cat "$TOKEN_FILE")"
CRED
sudo chmod +x /usr/local/bin/git-credential-github-token

# Fix permissions of the home directory if the user doesn't own it
OWNER=$(stat -c '%U' /home/user 2>/dev/null || echo "user")

if [ "$OWNER" != "user" ]; then
    sudo chown -R user:user /home/user 2>/dev/null || true
fi

# add helper files to the user's home directory
# NOTE: /app/helpers/.gitconfig exists and will overwrite $HOME/.gitconfig,
# so the credential.helper config must be set AFTER this copy.
cp -r /app/helpers/. "$HOME/" 2>/dev/null || true
git config --global credential.helper /usr/local/bin/git-credential-github-token

# ── Credential drift guard ────────────────────────────────────────────────
# Defends against two recurring failure modes:
#   1. ~/.gitconfig credential.helper getting silently changed (homelab#58 —
#      caused 7 days of broken fiction-writer git pushes after a manual edit
#      switched it from this helper to 'store')
#   2. Repo-level .git/config remote URLs getting embedded credentials baked
#      in (homelab#59 — discovered across 10 repos when audit-after-fix ran
#      on a single instance). Each is its own drift surface; both must be
#      monitored.
#
# Runs once at pod start (after the initial set above) AND every 600s in
# a background loop. Drift events are logged to stderr so they're visible
# in `kubectl logs`.

_drift_guard_helper_path="/usr/local/bin/git-credential-github-token"
_drift_guard_home="/home/u3aa02715"

_drift_check_gitconfig_helper() {
    local target="$_drift_guard_home/.gitconfig"
    [ -f "$target" ] || return 0
    local actual
    actual=$(grep -E '^[[:space:]]*helper' "$target" 2>/dev/null | head -1 | sed 's/^.*=[[:space:]]*//' | tr -d '[:space:]')
    case "$actual" in
        "$_drift_guard_helper_path"|"!$_drift_guard_helper_path") return 0 ;;
        *)
            echo "$(date -u +%FT%TZ) drift-guard: gitconfig credential.helper drifted to '$actual' — restoring" >&2
            sudo -u u3aa02715 git config --global --replace-all credential.helper "$_drift_guard_helper_path" 2>/dev/null \
              || git -c safe.directory=* config --file "$target" credential.helper "$_drift_guard_helper_path" 2>/dev/null \
              || true
            ;;
    esac
}

_drift_check_embedded_creds() {
    # Find every repo's .git/config under the user's home + strip embedded creds.
    find "$_drift_guard_home" -maxdepth 6 -name 'config' -path '*/.git/*' 2>/dev/null | while read -r cfg; do
        # Match credentials inside https URLs: //<user>:<token>@host or //<token>@host
        if grep -qE '^[[:space:]]*url[[:space:]]*=[[:space:]]*https?://[^/@[:space:]]+@' "$cfg" 2>/dev/null; then
            local repo_dir
            repo_dir=$(dirname "$(dirname "$cfg")")
            local masked
            masked=$(grep -E '^[[:space:]]*url' "$cfg" | head -1 | sed 's|//[^@]*@|//<REDACTED>@|')
            echo "$(date -u +%FT%TZ) drift-guard: embedded creds in $repo_dir → was '$masked' — stripping" >&2
            sed -i -E 's|(url[[:space:]]*=[[:space:]]*https?://)[^/@]+@|\1|' "$cfg"
        fi
    done
}

# Run once at startup
_drift_check_gitconfig_helper
_drift_check_embedded_creds

# And every 600s (10 minutes) in the background. Short-enough cadence that
# a mid-session drift is caught before the next user action that depends on
# correct credential plumbing.
(
    while true; do
        sleep 600
        _drift_check_gitconfig_helper || true
        _drift_check_embedded_creds || true
    done
) &
echo "$(date -u +%FT%TZ) drift-guard: background loop started (PID $!)"
# ── End credential drift guard ────────────────────────────────────────────

# Write open-terminal shell config to /etc/profile.d so it applies to every
# user on every pod start — including multi-user provisioned accounts and
# after pod restarts where the PVC already contains a .bashrc.
# GH_TOKEN / GITHUB_TOKEN are read dynamically from /tmp/github_token so they
# always reflect the latest refreshed token regardless of when the shell starts.
sudo tee /etc/profile.d/open-terminal.sh > /dev/null << 'EOF'
export GIT_PAGER=cat
export GIT_CONFIG_GLOBAL=/dev/null
export LESS=-RXF

# Resolve the current GitHub token from whichever path is available.
# /run/secrets/github_token is the K8s-managed file updated by the CronJob
# without a pod restart. /tmp/github_token is the fallback written by the
# container's own refresh loop.
_gh_token_file() {
    if [ -f /run/secrets/github_token ]; then
        echo /run/secrets/github_token
    elif [ -f /tmp/github_token ]; then
        echo /tmp/github_token
    fi
}

_current_gh_token() {
    local f
    f=$(_gh_token_file)
    [ -n "$f" ] && cat "$f"
}

TOKEN_FILE=$(_gh_token_file)
if [ -n "$TOKEN_FILE" ]; then
    export GH_TOKEN="$(cat "$TOKEN_FILE")"
    export GITHUB_TOKEN="$GH_TOKEN"
fi

# Wrap gh so long-running shells always read the current token from disk
# rather than the value frozen in GH_TOKEN at shell-start time.
gh() {
    GH_TOKEN="$(_current_gh_token)" command gh "$@"
}

verify_pr() {
    local branch=$1
    echo "Checking PR for branch: $branch"
    gh pr list --state open --search "branch:$branch" 2>/dev/null || echo "No open PRs found"
}

verify_push() {
    local branch=$1
    echo "Verifying push status..."
    git ls-remote --heads origin "$branch" 2>&1 | grep "$branch" && echo "✓ Branch pushed successfully" || echo "✗ Branch not found on remote"
}

# NOTE: setup_git_auth() was removed 2026-05-11. It embedded the current
# token into the git remote URL, which bypassed the GitHub-App credential
# helper installed at /usr/local/bin/git-credential-github-token and
# caused stale tokens to persist in .git/config across sessions. The
# helper handles auth correctly with rotating ghs_ installation tokens;
# no embedded URLs needed.
EOF

# Seed essential dotfiles when /home/user is bind-mounted empty.
# (Docker does not populate bind-mounts with image contents.)
# Custom shell config lives in /etc/profile.d/open-terminal.sh above.
if [ ! -f "$HOME/.bashrc" ]; then
    cp /etc/skel/.bashrc "$HOME/.bashrc" 2>/dev/null || true
fi
if [ ! -f "$HOME/.profile" ]; then
    cp /etc/skel/.profile "$HOME/.profile" 2>/dev/null || true
fi
if [ ! -f "$HOME/.kube/config" ]; then
    cp -r /etc/skel/.kube/ "$HOME/" 2>/dev/null || true
    sudo chown -R user:user "$HOME/.kube" 2>/dev/null || true
    chmod 700 "$HOME/.kube" 2>/dev/null || true
fi
mkdir -p "$HOME/.local/bin"

# Docker socket access — add user to the socket's group if mounted.
if [ -S /var/run/docker.sock ]; then
    SOCK_GID=$(stat -c '%g' /var/run/docker.sock)
    if ! getent group "$SOCK_GID" > /dev/null 2>&1; then
        sudo groupadd -g "$SOCK_GID" docker-host
    fi
    SOCK_GROUP=$(getent group "$SOCK_GID" | cut -d: -f1)
    if ! id -nG | grep -qw "$SOCK_GROUP"; then
        sudo usermod -aG "$SOCK_GROUP" user
        exec sg "$SOCK_GROUP" -c "exec $0 $*"
    fi
fi

# Auto-install system packages
if [ -n "${OPEN_TERMINAL_PACKAGES:-}" ]; then
    echo "Installing system packages: $OPEN_TERMINAL_PACKAGES"
    sudo apt-get update -qq && sudo apt-get install -y --no-install-recommends $OPEN_TERMINAL_PACKAGES
    sudo rm -rf /var/lib/apt/lists/*
fi

# Auto-install Python packages
if [ -n "${OPEN_TERMINAL_PIP_PACKAGES:-}" ]; then
    echo "Installing pip packages: $OPEN_TERMINAL_PIP_PACKAGES"
    if [ "${OPEN_TERMINAL_MULTI_USER:-false}" = "true" ]; then
        sudo pip install --no-cache-dir $OPEN_TERMINAL_PIP_PACKAGES
    else
        pip install --no-cache-dir $OPEN_TERMINAL_PIP_PACKAGES
    fi
fi

# Auto-install npm packages
if [ -n "${OPEN_TERMINAL_NPM_PACKAGES:-}" ]; then
    echo "Installing npm packages: $OPEN_TERMINAL_NPM_PACKAGES"
    if [ "${OPEN_TERMINAL_MULTI_USER:-false}" = "true" ]; then
        sudo npm install -g $OPEN_TERMINAL_NPM_PACKAGES
    else
        npm install -g $OPEN_TERMINAL_NPM_PACKAGES
    fi
fi

# -----------------------------------------------------------------------
# Network egress filtering via DNS whitelist + iptables + capability drop
# -----------------------------------------------------------------------
if [ "${OPEN_TERMINAL_ALLOWED_DOMAINS+set}" = "set" ]; then
    if ! command -v iptables &>/dev/null; then
        echo "WARNING: iptables not found — skipping egress firewall"

        if [ -f /app/helpers/bible_bridge.py ]; then
            echo "Starting bible bridge on port ${BRIDGE_PORT:-8765}..."
            (python3 /app/helpers/bible_bridge.py >> /tmp/bible_bridge.log 2>&1) &
            echo "Bible bridge PID: $!"
        fi

        exec open-terminal "$@"
    fi

    sudo iptables -F OUTPUT 2>/dev/null || true
    sudo iptables -A OUTPUT -o lo -j ACCEPT
    sudo iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

    if [ -z "$OPEN_TERMINAL_ALLOWED_DOMAINS" ]; then
        echo "Egress: blocking ALL outbound traffic"
        sudo iptables -A OUTPUT -j DROP
    else
        echo "Egress: DNS whitelist — $OPEN_TERMINAL_ALLOWED_DOMAINS"

        UPSTREAM_DNS=$(grep -m1 '^nameserver' /etc/resolv.conf | awk '{print $2}')
        UPSTREAM_DNS="${UPSTREAM_DNS:-1.1.1.1}"

        sudo ipset create allowed hash:ip -exist

        sudo mkdir -p /etc/dnsmasq.d
        {
            echo "no-resolv"
            echo "no-hosts"
            echo "listen-address=127.0.0.1"
            echo "port=53"
            echo "address=/#/"

            IFS=',' read -ra DOMAINS <<< "$OPEN_TERMINAL_ALLOWED_DOMAINS"
            for domain in "${DOMAINS[@]}"; do
                domain=$(echo "$domain" | xargs)
                [ -z "$domain" ] && continue
                domain="${domain#\*.}"
                echo "server=/${domain}/${UPSTREAM_DNS}"
                echo "ipset=/${domain}/allowed"
                echo "  ✓ ${domain} (+ subdomains)" >&2
            done
        } | sudo tee /etc/dnsmasq.d/egress.conf > /dev/null

        sudo dnsmasq --conf-file=/etc/dnsmasq.d/egress.conf
        echo "dnsmasq started (upstream: ${UPSTREAM_DNS})"

        echo "nameserver 127.0.0.1" | sudo tee /etc/resolv.conf > /dev/null

        sudo iptables -A OUTPUT -p udp --dport 53 -j DROP
        sudo iptables -A OUTPUT -p tcp --dport 53 -j DROP
        sudo iptables -A OUTPUT -m set --match-set allowed dst -j ACCEPT
        sudo iptables -A OUTPUT -j DROP
    fi

    echo "Egress firewall active — dropping CAP_NET_ADMIN permanently"

    if [ -f /app/helpers/bible_bridge.py ]; then
        echo "Starting bible bridge on port ${BRIDGE_PORT:-8765}..."
        (python3 /app/helpers/bible_bridge.py >> /tmp/bible_bridge.log 2>&1) &
        echo "Bible bridge PID: $!"
    fi

    exec capsh --drop=cap_net_admin -- -c "exec open-terminal $*"
fi

# ── Bible bridge ──────────────────────────────────────────────────────────────
if [ -f /app/helpers/bible_bridge.py ]; then
    echo "Starting bible bridge on port ${BRIDGE_PORT:-8765}..."
    (python3 /app/helpers/bible_bridge.py >> /tmp/bible_bridge.log 2>&1) &
    echo "Bible bridge PID: $!"
fi

exec open-terminal "$@"