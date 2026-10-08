#!/bin/bash
# Report this MyPlow's Claude Code usage to the Agent Index every 5 minutes.
# Same shape as life-assistant-hermes-agent's agent-index service: the client decides whether it
# is registered (0 yes, 3 no, 2 unreadable -- never register over 2), and only the one
# registration call is handed the Plow token.
id="${1:-}"
C="$(cd "$(dirname "$0")" && pwd -P)/agent_index_client.py"
if [ -z "$id" ]; then
  echo "agent-index: no AGENT_ID (not a 1-click deploy), nothing to report for -- standing down"
  exec sleep infinity
fi
# agentsview is the client's only view of Claude Code. It reads the transcripts where this
# image's Claude writes them, and must be synced first: a stale index is a valid EMPTY answer,
# which the Index would record as a day with no work.
export CLAUDE_PROJECTS_DIR="$HOME/.claude/projects"
while :; do
  python3 "$C" status >/dev/null
  case $? in
    0) ;;
    3) python3 "$C" --register --agent "$id" >/dev/null \
         || { echo "agent-index: not registered this pass"; sleep 300; continue; } ;;
    *) echo "agent-index: install state unreadable (above), not registering over it"
       sleep 300; continue ;;
  esac
  agentsview sync >/dev/null 2>&1 || echo "agent-index: agentsview sync failed"
  env -u PLOW_AGENT_TOKEN python3 "$C" --agent "$id" || echo "agent-index: report failed (above)"
  sleep 300
done
