#!/bin/sh
# Image-level check for the bible bridge's groups (open-terminal-docker#89 follow-up).
# Usage: bridge-groups-test.sh <image> <scratch-dir>
# Lays out /home like a Longhorn (local fs) volume after a restart: a 2770
# user home owned by 1001 with BIBLE_ROOT inside it, no passwd entry.
set -eu
IMG=$1; D=$2/bridge-home; NAME=ot-bridge-test
docker rm -f $NAME >/dev/null 2>&1 || true
docker run --rm --user 0 --entrypoint sh -v "$2":/s "$IMG" -c "rm -rf /s/bridge-home; mkdir -m 755 /s/bridge-home; install -d -o 1001 -g 1001 -m 2770 /s/bridge-home/u3aa02715 /s/bridge-home/u3aa02715/fiction /s/bridge-home/u3aa02715/fiction/proj"
docker run -d --name $NAME -e OPEN_TERMINAL_API_KEY=x -e OPEN_TERMINAL_MULTI_USER=true -e BRIDGE_TOKEN=t -v "$D":/home "$IMG" >/dev/null
for i in $(seq 1 30); do docker exec $NAME curl -sf -H "Authorization: Bearer t" localhost:8765/health >/dev/null 2>&1 && break; sleep 1; done
H=$(docker exec $NAME curl -s -H "Authorization: Bearer t" localhost:8765/health)
G=$(docker exec $NAME sh -c 'grep ^Groups /proc/$(pgrep -f "^python3 /app/helpers/bible_bridge.py" | tail -1)/status')
echo "health: $H"; echo "bridge $G"
docker rm -f $NAME >/dev/null
docker run --rm --user 0 --entrypoint sh -v "$2":/s "$IMG" -c "rm -rf /s/bridge-home"
echo "$H" | grep -q '"bible_root_exists": true' && echo BRIDGE-GROUPS-OK || { echo BRIDGE-GROUPS-FAIL; exit 1; }
