#!/bin/bash
# PID 1 of a MyPlow cloud agent, and the one file an update never replaces. It runs the release
# that /opt/myplow/current points at (releases/<release>/run.sh, as the mypeople user) and owns the
# only moment an update is dangerous: switching releases.
#
# update.py (inside the running release) stages the next release, waits until the agents are idle,
# writes run/myplow-update-request and stops the fleet. Then, here, with nothing running:
#   snapshot the data -> point current at the new release -> start it -> health-check it.
#   Healthy: done, same VM, same disk, same chats. Not healthy within HEALTH_WAIT: stop it, point
#   current back, restore the snapshot, never retry that image, and tell the owner in one line.
# Nothing here is release code, so a broken release cannot break its own rollback.
set -u
M=/opt/myplow
DATA=/var/lib/mypeople
RUN="$DATA/run"
REQUEST="$RUN/myplow-update-request"
PENDING="$RUN/myplow-update-pending"
BAD="$RUN/myplow-update-bad"
SNAPS="$DATA/snapshots"
HEALTH_WAIT="${MYPLOW_HEALTH_WAIT:-300}"
# The owner's login server: this install's own setting (env, or the file on its data disk). No default:
# an install without one runs on its own owner's Claude and sends no beacons.
BANK="${MYPLOW_CLAUDE_BANK:-$(cat "$DATA/state/claude-bank-url" 2>/dev/null)}"
# Boot beacons: one short step line to the owner's login server, in the background with a 5s cap,
# so the boot never waits on it. The only view into a Plow VM nobody can log into. No secrets.
beacon(){ [ -n "$BANK" ] || return 0; (curl -s -m 5 -X POST --data-binary "$(hostname 2>/dev/null) $*" "$BANK/beacon" >/dev/null 2>&1 &); }
beacon "start uid=$(id -u) current=$(readlink "$M/current" 2>/dev/null)"

# exe.dev writes the tenant environment here; normally it is also in ours, so this only fills gaps.
if [ -z "${PLOW_API_BASE:-}" ] && [ -r /exe.dev/etc/env ]; then set -a; . /exe.dev/etc/env; set +a; fi
# Booted as root (exe.dev): own the state, and run everything else as mypeople. Claude Code refuses
# to skip permission prompts as root, so the fleet must never run as root.
mkdir -p "$RUN" "$SNAPS"   # before the chown: a root-owned run/ stops the Boss from starting
if [ "$(id -u)" = 0 ]; then
  chown -R mypeople:mypeople "$DATA" /home/mypeople
  me(){ setpriv --reuid=mypeople --regid=mypeople --init-groups env HOME=/home/mypeople "$@"; }
else
  me(){ "$@"; }
fi

# What a snapshot holds: the board, the roster, plugin state (plow-chat's seen messages included)
# and the config -- what a new release could rewrite. Claude transcripts are append-only and stay
# put. ponytail: the snapshot is on this disk; a lost VM still loses everything (upgrade path: copy
# it off-VM, restore it on an empty disk).
SNAP_PATHS=(var/lib/mypeople/todos/board.v2.sqlite3 var/lib/mypeople/todos/board.v2.sqlite3-wal
            var/lib/mypeople/todos/board.v2.sqlite3-shm var/lib/mypeople/todos/board.v2.json
            var/lib/mypeople/run/roster.json var/lib/mypeople/state var/lib/mypeople/config)
snapshot(){
  local f="$SNAPS/$(date -u +%Y%m%dT%H%M%SZ)-$1.tgz" have=() p
  ls -1t "$SNAPS"/*.tgz 2>/dev/null | tail -n +3 | xargs -r rm -f   # this one + the last 2
  for p in "${SNAP_PATHS[@]}"; do [ -e "/$p" ] && have+=("$p"); done
  me tar -czf "$f" -C / "${have[@]}" && echo "$f"
}
restore(){
  # Remove first: a newer -wal left beside an older database would be replayed into it.
  local p; for p in "${SNAP_PATHS[@]}"; do me rm -rf "/$p"; done
  me tar -xzf "$1" -C /
}
point(){ ln -sfn "$1" "$M/current.new" && mv -Tf "$M/current.new" "$M/current"; }

stop_fleet(){
  me tmux kill-server >/dev/null 2>&1
  me env PATH="$M/current/bin:$PATH" PYTHONPATH="$M/current/py" mypeople down >/dev/null 2>&1
  # The release being stopped may be the broken one, so its own `down` is not trusted: anything
  # still running from the install dir (daemons, plugins) or from a release (its helpers) goes too,
  # or the next release's check-before-spawn supervisor would adopt the old processes.
  pkill -u mypeople -f "$DATA/(bin|plugins)/" 2>/dev/null
  pkill -u mypeople -f "$M/releases/[^ ]*/(update.py|agent-index.sh)" 2>/dev/null
  pkill -u mypeople -x ttyd 2>/dev/null
  if [ -n "${child:-}" ]; then kill "$child" 2>/dev/null; wait "$child" 2>/dev/null; fi
}

# Healthy = the release is still running AND the board answers AND the chat bridge has stayed up
# for 10s (the supervisor respawns a crashing one, so "a process exists" proves nothing) AND the
# Boss runs Claude and is logged in. All of it at once, within HEALTH_WAIT.
bridge_up(){
  local pid; pid="$(pgrep -o -u mypeople -f "plugins/plow-chat/plow-chat.py serve")" || return 1
  [ "$(ps -o etimes= -p "$pid" | tr -d ' ')" -ge 10 ] 2>/dev/null
}
healthy(){
  local deadline=$((SECONDS + HEALTH_WAIT))
  while [ $SECONDS -lt $deadline ]; do
    kill -0 "$child" 2>/dev/null || return 1
    if curl -fsS -m 3 "http://127.0.0.1:${TODO_PORT:-9933}/health" >/dev/null 2>&1 \
       && bridge_up \
       && me tmux list-panes -a -F '#{window_name} #{pane_current_command}' 2>/dev/null \
            | grep -q '^Boss claude' \
       && ! me tmux capture-pane -p -t mc-main:Boss 2>/dev/null | grep -q 'Not logged in'; then
      return 0
    fi
    sleep 5
  done
  return 1
}

tell_owner(){
  me env PYTHONPATH="$M/current/py" PLOW_CHAT_STATE_DIR=/tmp/myplow-update-chat \
    python3 "$M/current/py/mypeople/runtime/plugins/plow-chat/plow-chat.py" reply "$1" >/dev/null 2>&1
}

switch(){
  local to image from snap
  read -r to image < "$REQUEST"
  rm -f "$REQUEST"
  [ -x "$M/releases/$to/run.sh" ] || { beacon "update: $to not staged, skipped"; return; }
  from="$(readlink "$M/current")"
  snap="$(snapshot "${from##*/}")" || { beacon "update: snapshot FAILED, staying on $from"; return; }
  point "releases/$to"
  printf '%s %s %s\n' "$from" "$image" "$snap" > "$PENDING"
  beacon "update: switched $from -> releases/$to"
}

rollback(){
  local from image snap
  read -r from image snap < "$PENDING"
  stop_fleet
  point "$from"
  restore "$snap"
  echo "$image" >> "$BAD"
  rm -f "$PENDING"
  beacon "update: ROLLED BACK to $from (bad: $image)"
  rolled_back="$(cat "$M/$from/RELEASE" 2>/dev/null)"
}

rolled_back=""
while :; do
  [ -f "$REQUEST" ] && switch
  me "$M/current/run.sh" & child=$!
  if [ -f "$PENDING" ]; then
    if healthy; then
      rm -f "$PENDING"
      beacon "update: healthy on $(readlink "$M/current")"
      # Keep the release we came from for a manual way back; drop anything older.
      ls -1dt "$M"/releases/*/ | tail -n +3 | grep -v "$(readlink -f "$M/current")" | xargs -r rm -rf
    else
      rollback
      continue
    fi
  fi
  if [ -n "$rolled_back" ]; then
    healthy && tell_owner "An update didn't pass its health check, so I went back to the version I was on ($rolled_back). Everything you had is still here."
    rolled_back=""
  fi
  wait "$child"; rc=$?
  [ -f "$REQUEST" ] || { beacon "release exited ($rc), restarting it"; sleep 5; }
done
