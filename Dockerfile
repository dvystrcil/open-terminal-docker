# apt reads a pinned Debian snapshot, never the live Debian mirror
# (homelab#1981, D-051; design and tests: dvystrcil/homelab
# architecture/apt-snapshot-image-builds.md; copied from
# dvystrcil/mealie-mcp-docker#56). A mirror mid-sync can't fail a build, and the
# Debian package set depends on APT_SNAPSHOT, not on the minute the build ran.
#   - The pin is the repository itself: debian.sources' URIs point at
#     <archive>/debian/<APT_SNAPSHOT>/ and <archive>/debian-security/<APT_SNAPSHOT>/
#     on snapshot.debian.org. Not `--snapshot` or `Snapshot:`: those still fetch
#     the live mirror alongside the snapshot (the doc's trap 1).
#   - Every `apt-get update` in this file is `--error-on=any -o Acquire::Retries=3`.
#     Without --error-on=any an unreachable source is a warning, apt keeps going,
#     and the error surfaces later as "Unable to locate package", or not at all.
#     The vendor scripts (nodesource setup_22.x, get.docker.com) run their own
#     quiet `apt-get update`; the nodesource step re-runs it strictly before
#     installing, and get.docker.com's install fails on its own if an index is
#     missing (the lists are emptied after every apt step).
#   - Third-party apt repos stay LIVE (D-051): nodesource, download.docker.com,
#     pkgs.k8s.io, cli.github.com, apt.releases.hashicorp.com. Only Debian's own
#     sources are pinned, and only Debian hosts are checked for live lines.
#   - APT_SNAPSHOT is written once, here. The weekly bump job
#     (homelab bin/bump-apt-snapshot.py) edits this line.
#   - APT_SNAPSHOT_BASE is the `debian` repo URL, without the ID. The bump job
#     probes it, so it must stay exactly https://snapshot.debian.org/archive/debian.
#     `debian-security` is its sibling directory. In-cluster CI passes the
#     apt-cacher-ng form,
#     http://apt-cacher-ng.arc-runner.svc.cluster.local:3142/HTTPS///snapshot.debian.org/archive/debian.
#     The URL names the cache, so if the cache is down the build fails; nothing
#     falls back to the live mirror or to the snapshot direct.
#   - Check-Valid-Until: no, on both stanzas (the doc's trap 4). Debian's
#     trixie-security AND trixie-updates InRelease carry Valid-Until = Date + 7
#     days (read from snapshot.debian.org 2026-11-19), so without it a pin older
#     than a week fails apt's replay check. This switches off only that expiry
#     check (sources.list(5)); apt still verifies every index against Debian's
#     archive key (Signed-By), and a snapshot is old data on purpose.
#   - No TLS bootstrap: python:3.15.2 already ships ca-certificates, so the
#     direct https:// snapshot works from the first fetch.
#   - Pinned at build time ONLY. Stage 1 backs up the base image's own
#     debian.sources before the rewrite; stage 2 (the final image) restores it
#     after its last apt install. The shipped image never names the CI-only
#     apt-cacher-ng URL, and runtime apt (OPEN_TERMINAL_PACKAGES, sudo users)
#     reads the live Debian mirror exactly as before the pin.
#   - tests/apt-snapshot-test.sh proves it builds with the live mirror blocked and
#     fails clearly with the snapshot source blocked.
ARG APT_SNAPSHOT=20261119T000000Z
ARG APT_SNAPSHOT_BASE=https://snapshot.debian.org/archive/debian

# === STAGE 1: build open_terminal from our fork, not upstream :latest ===
#
# TEMPORARY. dvystrcil/open-terminal-app-fork carries fixes not yet in
# upstream open-webui/open-terminal, submitted as:
#   - open-webui/open-terminal#148 -- configurable uvicorn keep-alive
#     timeout (fixes intermittent ConnectionResetError)
#   - open-webui/open-terminal#149 -- process-log retention security fix
#     (a log file with no in-memory record was never pruned)
#   - open-webui/open-terminal#150 -- two-tier process-result expiry
#     (a slow caller could lose a finished command's result forever)
#   - open-webui/open-terminal#151 -- insert_after/append_to_section/
#     append endpoints + a defensive replace_file_content check
# Plus one homelab-specific commit NOT submitted upstream (GH_TOKEN
# refresh from disk before every subprocess spawn -- ties into our own
# entrypoint.sh token-rotation convention, not something upstream has
# any hook for).
#
# Once all four upstream PRs merge and a release picks them up, revert
# this stage and go back to a plain
# `FROM harbor-core.../ghcr-proxy/open-webui/open-terminal:latest`
# (see homelab#822). Mirrors upstream's own Dockerfile build steps
# exactly, substituting a git clone of our fork for `COPY . .`.
FROM python:3.15.2 AS fork-build
ARG APT_SNAPSHOT
ARG APT_SNAPSHOT_BASE

# Point apt at the snapshot (see the top of this file) and refuse to go on if
# any live Debian source is left. The base must end in /debian: debian-security
# is derived from it.
RUN archive="${APT_SNAPSHOT_BASE%/debian}" && \
    test "${archive}/debian" = "${APT_SNAPSHOT_BASE}" && \
    mkdir -p /usr/local/share/apt-snapshot-pin && \
    cp /etc/apt/sources.list.d/debian.sources /usr/local/share/apt-snapshot-pin/debian.sources.orig && \
    sed -i -E \
      -e "s|^URIs: http://deb\.debian\.org/debian-security/?$|URIs: ${archive}/debian-security/${APT_SNAPSHOT}/\nCheck-Valid-Until: no|" \
      -e "s|^URIs: http://deb\.debian\.org/debian/?$|URIs: ${archive}/debian/${APT_SNAPSHOT}/\nCheck-Valid-Until: no|" \
      /etc/apt/sources.list.d/debian.sources && \
    test "$(grep -c "^URIs: ${archive}/debian\(-security\)\?/${APT_SNAPSHOT}/$" /etc/apt/sources.list.d/debian.sources)" -eq 2 && \
    ! grep -rsE '^ *(deb |URIs:).*(deb|security)\.debian\.org' /etc/apt/sources.list /etc/apt/sources.list.d/ && \
    apt-get update --error-on=any -o Acquire::Retries=3 && apt-get install -y --no-install-recommends \
        coreutils findutils grep sed gawk diffutils patch \
        less file tree bc man-db \
        curl wget net-tools iputils-ping dnsutils netcat-openbsd socat telnet \
        openssh-client rsync \
        vim nano \
        git \
        build-essential cmake make \
        perl ruby-full lua5.4 \
        jq xmlstarlet sqlite3 \
        ffmpeg pandoc imagemagick texlive-latex-base \
        zip unzip tar gzip bzip2 xz-utils zstd p7zip-full \
        procps htop lsof strace sysstat \
        sudo tmux screen tini iptables ipset dnsmasq \
        ca-certificates gnupg apt-transport-https \
        libcap2-bin \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get update --error-on=any -o Acquire::Retries=3 \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://get.docker.com | sh

WORKDIR /app

RUN pip install --no-cache-dir \
    numpy pandas scipy scikit-learn \
    matplotlib seaborn plotly \
    jupyter ipython \
    requests beautifulsoup4 lxml \
    sqlalchemy psycopg2-binary \
    pyyaml toml jsonlines \
    tqdm rich \
    openpyxl weasyprint \
    python-docx python-pptx pypdf csvkit

# git clone stands in for upstream's `COPY . .` -- our source lives in a
# separate fork repo, not this one. FORK_SHA exists purely to bust
# Docker's build cache: `git clone --branch main` is byte-identical
# text on every build regardless of what commit main actually points
# to, so without something that changes per-build in this RUN step,
# a cached layer silently ships a stale fork clone forever. CI (see
# docker.yml) resolves the fork's current SHA via `git ls-remote` and
# passes it explicitly on every run.
ARG FORK_REF=main
ARG FORK_SHA=""
RUN echo "Building open-terminal-app-fork ref=${FORK_REF} sha=${FORK_SHA:-unpinned}" \
    && git clone --branch "${FORK_REF}" --depth 1 \
        https://github.com/dvystrcil/open-terminal-app-fork.git /build \
    && cd /build \
    && pip install --no-cache-dir . \
    && cp "$(readlink -f "$(which python3)")" /usr/local/bin/python3-ot \
    && setcap cap_setgid+ep /usr/local/bin/python3-ot \
    && sed -i "1s|.*|#!/usr/local/bin/python3-ot|" "$(which open-terminal)" \
    && rm -rf /build

RUN useradd -m -s /bin/bash user && echo 'user ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Matches upstream's own Dockerfile tail exactly -- these are image
# metadata (ENV/WORKDIR/EXPOSE), not filesystem content, so without
# restating them here stage 2 (FROM fork-build) would silently lose
# them. The old single-stage setup got these for free by inheriting
# straight from the pre-built upstream image; building from source
# here means restating what upstream's own Dockerfile sets.
ENV SHELL=/bin/bash
ENV PATH="/home/user/.local/bin:${PATH}"
WORKDIR /home/user
EXPOSE 8000

# === STAGE 2: homelab wrapper -- tools + entrypoint on top of stage 1 ===
FROM fork-build

USER root

# === CONSOLIDATED APT INSTALLATIONS (SINGLE LAYER) ===
# This dramatically reduces layers and improves caching.
# This is the last apt step that reads the snapshot pin, so it ends by
# restoring the base image's live debian.sources (pinned at build time only,
# see the top of this file).
RUN apt-get update --error-on=any -o Acquire::Retries=3 && apt-get install -y --no-install-recommends \
        ca-certificates \
        apt-transport-https \
        gnupg \
    && KUBE_VER=v1.34 \
    && curl -fsSL "https://pkgs.k8s.io/core:/stable:/${KUBE_VER}/deb/Release.key" \
        | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/${KUBE_VER}/deb/ /" \
        | tee /etc/apt/sources.list.d/kubernetes.list \
    && curl -fsSLo /usr/share/keyrings/githubcli-archive-keyring.gpg \
        https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && printf 'Types: deb\nURIs: https://cli.github.com/packages\nSuites: stable\nComponents: main\nArchitectures: %s\nSigned-By: /usr/share/keyrings/githubcli-archive-keyring.gpg\n' \
        "$(dpkg --print-architecture)" \
        | tee /etc/apt/sources.list.d/github-cli.sources \
    && curl -fsSL https://apt.releases.hashicorp.com/gpg \
        | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(. /etc/os-release && echo $VERSION_CODENAME) main" \
        | tee /etc/apt/sources.list.d/hashicorp.list \
    && apt-get update --error-on=any -o Acquire::Retries=3 \
    && apt-get install -y --no-install-recommends \
        kubectl \
        gh \
        ripgrep \
        fd-find \
        bat \
        tmux \
        sqlite3 \
        httpie \
        tree \
        htop \
        pigz \
        unar \
        rsync \
        zip \
        unzip \
        diffutils \
        jq \
        redis-tools \
        postgresql-client \
        ansible \
        gnupg2 \
        terraform \
        pandoc \
    && rm -rf /var/lib/apt/lists/* \
    && cp /usr/local/share/apt-snapshot-pin/debian.sources.orig /etc/apt/sources.list.d/debian.sources \
    && rm -rf /usr/local/share/apt-snapshot-pin

RUN pip3 install --no-cache-dir PyJWT cryptography --break-system-packages    

# ACT — run GitHub Actions workflows locally
RUN curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/nektos/act/master/install.sh | bash -s -- -b /usr/local/bin

# ArgoCD CLI
RUN ARCH=$(dpkg --print-architecture) \
    && VERSION=$(curl -fsSL https://raw.githubusercontent.com/argoproj/argo-cd/stable/VERSION) \
    && curl -fsSL -o /tmp/argocd \
        "https://github.com/argoproj/argo-cd/releases/download/v${VERSION}/argocd-linux-${ARCH}" \
    && install -m 555 /tmp/argocd /usr/local/bin/argocd \
    && rm -rf /tmp/argocd /tmp/*

# YQ — YAML processor
ARG YQ_VERSION=v4.52.5
RUN ARCH=$(dpkg --print-architecture) \
    && curl -sfL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${ARCH}" -o yq \
    && curl -sfL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/checksums" -o checksums \
    && grep "^yq_linux_${ARCH} " checksums | awk '{print $19 "  yq"}' | sha256sum -c - \
    && rm -f checksums \
    && chmod +x yq \
    && cp yq /usr/local/bin/yq \
    && rm -rf /tmp/*

# Helm — official binary install
# RUN ARCH=$(dpkg --print-architecture) \
#     && VERSION=$(curl -fsSL https://api.github.com/repos/helm/helm/releases/latest | grep '"tag_name"' | cut -d'"' -f4) \
#     && curl -fsSL "https://get.helm.sh/helm-${VERSION}-linux-${ARCH}.tar.gz" | tar -xz \
#     && install -m 555 "linux-${ARCH}/helm" /usr/local/bin/helm \
#     && rm -rf "linux-${ARCH}" /tmp/*


# Pre-configure kubectl for in-cluster serviceaccount
RUN mkdir -p /etc/skel/.kube && \
    cat > /etc/skel/.kube/config << 'EOF'
apiVersion: v1
kind: Config
clusters:
- cluster:
    server: https://kubernetes.default.svc
    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
  name: in-cluster
contexts:
- context:
    cluster: in-cluster
    user: open-terminal
  name: default
current-context: default
users:
- name: open-terminal
  user:
    tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
EOF

# Apply security patches on top of the upstream base image
RUN apt-get upgrade -y && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Custom entrypoint and helper scripts
COPY entrypoint.sh /app/entrypoint.sh
COPY helpers/ /app/helpers/
RUN chmod +x /app/entrypoint.sh

# Make GH_TOKEN reach NON-interactive sandbox shells. The command executor runs
# agent commands as `sudo -u <user> -- bash -c ...` — a non-login shell that
# never sources /etc/profile.d, and sudo strips the parent env. BASH_ENV makes
# bash source our profile (GH_TOKEN/GITHUB_TOKEN export + gh() wrapper) on every
# non-interactive start; the sudoers drop-in lets BASH_ENV survive `sudo -u`.
# entrypoint.sh writes the profile at runtime, so the path is valid by the time
# any command runs (bash tolerates an absent BASH_ENV file regardless). See #47.
ENV BASH_ENV=/etc/profile.d/open-terminal.sh
COPY sudoers.d/open-terminal-env /etc/sudoers.d/open-terminal-env
RUN chmod 0440 /etc/sudoers.d/open-terminal-env \
    && visudo -cf /etc/sudoers.d/open-terminal-env

USER user

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["run"]
