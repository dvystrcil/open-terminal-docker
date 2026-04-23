# open-terminal-docker

A downstream wrapper image around [`ghcr.io/open-webui/open-terminal`](https://github.com/open-webui/open-terminal) that turns the upstream "remote terminal API" into a batteries-included **DevOps / SRE / writing workbench** for AI agents.

The upstream image gives an agent a sandboxed shell with a REST API. This image keeps everything upstream provides and layers on the tools, configuration, and side-services we actually use day to day — so a fresh container is immediately useful for managing Kubernetes clusters, opening GitHub PRs, running infrastructure-as-code, and acting as a writable backend for the fiction-writing pipeline.

## What this image adds on top of upstream

### Pre-installed tooling

Baked into the image (single consolidated apt layer plus a few binary installs):

- **Kubernetes**: `kubectl` (v1.34 channel), `helm`, `argocd`
- **GitHub / git**: `gh` CLI, plus the upstream `git`
- **Infrastructure**: `terraform`, `ansible`, `act` (run GitHub Actions locally)
- **Data / files**: `yq`, `jq`, `pandoc`, `sqlite3`, `redis-tools`, `postgresql-client`
- **Productivity**: `ripgrep`, `fd-find`, `bat`, `tmux`, `tree`, `htop`, `httpie`
- **Archives / transfer**: `pigz`, `unar`, `rsync`, `zip`, `unzip`, `diffutils`
- **Crypto**: `gnupg2`

A final `apt-get upgrade` is run on top of the upstream base so security patches travel with each rebuild.

### Pre-wired in-cluster `kubectl`

`/etc/skel/.kube/config` ships a context (`in-cluster` / user `open-terminal`) that points at `kubernetes.default.svc` and reads the pod's serviceaccount token from `/var/run/secrets/kubernetes.io/serviceaccount/`. When the container runs in a Kubernetes pod with a serviceaccount mounted, `kubectl` works with no extra setup. New users provisioned by the upstream multi-user mode inherit this via skel.

### Custom entrypoint

[entrypoint.sh](entrypoint.sh) extends upstream behaviour with:

- **Docker-secrets style env vars** — any `<VAR>_FILE` is resolved into `<VAR>` (matching the official PostgreSQL image convention). Currently applied to `OPEN_TERMINAL_API_KEY`, so you can mount the API key as a file instead of passing it on the command line.
- **Home-directory ownership repair** — `chown`s `/home/user` back to `user` when a bind-mounted volume comes in owned by someone else.
- **Dotfile seeding for empty bind mounts** — copies `/etc/skel/.bashrc`, `.profile`, and `.kube/` into a freshly mounted home so the shell is usable immediately. Docker doesn't populate bind-mounts from the image, this fills that gap.
- **Shell helpers** appended to the seeded `.bashrc`:
  - `GIT_PAGER=cat`, `GIT_CONFIG_GLOBAL=/dev/null`, `LESS=-RXF` for non-interactive friendly output
  - `GH_TOKEN` exported into the shell so `gh` is authenticated out of the box
  - `verify_pr <branch>` — list open PRs for a branch
  - `verify_push <branch>` — confirm a branch reached the remote
  - `setup_git_auth <token>` — rewrite the `origin` URL with an `x-access-token` credential
- **Docker socket group fixup** — when `/var/run/docker.sock` is mounted, the entrypoint discovers the socket's GID, creates a matching group if needed, adds `user` to it, and re-execs through `sg` so the new group membership is live without a re-login.
- **Runtime package install hooks** — preserves and respects the upstream `OPEN_TERMINAL_PACKAGES` (apt) and `OPEN_TERMINAL_PIP_PACKAGES` (pip), and adds `OPEN_TERMINAL_NPM_PACKAGES` (npm). Multi-user mode installs pip/npm globally via `sudo` so every provisioned user shares them.
- **Network egress firewall** — passes through the upstream `OPEN_TERMINAL_ALLOWED_DOMAINS` mechanism (DNS whitelist via dnsmasq + iptables + `ipset`, then `CAP_NET_ADMIN` is dropped via `capsh`). Behaviour:
  - unset → full egress
  - empty string → block all outbound
  - comma list → only those domains (and subdomains) resolve
- **Bible bridge launch** — starts `helpers/bible_bridge.py` in the background on `${BRIDGE_PORT:-8765}` (see below). It is started **before** the egress firewall drops `CAP_NET_ADMIN` so it can bind its port.

### Helpers shipped into `$HOME`

On startup, everything in `/app/helpers/` is copied into the user's home so it is reachable from interactive shells and from the agent.

- [helpers/create-pr.sh](helpers/create-pr.sh) — opinionated five-step PR workflow (`branch → add → commit → push → gh pr create`) with colorized progress and an open-PR verification step at the end. Usage:

  ```bash
  ~/create-pr.sh <branch> <commit-msg> <pr-title> <pr-body>
  ```

- [helpers/bible_bridge.py](helpers/bible_bridge.py) — small `http.server`-based HTTP bridge that exposes one or more git-cloned "story bible" projects under `BIBLE_ROOT` (default `/home/u3aa02715/fiction`). It exists so the **Fiction Writing Filter** running in a different (pipelines) pod can read and write bible files across namespace boundaries without sharing a volume. Endpoints:

  | Method | Path | Purpose |
  |---|---|---|
  | `GET`  | `/health` | bridge status + list of detected project repos |
  | `GET`  | `/version` | bridge version (currently `1.1`) |
  | `GET`  | `/projects` | list project names under `BIBLE_ROOT` |
  | `GET`  | `/bible?project=…&task_type=…` | read all `bible/*.md` (and `fragments/*.md` for `FRAGMENT` / `BIBLE_UPDATE` tasks) |
  | `POST` | `/bible/write` | write/append a file, then `git add`/`commit`/`push` |
  | `POST` | `/bible/pull` | `git pull --ff-only` on a project |
  | `POST` | `/bible/sync` | `git add -A` + commit + push everything dirty |
  | `POST` | `/bible/pr` | branch + commit + push + `gh pr create` |

  Path traversal into other directories is blocked via `realpath` containment under `BIBLE_ROOT`. Optional `BRIDGE_TOKEN` enables `Authorization: Bearer …` checks. Configurable with `BIBLE_ROOT`, `BRIDGE_PORT`, `BRIDGE_TOKEN`, `GIT_REMOTE`, `GIT_BRANCH`.

  The bridge is dormant if `BIBLE_ROOT` is empty — it just lists no projects — so the image is harmless to run for non-fiction workloads.

## Build and run

```bash
docker build -t open-terminal-docker .
docker run -d --name open-terminal \
  -p 8000:8000 \
  -v open-terminal:/home/user \
  -e OPEN_TERMINAL_API_KEY=your-secret-key \
  open-terminal-docker
```

The wrapper inherits the upstream `CMD ["run"]` and `ENTRYPOINT` chain (now `tini → entrypoint.sh → open-terminal`), so all upstream CLI flags and environment variables continue to work — see the [upstream README](https://github.com/open-webui/open-terminal) for the full configuration surface (config files, multi-user mode, MCP server, etc.).

### Useful environment variables introduced or exposed by this wrapper

| Variable | Effect |
|---|---|
| `OPEN_TERMINAL_API_KEY_FILE` | Read the API key from a file (Docker/Kubernetes secret friendly) |
| `OPEN_TERMINAL_NPM_PACKAGES` | Space-separated npm packages to install at startup |
| `GH_TOKEN` | Exported into the shell so `gh` is authenticated |
| `BIBLE_ROOT` | Parent directory of bible-bridge git repos (default `/home/u3aa02715/fiction`) |
| `BRIDGE_PORT` | Port the bible bridge listens on (default `8765`) |
| `BRIDGE_TOKEN` | Optional bearer token for the bible bridge |
| `GIT_REMOTE` / `GIT_BRANCH` | Defaults used by bible-bridge git operations (`origin` / `main`) |

Plus everything upstream exposes: `OPEN_TERMINAL_PACKAGES`, `OPEN_TERMINAL_PIP_PACKAGES`, `OPEN_TERMINAL_MULTI_USER`, `OPEN_TERMINAL_ALLOWED_DOMAINS`, etc.

## Repository layout

```
Dockerfile           # FROM ghcr.io/open-webui/open-terminal:latest + tooling
entrypoint.sh        # secrets resolution, dotfile seeding, helpers, egress, bridge
helpers/
  bible_bridge.py    # multi-project Story Bible HTTP bridge
  create-pr.sh       # five-step PR workflow
  CONTAINER_TEST_PLAN.md
open_terminal/       # vendored copy of the upstream Python package (reference)
dev.sh               # local dev: uv run uvicorn open_terminal.main:app --reload
```

> The `open_terminal/` source tree is checked in for reference and local debugging via [dev.sh](dev.sh). The published image runs the upstream `open-terminal` binary from the base image, **not** this local copy — to ship code changes you would need to either pin a custom upstream version or restructure the Dockerfile to install from this tree.

## License

MIT — see [LICENSE](LICENSE).
