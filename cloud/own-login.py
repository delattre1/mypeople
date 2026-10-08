#!/usr/bin/env python3
"""Log a MyPlow cloud agent into ITS OWNER's own Claude, by text, at first boot.

Daniel's agents get his login from his login server. Anyone else's install gets a 401 there, and runs
this instead: Claude Code's own `claude setup-token` in a pty (no company key and no API in the
image; it runs on the installer's Claude plan) -> text the installer its link -> read the code they
text back from the same chat -> feed it -> print the login on stdout. claude-login.sh saves it on this
VM, so a restart or an in-place update does not ask again.

The login is printed on stdout only: never logged, never texted, never put in a beacon.
"""
import fcntl
import json
import os
import pty
import re
import select
import struct
import sys
import termios
import time
import urllib.request

API = (os.environ.get("PLOW_API_BASE") or "https://api.plow.co").rstrip("/")
TOKEN = os.environ.get("PLOW_AGENT_TOKEN") or "proxied"
ATTEMPTS = 5
CODE_WAIT = int(os.environ.get("MYPLOW_LOGIN_CODE_WAIT", str(24 * 3600)))  # then a restart asks again
ANSI = re.compile(rb"\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]")
OSC_LINK = re.compile(rb"\x1b\]8;[^;]*;(https://[^\x07\x1b]+)(?:\x07|\x1b\\)")
LOGIN = re.compile(r"sk-ant-[A-Za-z0-9_-]{60,}")
CURSOR_RIGHT = re.compile(rb"\x1b\[\d*C")
FIRST = ("Hi, I'm your MyPlow. To start, tap this to log in with your own Claude (Pro or Max), "
         "approve, then text me the code it shows: {url}")
AGAIN = "That code didn't work. Here's a fresh link; approve and text me the new code: {url}"


def log(msg):  # stderr only: claude-login.sh keeps it in its log; the login never passes here
    print(time.strftime("%H:%M:%S"), "own-login:", msg, file=sys.stderr, flush=True)


def api(method, path, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + TOKEN,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def owner_chat():
    """The 1:1 Plow made for this agent's owner. After a 1-click it exists only once the owner has
    texted their new line (Plow's "your agent is ready" text tells them to), so wait for it."""
    while True:
        try:
            chats = api("GET", "/v1/agents/cloud/me").get("chats") or []
            if chats:
                return chats[0]["uid"]
        except OSError as e:
            log(f"no chat yet ({e})")
        time.sleep(10)


def messages(chat):
    data = api("GET", f"/v1/chats/{chat}/messages")
    return data.get("data") or data.get("messages") or []


def text(chat, body):
    api("POST", f"/v1/chats/{chat}/messages", {"body": body})


def read_until(fd, pattern, seconds):
    """Collect pty output until `pattern` matches the visible text, or the time runs out."""
    buf, end = b"", time.time() + seconds
    while time.time() < end:
        if select.select([fd], [], [], 1)[0]:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if pattern(buf):
                break
    return buf


def login_of(buf):
    """The login setup-token prints, from "sk-ant-" up to "Store this", rejoined if the terminal
    wrapped it. The TUI draws some spaces as cursor moves, so those become spaces first."""
    text = ANSI.sub(b"", CURSOR_RIGHT.sub(b" ", buf)).decode(errors="replace")
    m = re.search(r"(sk-ant-.*?)\s*Store\s*this", text, re.S)
    tok = re.sub(r"\s+", "", m.group(1)) if m else ""
    return tok if LOGIN.fullmatch(tok) else None


def link_of(buf):
    m = OSC_LINK.search(buf)  # the terminal hyperlink carries the whole URL, never wrapped
    if m:
        return m.group(1).decode()
    m = re.search(r"https://claude\.(?:com|ai)/\S*oauth\S+", ANSI.sub(b"", buf).decode(errors="replace"))
    return m.group(0) if m else None


def start():
    """A fresh `claude setup-token`: (pid, pty fd, its authorize link)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm"
        os.execvp("claude", ["claude", "setup-token"])
    # A wide terminal: at 80 columns the 108-character login wraps onto two lines.
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 500, 0, 0))
    buf = read_until(fd, lambda b: link_of(b) and b"astecode" in ANSI.sub(b"", b).replace(b" ", b""), 60)
    url = link_of(buf)
    if not url:
        os.kill(pid, 9)
        raise RuntimeError("claude setup-token printed no login link")
    return pid, fd, url


def wait_for_code(chat, after_ids):
    """The installer's next text in this chat. Anything they sent before the link is not a code."""
    end = time.time() + CODE_WAIT
    while time.time() < end:
        time.sleep(5)
        try:
            new = [m for m in messages(chat)
                   if m.get("direction") == "inbound" and m.get("uid") not in after_ids
                   and (m.get("body") or "").strip()]
        except OSError as e:
            log(f"chat read failed ({e})")
            continue
        if new:
            new.sort(key=lambda m: m.get("created_at") or "")
            return new[-1]["body"].strip().split()[-1], {m["uid"] for m in new}
    return None, set()


def main():
    chat = owner_chat()
    first = True
    for attempt in range(1, ATTEMPTS + 1):
        pid, fd, url = start()
        seen = {m.get("uid") for m in messages(chat)}
        text(chat, (FIRST if first else AGAIN).format(url=url))
        first = False
        log(f"link texted (attempt {attempt})")
        code, used = wait_for_code(chat, seen)
        if not code:
            os.kill(pid, 9)
            log("no code came back; a restart texts a new link")
            return 1
        # Typed, then Enter on its own: in one write the Enter lands inside the pasted text and the
        # code is never submitted.
        os.write(fd, code.encode())
        time.sleep(1)
        os.write(fd, b"\r")
        screen = read_until(fd, lambda b: b"Storethis" in ANSI.sub(b"", b).replace(b" ", b"")
                            or b"error" in ANSI.sub(b"", b).lower(), 90)
        os.kill(pid, 9)
        found = login_of(screen)
        if found:
            log("logged in")
            sys.stdout.write(found)
            return 0
        shown = re.sub(r"sk-ant-\S+", "sk-ant-<redacted>", ANSI.sub(b"", screen).decode(errors="replace"))
        log("that code did not log in: " + " ".join(shown.split())[-240:])
    text(chat, "I couldn't log in after a few tries. Restart me and I'll send a new link.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
