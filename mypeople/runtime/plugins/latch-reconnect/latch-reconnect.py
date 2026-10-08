#!/usr/bin/env python3
"""Latch reconnect: agents get their Latch tools back after Latch restarts, without anyone noticing.

Claude Code retries a dropped MCP server ~5 times over ~31s, then gives up for the life of the
session. Latch installs its updates when it quits and nothing reopens it, so after the 10-01 update
(off 3h22m) every running agent silently lost Latch until a human typed /mcp. This watcher sees
Latch come back and types `/mcp reconnect all` into each Claude agent's window, only when IDLE.

Latch is UP when an MCP `ping` through the Plow relay (the URL + key the agents use, read from
~/.claude.json) answers with a result. A reconnect is owed to every live Claude agent when Latch
comes back up after being down, when a new Latch process appears, and once when this starts.

Latch's updater installs on QUIT and never reopens it. So when Latch is gone and the installed
app's version differs from the one last seen running, this reopens it in the background (after
RELAUNCH_AFTER seconds, at most once per RELAUNCH_AFTER). A quit with no new version is the
owner's choice and is left alone.

IDLE, all re-checked right before typing (a busy agent stays owed until it is idle):
  - its lifecycle hook status is "idle" (Stop fired, no new prompt), or "starting" (a session
    never prompted since it started: it sits at its prompt), unchanged for IDLE_SECONDS;
  - its pane is not in copy mode (keys would scroll, not type);
  - the pane's bottom lines show no in-flight turn ("esc to interrupt", "waiting for response");
  - the composer line is empty, so nothing half-typed (by a human or `mp send`) is touched.
    Claude Code's suggested next prompt is drawn there DIMMED and is not input: it is ignored.

Turn it on with LATCH_RECONNECT=1 in queue.env; supervise.sh keeps it running.
  latch-reconnect.py serve          run the watcher (supervise.sh entry point)
  latch-reconnect.py status         Latch up/down, pid, agents still owed a reconnect
  latch-reconnect.py once <agent>   reconnect one agent now, if it is idle
"""
import json
import os
import plistlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME")
               or Path.home() / "mypeople")
STATUS_DIR = INSTALL / "status"
STATE = Path(os.environ.get("LATCH_RECONNECT_STATE")
             or INSTALL / "state" / "latch-reconnect" / "state.json")
CLAUDE_JSON = Path(os.environ.get("LATCH_RECONNECT_CLAUDE_JSON") or Path.home() / ".claude.json")
HOST_ID = os.environ.get("HOST_ID") or os.uname().nodename.split(".")[0]
POLL_SECONDS = float(os.environ.get("LATCH_RECONNECT_POLL_SECONDS", "20"))
IDLE_SECONDS = float(os.environ.get("LATCH_RECONNECT_IDLE_SECONDS", "20"))
# The main process only: its argv is the bare binary. Helpers (".../MacOS/Plow Latch Helper (GPU)")
# and Latch's own Node children (the same binary plus a script path) come and go on their own.
LATCH_PROCESS = os.environ.get("LATCH_RECONNECT_PROCESS", r"Plow Latch\.app/Contents/MacOS/Plow Latch$")
LATCH_APP = Path(os.environ.get("LATCH_RECONNECT_APP", "/Applications/Plow Latch.app"))
RELAUNCH_AFTER = float(os.environ.get("LATCH_RECONNECT_RELAUNCH_AFTER", "60"))
COMMAND = "/mcp reconnect all"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# A dimmed span (SGR 2) up to its reset: how the composer draws a suggestion, never typed text.
DIM = re.compile(r"\x1b\[2m.*?(?:\x1b\[(?:0|22)?m|$)")
BUSY_MARKERS = ("esc to interrupt", "waiting for response", "ctrl+c:cancel")


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] latch-reconnect: {msg}",
          flush=True)


def tmux(*args):
    env = dict(os.environ)
    env.pop("TMUX", None)
    return subprocess.run(["tmux", *args], env=env, capture_output=True, text=True)


def relay_servers():
    """(url, auth) of every Plow-relay MCP server any agent project uses. Read per tick, so a
    new or removed connector needs no restart. The key is used, never logged."""
    try:
        cfg = json.loads(CLAUDE_JSON.read_text())
    except Exception:
        return []
    scopes = [cfg.get("mcpServers") or {}]
    scopes += [(p or {}).get("mcpServers") or {} for p in (cfg.get("projects") or {}).values()]
    out = {}
    for scope in scopes:
        for s in scope.values():
            url = (s or {}).get("url") or ""
            if s.get("type") == "http" and "/v1/relay/devices/" in url:
                out[url] = (s.get("headers") or {}).get("Authorization", "")
    return list(out.items())


def ping(url, auth):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": auth, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200 and '"result"' in r.read(4096).decode(errors="ignore")
    except (urllib.error.URLError, OSError, ValueError):
        return False


def latch_up():
    return any(ping(u, a) for u, a in relay_servers())


def latch_version():
    try:
        with open(LATCH_APP / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleVersion")
    except (OSError, plistlib.InvalidFileException):
        return None


def relaunch():
    r = subprocess.run(["open", "-g", "-a", str(LATCH_APP)], capture_output=True, text=True)
    log("reopened Latch after its update" if r.returncode == 0 else f"reopen failed: {r.stderr.strip()[:120]}")


def latch_pid():
    r = subprocess.run(["pgrep", "-f", LATCH_PROCESS], capture_output=True, text=True)
    return min((int(p) for p in r.stdout.split()), default=None)


def claude_agents():
    """{agent_id: status dict} for live Claude agents. /mcp is a Claude Code command."""
    out = {}
    for f in STATUS_DIR.glob("mc-*/*.json"):
        try:
            st = json.loads(f.read_text())
        except Exception:
            continue
        if st.get("backend") == "claude" and st.get("state", "alive") == "alive":
            out[f"{HOST_ID}/{f.parent.name[3:]}:{f.stem}"] = st
    return out


def target(aid):
    sess, tab = aid.split("/", 1)[1].split(":", 1)
    return f"mc-{sess}:{tab}"


def live_panes():
    """{"mc-main:eng-1": (pane_id, in_mode)} by EXACT window name. Never `-t mc-main:eng-1`:
    tmux resolves a missing window by name prefix, and display-message even falls back to the
    current window, so a dead agent's checks (and keys) would land on another agent's pane."""
    r = tmux("list-windows", "-a", "-F", "#{session_name}:#{window_name}\t#{pane_id}\t#{pane_in_mode}")
    out = {}
    for line in r.stdout.splitlines():
        name, pane, mode = (line.split("\t") + ["", ""])[:3]
        out[name] = (pane, mode)
    return out


def why_busy(aid, st=None, now=None, panes=None):
    """None when the agent is idle enough to type into, else the reason it is not."""
    st = st if st is not None else claude_agents().get(aid)
    if not st:
        return "gone"
    # Pane first: a status file outlives its agent, frozen at whatever it last said.
    pane, mode = (panes if panes is not None else live_panes()).get(target(aid), (None, None))
    if not pane:
        return "no pane"
    # "starting" is a session never prompted since it began: it sits at its prompt for good,
    # and leaving it out left those agents cut off from Latch after every restart.
    if st.get("status") not in ("idle", "starting"):
        return "status %s" % st.get("status")
    try:
        if (now or time.time()) - float(st.get("timestamp") or 0) < IDLE_SECONDS:
            return "%s too recently" % st.get("status")
    except ValueError:
        return "no timestamp"
    if mode != "0":
        return "pane in copy mode"
    rows = [(raw, ANSI.sub("", raw)) for raw in tmux("capture-pane", "-e", "-p", "-t", pane).stdout.splitlines()]
    rows = [r for r in rows if r[1].strip()][-12:]
    if any(m in "\n".join(plain for _, plain in rows).lower() for m in BUSY_MARKERS):
        return "turn in flight"
    prompts = [raw for raw, plain in rows if plain.lstrip().startswith("❯")]
    if not prompts:
        return "no composer on screen"
    if ANSI.sub("", DIM.sub("", prompts[-1])).replace("\u00a0", " ").strip() != "❯":
        return "composer not empty"
    return None


def reconnect(aid):
    """Type the reconnect into an idle agent. True when typed."""
    panes = live_panes()
    if why_busy(aid, panes=panes):
        return False
    pane = panes[target(aid)][0]
    # One tmux invocation for text + Enter, so no other client's keys land between them.
    r = tmux("send-keys", "-t", pane, "-l", COMMAND, ";", "send-keys", "-t", pane, "Enter")
    if r.returncode != 0:
        log(f"{aid}: send-keys failed: {r.stderr.strip()[:120]}")
        return False
    time.sleep(4)
    tail = [ln for ln in tmux("capture-pane", "-p", "-t", pane).stdout.splitlines() if "Reconnected" in ln]
    log(f"{aid}: reconnected ({tail[-1].strip()[:100] if tail else 'no result line seen'})")
    return True


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE)


def tick(s, up, pid, agents, version=None, now=None):
    """One pass: reopen Latch after an update, decide who is owed a reconnect, then serve the idle
    ones. Mutates s."""
    now = now or time.time()
    if pid:
        s["gone_since"] = None
        s["running_version"] = version or s.get("running_version")
    else:
        s["gone_since"] = s.get("gone_since") or now
        updated = version and s.get("running_version") and version != s["running_version"]
        if (updated and now - s["gone_since"] >= RELAUNCH_AFTER
                and now - (s.get("relaunched_at") or 0) >= RELAUNCH_AFTER):
            s["relaunched_at"] = now
            relaunch()
    owed = set(s.get("owed", []))
    if not up:
        s["down_since"] = s.get("down_since") or time.time()
    else:
        panes = live_panes()
        cause = ("back after %ds down" % (time.time() - s["down_since"])) if s.get("down_since") else (
            "new Latch process" if pid and s.get("pid") and pid != s["pid"] else (
                "watcher start" if not s.get("started") else ""))
        if cause:
            alive = {a for a in agents if target(a) in panes}
            log(f"Latch {cause}: {len(alive)} Claude agents owed a reconnect")
            owed |= alive
        s["down_since"] = None
        s["started"] = True
        for aid in sorted(owed):
            reason = why_busy(aid, agents.get(aid), panes=panes)
            if reason in ("gone", "no pane") or (reason is None and reconnect(aid)):
                owed.discard(aid)
    s["pid"] = pid or s.get("pid")
    s["owed"] = sorted(owed)
    return s


def serve():
    log(f"watching Latch every {POLL_SECONDS:.0f}s ({len(relay_servers())} relay connectors)")
    s = load_state()
    s["started"] = False
    while True:
        try:
            save_state(tick(s, latch_up(), latch_pid(), claude_agents(), latch_version()))
        except Exception as e:  # a bad tick must never kill the watcher
            log(f"tick failed: {e}")
        time.sleep(POLL_SECONDS)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "serve":
        serve()
    elif cmd == "status":
        s = load_state()
        print(json.dumps({"latch_up": latch_up(), "latch_pid": latch_pid(),
                          "latch_version": latch_version(), "running_version": s.get("running_version"),
                          "down_since": s.get("down_since"),
                          "owed": {a: why_busy(a) or "idle, next tick" for a in s.get("owed", [])}},
                         indent=1))
    elif cmd == "once" and len(argv) > 2:
        reason = why_busy(argv[2])
        if reason:
            print(f"not typed: {reason}")
            return 1
        return 0 if reconnect(argv[2]) else 1
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
