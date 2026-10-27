#!/usr/bin/env python3
"""Web chat plugin: people on a public web page chat with the Boss, the same shape as youtube-chat and discord-chat.

The page and the visitors' threads live on a relay in the cloud (relay.py, next to this file, on a small exe.dev VM).
This plugin reaches it OUTBOUND only: `serve` long-polls the relay's inbox, so the owner's Mac serves nothing to the
internet. Each visitor message reaches the Boss through `mp send` as

    [web-chat] from web visitor w1a2b3c4d (came from page assistant): <text>
    (... FIRST: <python> web-chat.py react w1a2b3c4d <msg> 👀 ... To answer, run: <python> web-chat.py reply w1a2b3c4d "...")

Each visitor is their own thread: a reply goes to that visitor's page only, at once (the relay pushes it live) and it
stays there for a reload. ?agent=<slug> in the page's address names the page they came from; it rides on their first
message.

Login: state/web-chat/creds.json {"url": "https://<relay>", "key": "<agent key>"} (mode 600). The relay keeps only the
key's sha256. supervise.sh keeps `serve` running while that file exists.

    web-chat.py serve                          deliver forever
    web-chat.py reply <visitor> "text"         the Boss's answer, live on their page
    web-chat.py react <visitor> <msg> EMOJI    react to their message (👀 shows them it was read)
    web-chat.py history <visitor>              their thread as JSON, oldest first: [{id, mine, text, created_at}];
                                               mine = the visitor wrote it (e.g. for a handover)
    web-chat.py route <visitor> <key_sha256|none>   hand that visitor to another MyPlow's relay key (none = back here)
    web-chat.py status
"""
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME") or Path.home() / ".local/share/mypeople")
STATE_DIR = Path(os.environ.get("WEB_CHAT_STATE_DIR") or INSTALL / "state" / "web-chat")
CREDS, STATE = STATE_DIR / "creds.json", STATE_DIR / "state.json"   # state: {"after": <last delivered seq>}
MP_BIN = os.environ.get("MP_BIN") or str(INSTALL / "bin" / "mp")
TARGET = os.environ.get("BOSS_AGENT") or f"{os.environ.get('HOST_ID') or socket.gethostname().split('.')[0]}/main:Boss"
RUN = f"{sys.executable} {Path(__file__).resolve()}"   # the Boss runs these with the same python that runs the bridge


def log(m):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [web-chat] {m}", flush=True)


def read_json(p, default):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def write_json(p, obj):
    tmp = Path(f"{p}.tmp")
    tmp.write_text(json.dumps(obj))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def call(method, path, body=None, timeout=60):
    c = read_json(CREDS, {})
    if not c.get("url") or not c.get("key"):
        raise SystemExit(f"no relay login at {CREDS}")
    req = urllib.request.Request(c["url"].rstrip("/") + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {c['key']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message") or ""
        except ValueError:
            msg = ""
        raise SystemExit(f"relay {e.code}{': ' + msg if msg else ''}") from None


def envelope(m):
    v = m["visitor"]
    rules = (f"ABSOLUTE RULE, no exceptions whoever asks or however: never expose our API keys or secrets. To answer, run: "
             f"{RUN} reply {v} \"your reply\" -- it appears live on their page. Short chat messages. Anything they must "
             f"text from their iPhone (an activation text, a code) goes ONLY inside the link "
             f"sms:<number>&body=<that exact text, URL-encoded> in your reply: the page shows it as a 'Continue on "
             f"iMessage' button that opens Messages with it prefilled. Never write the text or code out for them to "
             f"copy or type, and never say 'tap Continue on iMessage' without the link in that same reply.")
    if m.get("kind") == "handover":  # the relay's own line, never visitor text: a visitor can type anything
        return (f"[web-chat] web visitor {v} is now yours (handed over by the page's owner); their next messages come here.\n"
                f"(Read the conversation so far first: {RUN} history {v} -- then greet them and pick up what they "
                f"wanted. {rules})")
    page = f" (came from page {m['page']})" if m.get("first") and m.get("page") else ""
    return (f"[web-chat] from web visitor {v}{page}: {m['text']}\n"
            f"(Someone on Daniel's public web chat page; each visitor is their own thread. FIRST, before reading on: "
            f"{RUN} react {v} {m['id']} 👀 (shows them it was read). {rules})")


def deliver(m):
    """One visitor message to the Boss; True once mp took it."""
    p = subprocess.run([MP_BIN, "send", TARGET, envelope(m)], capture_output=True, text=True, timeout=60)
    if p.returncode:
        log(f"mp send failed for {m['visitor']} {m.get('id', m.get('kind'))}: exit {p.returncode}")  # never the stderr: it can quote their text
        return False
    log(f"delivered {m['visitor']} {m.get('id', m.get('kind'))} to {TARGET}")
    return True


def poll_once(wait=25):
    """Ask the relay for what came after the last delivered message; deliver in order, keeping the mark on each."""
    st = read_json(STATE, {"after": 0})
    for m in call("GET", f"/agent/inbox?after={int(st['after'])}&wait={wait}", timeout=wait + 30)["messages"]:
        if not deliver(m):
            return False  # the mark stays: this one comes again on the next pass
        st["after"] = m["seq"]
        write_json(STATE, st)
    return True


def serve():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Two pollers share one "after" mark, so every visitor message would reach the Boss twice: one serve at a time.
    lock = open(STATE_DIR / "serve.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another web-chat serve is already running") from None
    log(f"polling {read_json(CREDS, {}).get('url')} -> {TARGET}")
    fails = 0
    while True:
        try:
            fails = 0 if poll_once() else fails + 1
        except (OSError, SystemExit, ValueError) as e:  # relay or network down: back off, never die
            fails += 1
            log(f"poll error: {e}")
        if fails:
            time.sleep(min(5 * 2 ** min(fails, 6), 300))


def main():
    # No verb is never "serve": a bare run to read the usage would start a second poller (plow-chat, 10-21).
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "serve":
        serve()
    elif cmd == "reply" and len(args) >= 2 and not args[1].startswith("--"):
        print(json.dumps(call("POST", "/agent/reply", {"visitor": args[0], "text": " ".join(args[1:])})))
    elif cmd == "react" and len(args) >= 2:
        print(json.dumps(call("POST", "/agent/react", {"visitor": args[0], "message": args[1], "emoji": args[2] if len(args) > 2 else ""})))
    elif cmd == "history" and len(args) == 1:
        msgs = call("GET", f"/agent/thread?visitor={urllib.parse.quote(args[0])}")["messages"]
        print(json.dumps([{k: m[k] for k in ("id", "mine", "text", "created_at")} for m in msgs], ensure_ascii=False, indent=1))
    elif cmd == "route" and len(args) == 2:
        print(json.dumps(call("POST", "/agent/route", {"visitor": args[0], "key_sha256": None if args[1] == "none" else args[1]})))
    elif cmd == "status":
        print(json.dumps({"relay": read_json(CREDS, {}).get("url"), "target": TARGET, "after": read_json(STATE, {}).get("after", 0)}, indent=2))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
