#!/bin/bash
# Move the `myplow` pin to one release. Every cloud MyPlow follows that pin in place (update.py), so
# the pin moves only after the release passes three checks here, on throwaway "VMs" booted from the
# real images, with fakeplow.py standing in for Plow and the login server (no line, no phone):
#   in place  boot the CURRENT pin; its Boss makes a card from a text; add an OS-level tool and a home
#             file; move the pin: it must switch, pass its own health check, and keep the card, the
#             handled text, the tool, the home file and the Boss's session -- and still know the card
#   fresh     boot the release on an empty disk: healthy
#   rollback  pin a copy of the release whose chat bridge cannot start: it goes back, healthy
# Then `plow-agents image promote myplow` and read the pin back. Any failure: nothing is promoted.
#
# Runs on a Linux docker host, the way `make cloud` starts it: the ECR push login arrives on stdin.
#   publish.sh <release>     CLAUDE_LOGIN_FILE=<the Claude login the gate's Boss runs on>
set -euo pipefail
V="${1:?usage: publish.sh <release>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGIN="${CLAUDE_LOGIN_FILE:?set CLAUDE_LOGIN_FILE to the Claude login file the gate VM runs on}"
SRC="ghcr.io/delattre1/mypeople:cloud-$V"
ECR="${MYPLOW_ECR:-public.ecr.aws/e1h7x4a2/myplow-cloud}"
PA="${PLOW_AGENTS:-$HOME/plow-projects/plow-agents/bin/plow-agents}"
G=myplow-gate
say(){ echo "== $(date -u +%T) $*"; }
fail(){ echo "GATE FAILED, nothing promoted: $*" >&2; exit 1; }
cleanup(){ docker rm -f $G-vm $G-plow $G-registry >/dev/null 2>&1 || true
           docker network rm $G >/dev/null 2>&1 || true; rm -rf "${CFG:-/nonexistent}"; }
trap cleanup EXIT
pinned(){ "$PA" image show myplow | python3 -c 'import json,sys; print(json.load(sys.stdin)["plow"]["image"] or "")'; }

ECRPW="$(cat)"
[ -n "$ECRPW" ] || fail "no ECR login on stdin"
say "copy $SRC -> $ECR:$V"
docker pull -q --platform linux/amd64 "$SRC" >/dev/null
[ "$(docker inspect -f '{{index .Config.Labels "myplow.release"}}' "$SRC")" = "$V" ] || fail "$SRC is not release $V"
CFG="$(mktemp -d)"
printf '%s' "$ECRPW" | DOCKER_CONFIG="$CFG" docker login -u AWS --password-stdin public.ecr.aws >/dev/null
docker tag "$SRC" "$ECR:$V"
DOCKER_CONFIG="$CFG" docker push -q "$ECR:$V" >/dev/null
NEW="$ECR@$(docker inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$ECR:$V" | sed -n "s|^$ECR@||p" | head -1)"
OLD="$(pinned)"
say "release $V = $NEW; pin now $OLD"
[ "$NEW" != "$OLD" ] || { say "already the pin"; exit 0; }
[ -n "$OLD" ] || fail "myplow has no pin to update from"

# --- the rig: a registry (gate 3's broken copy), fakeplow, one VM at a time ---
cleanup; docker network create $G >/dev/null
docker run -d --name $G-registry --network $G -p 127.0.0.1::5000 registry:2 >/dev/null
docker run -d --name $G-plow --network $G -p 127.0.0.1::8080 -v "$HERE/fakeplow.py:/fakeplow.py:ro" \
  -v "$LOGIN:/secret/claude-setup-token:ro" python:3.12-slim python3 -u /fakeplow.py >/dev/null
sleep 3
FP="http://$(docker port $G-plow 8080 | head -1)"; REG="$(docker port $G-registry 5000 | head -1)"
pin(){ curl -fsS -X POST -d "{\"image\": \"$1\"}" "$FP/rig/pin" >/dev/null; }
text(){ python3 -c 'import json,sys; print(json.dumps({"body": sys.argv[1]}))' "$1" \
          | curl -fsS -X POST -d @- "$FP/rig/inbound" | python3 -c 'import json,sys; print(json.load(sys.stdin)["uid"])'; }
replied(){ curl -fsS "$FP/rig/messages" | python3 -c '
import json, sys
ms, after, word = json.load(sys.stdin)["data"], sys.argv[1], sys.argv[2].lower()
i = next((k for k, m in enumerate(ms) if m["uid"] == after), None)
sys.exit(0 if i is not None and any(m["direction"] == "outbound" and word in m["body"].lower() for m in ms[i + 1:]) else 1)' "$1" "$2"; }
beacon(){ curl -fsS "$FP/rig/beacons" | python3 -c '
import json, sys
sys.exit(0 if any(b["at"] >= sys.argv[1] and sys.argv[2] in b["line"] for b in json.load(sys.stdin)["data"]) else 1)' "$1" "$2"; }
within(){ local end=$((SECONDS + $1)); shift; until "$@"; do [ $SECONDS -lt $end ] || return 1; sleep 5; done; }
boot(){   # boot <image> <scheme its updates pull with>
  docker rm -f $G-vm >/dev/null 2>&1 || true
  docker run -d --name $G-vm --hostname $G --network $G --user root \
    -e PLOW_API_BASE=http://$G-plow:8080 -e MYPLOW_CLAUDE_BANK=http://$G-plow:8080/claude-bank \
    -e MYPLOW_SLUG=myplow -e MYPLOW_REGISTRY_SCHEME="$2" \
    -e MYPLOW_UPDATE_FIRST=20 -e MYPLOW_UPDATE_EVERY=20 -e MYPLOW_HEALTH_WAIT=300 "$1" >/dev/null; }
vm(){ docker exec -i $G-vm setpriv --reuid=mypeople --regid=mypeople --init-groups env HOME=/home/mypeople bash -s; }
# The same "healthy" entrypoint.sh switches on: board answers, chat bridge up 10s, Boss logged in.
healthy(){ vm >/dev/null 2>&1 <<'EOF'
set -e
curl -fsS -m 3 http://127.0.0.1:9933/health >/dev/null
p=$(pgrep -o -f "plugins/plow-chat/plow-chat.py serve"); [ "$(ps -o etimes= -p "$p" | tr -d ' ')" -ge 10 ]
tmux list-panes -a -F '#{window_name} #{pane_current_command}' | grep -q '^Boss claude'
! tmux capture-pane -p -t mc-main:Boss | grep -q 'Not logged in'
EOF
}
release(){ vm <<< 'readlink /opt/myplow/current'; }
state(){ vm <<EOF
D=/var/lib/mypeople
echo "cards \$(python3 -c "import sqlite3; print(sqlite3.connect('file:\$D/todos/board.v2.sqlite3?mode=ro', uri=True).execute('select count(*) from tasks').fetchone()[0])")"
echo "boss  \$(python3 -c "import json; print(json.load(open('\$D/status/mc-main/Boss.json')).get('session_id'))")"
echo "seen  \$(python3 -c "import json; print('$1' in json.load(open('\$D/state/plow-chat/state.json')).get('seen_ids', []))")"
echo "tool  \$(myplow-gate-tool 2>&1)"
echo "home  \$(cat ~/.myplow-gate 2>&1)"
EOF
}
verdict(){ beacon "$1" "update: healthy on" || beacon "$1" "ROLLED BACK"; }

say "gate 1/3 in place: boot the current pin"
pin "$OLD"; boot "$OLD" https
within 900 healthy || fail "the current pin never came up healthy (a rig problem, not $V)"
docker exec $G-vm sh -c 'printf "#!/bin/sh\necho kept\n" > /usr/local/bin/myplow-gate-tool && chmod 755 /usr/local/bin/myplow-gate-tool'
vm <<< 'echo kept > ~/.myplow-gate'
SEED="$(text "Gate test: put one card on your board titled GATE-HERON, then reply with just its title.")"
within 900 replied "$SEED" GATE-HERON || fail "the current pin's Boss never answered (a rig problem, not $V)"
before="$(state "$SEED")"; echo "$before"
T="$(date -u +%FT%T)"; pin "$NEW"; say "pin -> $V"
within 1800 verdict "$T" || fail "in place: no switch to $V within 30 min"
beacon "$T" "healthy on releases/$V" || fail "in place: $V failed its health check and rolled back"
[ "$(release)" = "releases/$V" ] || fail "in place: running $(release)"
after="$(state "$SEED")"; echo "$after"
[ "$after" = "$before" ] || fail "in place: the data changed across the switch"
Q="$(text "Gate test after an update: what title did the card you made have? Reply with just the title.")"
within 900 replied "$Q" GATE-HERON || fail "in place: the Boss no longer knows its card"
say "gate 1/3 passed"

say "gate 2/3 fresh: boot $V on an empty disk"
boot "$NEW" http   # http: gate 3's broken copy comes from the local registry
within 900 healthy || fail "fresh: $V never came up healthy"
[ "$(release)" = "releases/$V" ] || fail "fresh: running $(release)"
say "gate 2/3 passed"

say "gate 3/3 rollback: pin a copy of $V whose chat bridge cannot start"
docker build -q -t "$REG/myplow-cloud:$V-broken" --build-arg BASE="$NEW" --build-arg V="$V" - >/dev/null <<'EOF'
ARG BASE
FROM $BASE
ARG V
USER root
RUN R=/opt/myplow/releases && mv "$R/$V" "$R/$V-broken" && echo "$V-broken" > "$R/$V-broken/RELEASE" \
 && echo 'def broken(:' >> "$R/$V-broken/py/mypeople/runtime/plugins/plow-chat/plow-chat.py" \
 && ln -sfn "releases/$V-broken" /opt/myplow/current \
 && chown -hR mypeople:mypeople "$R/$V-broken" /opt/myplow/current
USER mypeople
LABEL myplow.release=$V-broken
EOF
docker push -q "$REG/myplow-cloud:$V-broken" >/dev/null
BROKEN="$G-registry:5000/myplow-cloud@$(docker inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$REG/myplow-cloud:$V-broken" | sed -n "s|^$REG/myplow-cloud@||p" | head -1)"
T="$(date -u +%FT%T)"; pin "$BROKEN"
within 1800 verdict "$T" || fail "rollback: no verdict on the broken copy within 30 min"
beacon "$T" "ROLLED BACK" || fail "rollback: the broken copy passed its health check"
within 900 healthy || fail "rollback: not healthy after going back"
[ "$(release)" = "releases/$V" ] || fail "rollback: running $(release)"
vm <<< "grep -qF '$BROKEN' /var/lib/mypeople/run/myplow-update-bad" || fail "rollback: the broken copy is not marked bad"
say "gate 3/3 passed"

say "promote myplow -> $NEW"
"$PA" image promote myplow "$NEW" >/dev/null
[ "$(pinned)" = "$NEW" ] || fail "the pin did not read back as $NEW"
say "pinned $V: every cloud MyPlow switches to it in place on its next idle check (5 min)"
