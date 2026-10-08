#!/usr/bin/env bash
# One persistent agent session in tmux window ig:agent. If the CLI exits it comes straight back,
# so the plugin always has a tab to deliver into. The container lives as long as the session.
set -eu
# First boot (Claude): skip onboarding and the trust / bypass prompts, which would block the tab.
[ -f ~/.claude.json ] || cat > ~/.claude.json <<EOF
{"hasCompletedOnboarding": true, "bypassPermissionsModeAccepted": true,
 "projects": {"/home/node": {"hasTrustDialogAccepted": true}}}
EOF
# IG_AGENT_CMD is set by the plugin; which CLI and model run is its choice, not the image's.
tmux new-session -d -s ig -n agent -x 200 -y 50 \
  "while true; do ${IG_AGENT_CMD:-grok --permission-mode bypassPermissions -m grok-4.7}; sleep 2; done"
exec tmux wait-for ig-agent-stop
