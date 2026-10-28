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
import importlib.util
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
                f"wanted. This web chat is a preview (Daniel 10-28): in that greeting, tell them once that they can keep "
                f"testing you here, and that the real test is on their phone: they scan the QR code or tap the Continue "
                f"on iMessage button and text you on iMessage. If you have your own iMessage number, put "
                f"sms:<that number>&body=Hi in that same reply: the page turns it into that button (a QR code on a "
                f"computer). {rules})")
    page = f" (came from page {m['page']})" if m.get("first") and m.get("page") else ""
    # Tapbacks they made since their last message ride along, apart from the new text (as Plow Chat hands them over).
    taps = "".join(f"(Earlier, separate from this text: they reacted {t['emoji']} to {'your' if t['on_mine'] else 'their own'} "
                   f"message \"{t['on']}\".)\n" for t in m.get("tapbacks") or [])
    return (f"[web-chat] from web visitor {v}{page}: {m['text']}\n{taps}"
            f"(Someone on Daniel's public web chat page; each visitor is their own thread. FIRST, before reading on: "
            f"{RUN} react {v} {m['id']} 👀 (shows them it was read). {rules})")


PLOW_CHAT = INSTALL / "plugins" / "plow-chat" / "plow-chat.py"
_router = None


def _plow_chat():
    """plow-chat's own router (its pick_agent: one JEV decision over the live agents), loaded once."""
    global _router
    if _router is None:
        spec = importlib.util.spec_from_file_location("plow_chat_router", PLOW_CHAT)
        _router = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_router)
    return _router


ANSWERED = STATE_DIR / "answered.json"  # {visitor: {agent, at, text}}: who answered each visitor last, for the router


def note_answer(visitor, text):
    """Remember which agent (its AGENT_ID) answered this visitor, when, and what it said: a bare follow-up like "Yes"
    then goes to whoever made the offer (Daniel 10-28: his "Yes" to Lin's offer went to the Boss, two replies)."""
    agent = (os.environ.get("AGENT_ID") or "").rsplit(":", 1)[-1]
    if agent:
        seen = read_json(ANSWERED, {})
        seen[visitor] = {"agent": agent, "at": time.time(), "text": text[:300]}
        write_json(ANSWERED, dict(list(seen.items())[-500:]))


def target_for(m):
    """PLOW_CHAT_ROUTER=jev (Daniel 10-28): a visitor's text goes straight to the live agent that owns it, picked the
    way plow-chat picks for iMessage (with who answered them last as context), so the Boss does not route by
    thinking. A handover, the router off, or anything failing goes to the Boss."""
    if os.environ.get("PLOW_CHAT_ROUTER") != "jev" or m.get("kind") == "handover" or not m.get("text"):
        return TARGET
    try:
        pc = _plow_chat()
        last = read_json(ANSWERED, {}).get(m.get("visitor") or "") or {}
        minutes = (time.time() - last["at"]) / 60 if last else None
        if last and minutes <= pc.FOLLOW_MINUTES:
            return pc.pick_agent(m["text"], {}, last["agent"], minutes, last["text"])
        return pc.pick_agent(m["text"], {})
    except Exception as e:  # noqa: BLE001 - a router that fails must never lose the text
        log(f"router: {type(e).__name__}; to the Boss")
        return TARGET


def deliver(m):
    """One visitor message to the agent that owns it (the Boss unless the router picks another); True once mp took it."""
    to = target_for(m)
    p = subprocess.run([MP_BIN, "send", to, envelope(m)], capture_output=True, text=True, timeout=60)
    if p.returncode:
        log(f"mp send failed for {m['visitor']} {m.get('id', m.get('kind'))}: exit {p.returncode}")  # never the stderr: it can quote their text
        return False
    log(f"delivered {m['visitor']} {m.get('id', m.get('kind'))} to {to}")
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
        text = " ".join(args[1:])
        print(json.dumps(call("POST", "/agent/reply", {"visitor": args[0], "text": text})))
        note_answer(args[0], text)
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
