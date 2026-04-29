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

# Fix permissions of the home directory if the user doesn't own it
OWNER=$(stat -c '%U' /home/user 2>/dev/null || echo "user")

if [ "$OWNER" != "user" ]; then
    sudo chown -R user:user /home/user 2>/dev/null || true
fi

# add helper functions to the user's shell environment for PR verification and git auth setup
cp -r /app/helpers/. "$HOME/" 2>/dev/null || true

# Seed essential dotfiles when /home/user is bind-mounted empty
# (Docker does not populate bind-mounts with image contents)
if [ ! -f "$HOME/.bashrc" ]; then
    cp /etc/skel/.bashrc "$HOME/.bashrc" 2>/dev/null || true
    # Append runtime environment settings to .bashrc
    # NOTE: GH_TOKEN / GITHUB_TOKEN are read dynamically from /tmp/github_token
    # at each shell startup so they always reflect the latest refreshed token,
    # rather than capturing the value that was current when entrypoint ran.
    cat >> "$HOME/.bashrc" << 'EOF'

    # Disable git pager for consistent output
    export GIT_PAGER=cat
    export GIT_CONFIG_GLOBAL=/dev/null

    # Load the latest GitHub App installation token (refreshed every 50 min by entrypoint)
    if [ -f /tmp/github_token ]; then
        export GH_TOKEN="$(cat /tmp/github_token)"
        export GITHUB_TOKEN="$GH_TOKEN"
    fi

    # Ensure full output from commands
    export LESS=-RXF

    # Helper function to verify PR creation
    verify_pr() {
        local branch=$1
        echo "Checking PR for branch: $branch"
        gh pr list --state open --search "branch:$branch" 2>/dev/null || echo "No open PRs found"
    }

    # Helper function to verify push
    verify_push() {
        local branch=$1
        echo "Verifying push status..."
        git ls-remote --heads origin "$branch" 2>&1 | grep "$branch" && echo "✓ Branch pushed successfully" || echo "✗ Branch not found on remote"
    }

    # Helper function to set up authenticated git remote (if not already done)
    # Reads the freshest token from /tmp/github_token rather than the exported env var
    # so it works correctly even after a background refresh has occurred.
    setup_git_auth() {
        local token
        token="${1:-$(cat /tmp/github_token 2>/dev/null)}"
        if [ -z "$token" ]; then
            echo "No GitHub token available" >&2
            return 1
        fi
        local current_url
        current_url=$(git remote get-url origin 2>/dev/null) || { echo "No git remote 'origin' found"; return 1; }
        local new_url
        new_url=$(echo "$current_url" | sed "s|https://|https://x-access-token:${token}@|")
        git remote set-url origin "$new_url" 2>/dev/null && echo "✓ Remote URL updated with auth" || echo "Note: Remote may already be configured"
    }
EOF
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