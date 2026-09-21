#!/usr/bin/env bash
# Single persistent daemon supervisor (§5.8 / H-STABLE). Own pidfile, setsid, survives the install
# shell. CHECK-BEFORE-SPAWN: pgrep first; if a daemon is already alive it spawns NOTHING (steady-state
# invariant: exactly one of each). Owns queue-server, todo-server, queue-client, ttyd, board-exporter,
# boss-supervisor.
set -u
# The live Terminal Graph consumes several descriptors per persistent ttyd/WebSocket client. macOS launchd
# defaults to 256, which can strand every live tile in ttyd's reconnect screen under normal fleets.
ulimit -n "${MYPEOPLE_NOFILE_LIMIT:-8192}" 2>/dev/null || true
source "${MYPEOPLE_CONFIG_PATH:-$HOME/.config/mypeople/queue.env}" 2>/dev/null || true
ID="${INSTALL_DIR:-$HOME/mypeople}"
export PATH="$HOME/.local/bin:$ID/bin:$PATH"
export LANG="${LANG:-C.UTF-8}" LC_ALL="${LC_ALL:-C.UTF-8}"
BIN="$ID/bin"
LOG="$ID/logs"
mkdir -p "$LOG"
PIDFILE="$ID/run/supervise.pid"

# single-supervisor guard
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  if [ "$(cat "$PIDFILE")" != "$$" ]; then
    echo "supervisor already running ($(cat "$PIDFILE"))"; exit 0
  fi
fi
echo $$ > "$PIDFILE"

ensure(){
  # Match the daemon by script ABSPATH in argv. Never key liveness on interpreter/comm: Homebrew
  # framework Python reports `Python`, not `python3`, which otherwise causes duplicate respawns.
  local pat="$1"; shift
  if pgrep -f "$pat" >/dev/null 2>&1; then return; fi
  echo "$(date -u +%FT%TZ) starting: $*" >> "$LOG/supervise.log"
  # Detach into its own session. Linux has setsid; macOS/BSD don't — fall back to nohup,
  # which (with the supervisor itself already session-led via start_new_session) is enough
  # to keep daemons alive independent of the controlling terminal.
  if command -v setsid >/dev/null 2>&1; then
    setsid bash -c "$*" </dev/null >>"$LOG/daemon.log" 2>&1 &
  else
    nohup bash -c "$*" </dev/null >>"$LOG/daemon.log" 2>&1 &
  fi
}

TTYD_PORT="${TTYD_PORT:-7681}"
# The Terminal Graph tiles onto a READ-ONLY ttyd: a tile is a view, and a stray click must never
# type into a live agent. Stock ttyd is readonly unless -W, so this is the same binary, no flag.
TTYD_RO_PORT="${TTYD_RO_PORT:-$((TTYD_PORT + 1))}"
HUD_PORT="${HUD_PORT:-9900}"
TODO_PORT="${TODO_PORT:-9933}"
# Closing a browser TTY tab must never prompt "Are you sure you want to leave this page?" — a tile
# or attach view is disposable, and the xterm.js beforeunload guard is pure friction here. Client
# options go AFTER -p so the pgrep liveness patterns above stay an exact prefix of the live argv.
# ttyd 1.7.x rewrites argv for ps (`-t key=value` shows as `key value`), so never grep key=value.
TTYD_CLIENT_OPTS="-t disableLeaveAlert=true"

# ttyd 1.7.7 does not reap every ttyd-attach.sh child it forks: a disconnected tile can leave a
# terminated child un-waited, and ttyd keeps that child's pty MASTER fd open forever. macOS caps
# ptys at kern.tty.ptmx_max (511), so a viewer left up for days accumulates ~100 dead ptys/day
# until NOTHING on the machine can open a terminal — Ghostty, tmux, ssh, all of it. Observed
# 2026-08-11: one ttyd holding 414 ptys, 410 of its children terminated-but-unreaped.
# 1.7.7 is current stable, so there is no upgrade; recycle the viewer instead. Keyed on the count
# of TERMINATED children (macOS STAT `E`, Linux `Z`) — never on total children, which is just the
# live tile count and scales with the fleet. ttyd is a stateless viewer and browsers reconnect on
# their own, so a recycle costs a blink; `ensure` below respawns it within one loop.
TTYD_MAX_DEAD_CHILDREN="${TTYD_MAX_DEAD_CHILDREN:-20}"
recycle_leaked_ttyd(){
  local pat="$1" pid dead
  for pid in $(pgrep -f "$pat" 2>/dev/null); do
    dead=$(ps -eo ppid=,stat= | awk -v p="$pid" '$1==p && $2 ~ /[EZ]/' | wc -l | tr -d ' ')
    if [ "$dead" -gt "$TTYD_MAX_DEAD_CHILDREN" ]; then
      echo "$(date -u +%FT%TZ) recycling ttyd $pid: $dead unreaped children leaking ptys" >> "$LOG/supervise.log"
      kill "$pid" 2>/dev/null
    fi
  done
}

while true; do
  ensure "$BIN/queue-server.py"            "exec python3 '$BIN/queue-server.py'"
  ensure "$BIN/todo-server.py"             "exec python3 '$BIN/todo-server.py'"
  ensure "$BIN/queue-client.py"            "exec python3 '$BIN/queue-client.py'"
  ensure "$BIN/board-exporter.py"          "exec python3 '$BIN/board-exporter.py'"
  ensure "ttyd -W -a -p $TTYD_PORT"        "exec ttyd -W -a -p $TTYD_PORT $TTYD_CLIENT_OPTS '$BIN/ttyd-attach.sh'"
  ensure "ttyd -a -p $TTYD_RO_PORT"        "exec ttyd -a -p $TTYD_RO_PORT $TTYD_CLIENT_OPTS '$BIN/ttyd-attach.sh'"
  ensure "$BIN/boss-supervisor.sh"         "exec bash '$BIN/boss-supervisor.sh'"
  # GitHub PR watcher: off until queue.env sets GITHUB_PRS (see the plugin's header).
  [ -n "${GITHUB_PRS:-}" ] && ensure "$ID/plugins/github-prs/github-prs.py" "exec python3 '$ID/plugins/github-prs/github-prs.py' serve"
  recycle_leaked_ttyd "ttyd -W -a -p $TTYD_PORT"
  recycle_leaked_ttyd "ttyd -a -p $TTYD_RO_PORT"
  sleep 10
done
