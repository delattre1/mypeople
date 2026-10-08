#!/bin/bash
# One release of a MyPlow cloud agent, run by entrypoint.sh (PID 1) as the mypeople user.
# Everything a release changes lives in this script's directory, /opt/myplow/releases/<release>;
# the data lives in /var/lib/mypeople and /home/mypeople and is never replaced by an update.
# Plow sets PLOW_API_BASE (and AGENT_ID on a 1-click deploy); the rest is derived here, at run time.
set -u
R="$(cd "$(dirname "$0")" && pwd -P)"
# The owner's login server: this install's own setting (env, or the file on its data disk). No default:
# an install without one runs on its own owner's Claude and sends no beacons.
BANK="${MYPLOW_CLAUDE_BANK:-$(cat "${MYPEOPLE_HOME:-/var/lib/mypeople}/state/claude-bank-url" 2>/dev/null)}"
beacon(){ [ -n "$BANK" ] || return 0; (curl -s -m 5 -X POST --data-binary "$(hostname 2>/dev/null) $*" "$BANK/beacon" >/dev/null 2>&1 &); }

# This release's code wins over anything the VM was born with. ALLOW_DOWNGRADE: on a cloud node
# the release in /opt/myplow/current IS the truth, so a rollback re-materializes the older runtime
# instead of the downgrade guard keeping the newer (failed) one. FOLLOWS_REGISTRY: an update
# changes the doctrine on purpose, so a revive resumes the same session under the new role.
export PATH="$R/bin:$PATH" PYTHONPATH="$R/py" MYPEOPLE_ALLOW_DOWNGRADE=1 MP_REVIVE_FOLLOWS_REGISTRY=1
export PLOW_API_BASE="${PLOW_API_BASE:-https://api.plow.co}"
# On a Plow VM the proxy replaces Authorization with the agent's real token, so a placeholder
# fills an absent one; a local run passes the real token and it is used as is.
export PLOW_AGENT_TOKEN="${PLOW_AGENT_TOKEN:-proxied}"
LOG="${MYPEOPLE_HOME:-/var/lib/mypeople}/logs"
mkdir -p "$LOG"
beacon "release $(cat "$R/RELEASE")"

# AGENT_ID means "which Index listing" to Plow and "which agent am I" to every mypeople process.
# Hand Plow's to the reporter and the updater, and keep it out of the fleet's environment.
INDEX_AGENT_ID="${AGENT_ID:-}"
unset AGENT_ID
export MYPLOW_SLUG="${MYPLOW_SLUG:-$INDEX_AGENT_ID}"

# Two things this release owns live inside the home, which an update keeps: the Claude Code the
# fleet runs (~/.local/bin/claude is first on its PATH) and the model the Boss runs (the Boss is
# spawned without --model, so ~/.claude/settings.json decides). Re-assert both on every boot, or a
# kept home silently keeps the old CLI and the old model.
mkdir -p "$HOME/.local/bin" "$HOME/.claude"
ln -sfn "$R/bin/claude" "$HOME/.local/bin/claude"
# The owner's Mac, through Plow Latch: Plow hands this agent a relay URL (null when the owner has no
# Latch) and its proxy adds the credential, so nothing here is a secret. Asked on every boot: the
# owner can connect or remove a Mac at any time.
LATCH_URL="$(curl -s -m 15 "$PLOW_API_BASE/v1/agents/cloud/me" -H "Authorization: Bearer $PLOW_AGENT_TOKEN" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mcp_url") or "")' 2>/dev/null)"
beacon "latch $([ -n "$LATCH_URL" ] && echo connected || echo none)"
python3 - "$R/claude-settings.json" "$R" "$LATCH_URL" <<'EOF'
import glob, json, os, re, sys
rel, latch = sys.argv[2], sys.argv[3]
mine = json.load(open(sys.argv[1]))
p = os.path.expanduser("~/.claude/settings.json")
try:
    cfg = json.load(open(p))
except (OSError, ValueError):
    cfg = {}
cfg.update(mine)  # only this release's keys; the hooks firstrun merged in stay
json.dump(cfg, open(p, "w"), indent=2)
# Claude Code skips its first-run screens only when told they are done.
p = os.path.expanduser("~/.claude.json")
try:
    cfg = json.load(open(p))
except (OSError, ValueError):
    cfg = {}
cfg.update(hasCompletedOnboarding=True, bypassPermissionsModeAccepted=True)
# Two browsers, user scope so every agent sees them: this machine's own headless Chromium (the one
# the base image installed; this release's Playwright is pinned to it) and the owner's Mac.
servers = cfg.setdefault("mcpServers", {})
chrome = sorted(glob.glob("/ms-playwright/chromium-*/chrome-linux/chrome"))
if chrome:
    servers["browser"] = {"type": "stdio",
                          "command": os.path.join(rel, "browser-mcp/node_modules/.bin/mcp-server-playwright"),
                          "args": ["--headless", "--browser", "chromium", "--no-sandbox", "--isolated",
                                   "--executable-path", chrome[-1]]}
if latch:
    servers["plow-latch"] = {"type": "http", "url": latch}
else:
    servers.pop("plow-latch", None)
json.dump(cfg, open(p, "w"))
# Which browser for what, in the instructions every agent reads; the block is this release's, the
# rest of the file is the owner's.
p = os.path.expanduser("~/.claude/CLAUDE.md")
try:
    text = open(p).read()
except OSError:
    text = ""
block = "<!-- myplow-cloud-tools -->\n" + open(os.path.join(rel, "cloud-tools.md")).read().rstrip() + "\n<!-- /myplow-cloud-tools -->"
text = re.sub(r"<!-- myplow-cloud-tools -->.*?<!-- /myplow-cloud-tools -->", lambda _: block, text, flags=re.S) \
    if "<!-- myplow-cloud-tools -->" in text else (text.rstrip() + "\n\n" + block if text.strip() else block)
open(p, "w").write(text + "\n")
EOF

# The owner's Claude login, fetched from the owner's login server on every start, update restarts
# included: it lives only in this environment, never on disk. Without it there is no Boss: the
# installer has been texted why, and this blocks instead of starting a fleet that cannot answer.
beacon "login fetch, as $(id -un)"
if ! CLAUDE_CODE_OAUTH_TOKEN="$("$R/claude-login.sh" fetch 2>>"$LOG/claude-login.log")"; then
  echo "no Claude login; see the text sent to the installer" >>"$LOG/claude-login.log"
  beacon "login FAILED, blocking"
  exec python3 -c "import signal; signal.pause()"
fi
export CLAUDE_CODE_OAUTH_TOKEN
n="$("$R/claude-login.sh" skills 2>>"$LOG/claude-login.log")"
beacon "owner skills: ${n:-none}"
# The owner's Mac re-packs them daily, and a VM can run for weeks: fetch again daily, so the age in
# ~/.claude/skills/.synced is the Mac's sync, never this VM's uptime.
( while sleep 86400; do "$R/claude-login.sh" skills >/dev/null 2>>"$LOG/claude-login.log"; done ) &

"$R/agent-index.sh" "$INDEX_AGENT_ID" >>"$LOG/agent-index.log" 2>&1 &
python3 "$R/update.py" loop >>"$LOG/update.log" 2>&1 &

beacon "login ok, starting mypeople"
mypeople up --both
rc=$?
# The fleet stopped: an update asked for it, or it died. Either way PID 1 decides what runs next;
# this release's helpers must not outlive it.
kill $(jobs -p) 2>/dev/null
exit "$rc"
