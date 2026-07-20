#!/usr/bin/env bash
# Boss supervisor (§5.3): always exactly one Boss. Keys off the tmux window (source of truth),
# not the queue. A known Boss is strictly resumed; only a node with no Boss record may spawn one.
set -u
source "${MYPEOPLE_CONFIG_PATH:-$HOME/.config/mypeople/queue.env}" 2>/dev/null || true
export PATH="$HOME/.local/bin:${INSTALL_DIR:-$HOME/mypeople}/bin:$PATH"
HOST="${HOST_ID:-$(hostname -s)}"
export TMUX=

# A node with no AI login cannot host an agent: spawning a Boss against a backend that cannot
# answer produces a window that looks alive and does nothing. So this loop stays idle until a
# login lands -- and because it re-checks every pass, the login alone starts the Boss, with no
# container down/up (card f0e7101c94). The gate LATCHES OPEN on first success, so an ordinary
# authenticated node pays exactly nothing for it, and `mypeople` being absent (runtime used
# standalone) means no gate at all -- i.e. the previous behaviour, unchanged.
LOGIN_GATE=closed
authenticated(){
  [ "$LOGIN_GATE" = open ] && return 0
  command -v mypeople >/dev/null 2>&1 || { LOGIN_GATE=open; return 0; }
  if mypeople auth-check --quiet >/dev/null 2>&1; then
    LOGIN_GATE=open
    # auth-check persists the backend the login actually landed on; pick it up so the Boss is
    # spawned against that one and not against the placeholder written before the login.
    source "${MYPEOPLE_CONFIG_PATH:-$HOME/.config/mypeople/queue.env}" 2>/dev/null || true
    echo "$(date -u +%FT%TZ) login detected -> starting Boss" >> "${INSTALL_DIR:-$HOME/mypeople}/logs/boss-supervisor.log"
    return 0
  fi
  return 1
}

while true; do
  if ! authenticated; then
    sleep 15
    continue
  fi
  if tmux list-windows -t mc-main -F '#{window_name}' 2>/dev/null | grep -qx Boss; then
    :
  else
    echo "$(date -u +%FT%TZ) boss absent -> ensuring persisted session" >> "${INSTALL_DIR:-$HOME/mypeople}/logs/boss-supervisor.log"
    mp ensure-boss "$HOST/main:Boss" >> "${INSTALL_DIR:-$HOME/mypeople}/logs/boss-supervisor.log" 2>&1 || \
      echo "$(date -u +%FT%TZ) ERROR: boss session recovery failed" >> "${INSTALL_DIR:-$HOME/mypeople}/logs/boss-supervisor.log"
  fi
  # Reconcile the rest of the desired fleet too. Only accidental window/server loss is revived;
  # agents stopped through `mp kill` retain their deliberate retirement reason and stay stopped.
  mp reconcile >> "${INSTALL_DIR:-$HOME/mypeople}/logs/boss-supervisor.log" 2>&1 || true
  sleep 15
done
