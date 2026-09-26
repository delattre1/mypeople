#!/usr/bin/env bash
# MyPlow lifecycle hook. Gates on AGENT_ID (its FIRST line) so non-agent sessions
# has no AGENT_ID and exits silently. Delivers status writes + Boss notification.
[ -z "$AGENT_ID" ] && exit 0
EVENT="$1"
INPUT="$(cat)"
DIR="${INSTALL_DIR:-$HOME/mypeople}/plugins/tmux-boss-hooks"
printf '%s' "$INPUT" | python3 "$DIR/hook-handler.py" "$EVENT" 2>/dev/null || true
exit 0
