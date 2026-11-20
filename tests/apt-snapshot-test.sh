#!/usr/bin/env bash
# Blocked-mirror build test for the apt snapshot pin (dvystrcil/homelab#1981, D-051).
#
# Builds the Dockerfile twice, with --no-cache so apt really runs:
#
#   1. mirror-blocked  deb.debian.org and security.debian.org resolve to
#                      127.0.0.1 inside every RUN step. The build must still
#                      succeed, apt must fetch both `debian` and
#                      `debian-security` from the snapshot source, and nothing
#                      from the live mirror. This image also adds third-party
#                      apt repos (nodesource, download.docker.com, pkgs.k8s.io,
#                      cli.github.com, hashicorp). They stay live (D-051), so
#                      their fetches are allowed, but only from the hosts in
#                      THIRD_PARTY_RE: any other host fails the case.
#   2. source-blocked  the snapshot source itself (the host in APT_SNAPSHOT_BASE:
#                      snapshot.debian.org, or the apt-cacher-ng Service in CI) is
#                      blocked the same way. The build must FAIL with apt's own
#                      `E: Failed to fetch`, without reaching the live mirror.
#
# Why --add-host: it is a build flag, so it reaches the RUN containers through the
# daemon on any runner. On the ARC DinD runner the daemon is a sidecar that can't
# see this container's files, so nothing here bind-mounts from the host.
#
#   APT_SNAPSHOT_BASE  passed as --build-arg when set (CI passes the cache URL).
#                      Unset = the Dockerfile's default, snapshot.debian.org direct.
#   DPKG_OUT           optional: write case 1's image `dpkg-query -W` here.
#
# Copied from dvystrcil/mealie-mcp-docker#56. Differences: IMG, and the
# THIRD_PARTY_RE allowlist (mealie-mcp has no third-party apt repos).
#
# Prints APT-SNAPSHOT-TEST-OK as its last line only if both cases held.
set -euo pipefail
cd "$(dirname "$0")/.."

IMG=open-terminal-apt-snapshot-test:ci
LOGDIR=$(mktemp -d)
trap 'rm -rf "$LOGDIR"' EXIT

args=()
if [ -n "${APT_SNAPSHOT_BASE:-}" ]; then
  args+=(--build-arg "APT_SNAPSHOT_BASE=${APT_SNAPSHOT_BASE}")
  base="$APT_SNAPSHOT_BASE"
else
  base=$(sed -n 's/^ARG APT_SNAPSHOT_BASE=//p' Dockerfile)
fi
src_host=$(printf '%s\n' "$base" | sed -E 's|^[a-z]+://([^/:]+).*|\1|')
[ -n "$src_host" ] || { echo "could not read a host from '$base'"; exit 1; }
echo "snapshot source: $base (host $src_host)"

build() {  # build <log> <extra args...>; returns docker's exit code
  local log=$1; shift
  set +e
  docker buildx build --builder default --no-cache --progress=plain \
    "${args[@]}" "$@" -t "$IMG" . >"$log" 2>&1
  local rc=$?
  set -e
  return $rc
}

live_re='(Get|Hit|Ign|Err):[0-9]+ [a-z]+://(deb|security)\.debian\.org'
# deb.debian.org and security.debian.org publish SRV records
# (_http._tcp.<host> -> debian.map.fastlydns.net), and apt follows them
# (Acquire::EnableSrvRecords defaults to true), so blocking the two names alone
# does NOT block the mirror: found 2026-11-19, apt fetched everything from Fastly.
# The SRV target is blocked too, and case 1 also requires that EVERY fetch came
# from the snapshot source, so a new SRV target can't make it pass quietly.
live=(--add-host deb.debian.org:127.0.0.1 --add-host security.debian.org:127.0.0.1
      --add-host debian.map.fastlydns.net:127.0.0.1)
# Third-party apt repos this Dockerfile adds. They are not pinned (D-051), so a
# fetch from them is fine; a fetch from any host not listed here, or from a
# Debian host other than the snapshot source, fails case 1.
THIRD_PARTY_RE='(deb\.nodesource\.com|download\.docker\.com|pkgs\.k8s\.io|prod-cdn\.packages\.k8s\.io|cli\.github\.com|apt\.releases\.hashicorp\.com)'
fail=0

echo "== case 1: live mirror blocked, snapshot reachable: must build"
if build "$LOGDIR/1.log" "${live[@]}"; then
  gets=$(grep -cE "Get:[0-9]+ [a-z]+://${src_host}[:/]" "$LOGDIR/1.log" || true)
  sec=$(grep -cE "Get:[0-9]+ [a-z]+://${src_host}[:/].*/debian-security/" "$LOGDIR/1.log" || true)
  others=$(grep -E "Get:[0-9]+ [a-z]+://" "$LOGDIR/1.log" \
    | grep -vE "Get:[0-9]+ [a-z]+://${src_host}[:/]" \
    | grep -cvE "Get:[0-9]+ [a-z]+://${THIRD_PARTY_RE}[:/]" || true)
  third=$(grep -cE "Get:[0-9]+ [a-z]+://${THIRD_PARTY_RE}[:/]" "$LOGDIR/1.log" || true)
  livegets=$(grep -cE "$live_re" "$LOGDIR/1.log" || true)
  echo "   fetches from $src_host: $gets (debian-security: $sec); third-party (live, allowed): $third; from anywhere else: $others; live-mirror lines: $livegets"
  if [ "$gets" -lt 1 ] || [ "$sec" -lt 1 ] || [ "$others" -ne 0 ] || [ "$livegets" -ne 0 ]; then
    echo "FAIL: case 1 built, but apt did not read Debian only from the snapshot (both repos)"; fail=1
    grep -E "Get:[0-9]+ [a-z]+://" "$LOGDIR/1.log" | grep -vE "Get:[0-9]+ [a-z]+://${src_host}[:/]" \
      | grep -vE "Get:[0-9]+ [a-z]+://${THIRD_PARTY_RE}[:/]" | head -10 || true
  else
    echo "   ok"
    [ -z "${DPKG_OUT:-}" ] || docker run --rm --entrypoint dpkg-query "$IMG" -W >"$DPKG_OUT"
  fi
else
  echo "FAIL: case 1 did not build with the live mirror blocked"; fail=1
  grep -E "^#[0-9]+ .*(E:|W:|Err:)" "$LOGDIR/1.log" | head -20 || true
  tail -n 20 "$LOGDIR/1.log"
fi

echo "== case 2: snapshot source blocked too: must fail clearly, no fallback"
if build "$LOGDIR/2.log" "${live[@]}" --add-host "${src_host}:127.0.0.1"; then
  echo "FAIL: case 2 built with its only apt source unreachable"; fail=1
else
  err=$(grep -E "E: Failed to fetch" "$LOGDIR/2.log" | head -3 || true)
  livegets=$(grep -cE "(Get|Hit):[0-9]+ [a-z]+://(deb|security)\.debian\.org" "$LOGDIR/2.log" || true)
  if [ -z "$err" ]; then
    echo "FAIL: case 2 failed, but not with apt's 'E: Failed to fetch'"; fail=1
    tail -n 20 "$LOGDIR/2.log"
  elif [ "$livegets" -ne 0 ]; then
    echo "FAIL: case 2 reached the live mirror"; fail=1
  else
    printf '   apt said: %s\n' "$err"
    echo "   ok"
  fi
fi

docker image rm -f "$IMG" >/dev/null 2>&1 || true
[ "$fail" -eq 0 ] || exit 1
echo APT-SNAPSHOT-TEST-OK
