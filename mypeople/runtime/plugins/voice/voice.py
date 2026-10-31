#!/usr/bin/env python3
"""Voice plugin: what the owner says at their live mic reaches the Boss, and the Boss answers out loud.

Turn it on by writing state/voice/config.json; `mypeople up` then keeps it running:
  {"sse": "HOST:PORT",                  a live-caption server whose GET /events streams {"text": ...}
   "say": ["/path/to/speak", "--flag"], the command that speaks one line (the text is added last)
   "from": "Ana (studio mic)"}          who is talking, as the Boss is told

Usage:
  voice.py serve        follow the captions; each finished thought is one mp send to the Boss
  voice.py say "text"   speak a reply out loud (the command every voice message carries)
  voice.py status       print the config and the lines last spoken

Like plow-chat, each message is a package: the words, who said them, and the exact command that
answers in the same medium (spoken at the mic -> spoken back on the speaker). The mic is live, so
most of it is the owner talking to their stream or the room; the package says so, and the Boss
answers only what is meant for it.

A thought ships after SILENCE_MS without new words, or HARD_MS after it began. The caption text
grows, but the model can rewrite or trim it; text already sent is re-found in the new buffer so it
is never sent twice. The one filter: the speaker sits near the mic, so the Boss's own spoken reply
comes back in the captions. A thought whose word pairs are mostly pairs `say` spoke in the last
minute is dropped.
"""
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME") or Path(__file__).resolve().parents[2])
STATE_DIR = Path(os.environ.get("VOICE_STATE_DIR") or INSTALL / "state" / "voice")
CONFIG = STATE_DIR / "config.json"
SAID = STATE_DIR / "said.txt"   # the last lines `say` spoke, for the echo filter
HOST_ID = os.environ.get("HOST_ID") or os.uname().nodename.split(".")[0]
BOSS_AGENT = os.environ.get("BOSS_AGENT") or f"{HOST_ID}/main:Boss"
MP_BIN = os.environ.get("MP_BIN") or str(INSTALL / "bin" / "mp")
RUN = f"{sys.executable} {Path(__file__).resolve()}"   # the Boss answers with the python that runs the bridge
SILENCE_MS = 1500   # end of a thought: a pause shorter than this is mid-sentence
HARD_MS = 20000     # a thought that never pauses still ships
ECHO_S = 60         # how long a spoken reply can come back through the mic
MAX_CHARS = 2000


def log(msg: str):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] voice: {msg}", flush=True)


def config() -> dict:
    return json.loads(CONFIG.read_text())


def send_to_agent(message: str, agent: str = BOSS_AGENT) -> bool:
    try:
        r = subprocess.run([sys.executable, MP_BIN, "send", agent, message],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:   # 10-31: one hung send silenced the old bridge
        log(f"ERROR mp send: {e}")
        return False
    if r.returncode != 0:
        log(f"ERROR mp send rc={r.returncode}: {r.stderr.strip()[:200]}")
        return False
    return True


def envelope(who: str, text: str) -> str:
    return (f"[voice] from {who}: {text}\n"
            f"(This is the owner's live mic, transcribed as they speak. Most of it is them talking to their "
            f"stream, the room or other agents, not to you: answer only when they are talking to you, and "
            f"otherwise do nothing. To answer out loud, run: {RUN} say \"your reply\" — it plays on the "
            f"speaker next to them. Short spoken sentences in the language they used, no markdown.)")


def _pairs(s: str) -> list:
    w = re.findall(r"\w+", s.lower())
    return list(zip(w, w[1:]))


def is_echo(line: str) -> bool:
    """Most of this line's word pairs were just spoken by `say`. Pairs, not words: filler words alone
    (que, é, eu, do) made one of the owner's own questions look like an echo."""
    try:
        if time.time() - SAID.stat().st_mtime > ECHO_S:
            return False
        said = set(_pairs(SAID.read_text().replace("\n", " ")))
    except OSError:
        return False
    p = _pairs(line)
    return len(p) >= 2 and sum(x in said for x in p) >= 0.5 * len(p)


def refind(sent: str, t: str) -> int:
    """Where already-sent text ends in a rewritten or trimmed caption buffer."""
    if not sent or not t:
        return 0
    if t.startswith(sent):
        return len(sent)
    pref = len(os.path.commonprefix([sent, t]))
    if pref >= max(24, len(sent) // 3):
        return pref
    for n in (120, 80, 40, 24):
        n = min(n, len(sent))
        if n < 16:
            break
        i = t.rfind(sent[-n:])
        if i >= 0:
            return i + n
    words = sent.split()
    for k in range(min(10, len(words)), 2, -1):
        phrase = " ".join(words[-k:])
        i = t.rfind(phrase) if len(phrase) >= 12 else -1
        if i >= 0:
            return i + len(phrase)
    return pref


def trim_repeat(line: str, last: str) -> str:
    """What is left of a thought once a repeat of the previous one is taken out."""
    if line == last:
        return ""
    if last and len(line) > 40:
        if (last in line and len(last) >= 0.7 * len(line)) or (line in last and len(line) >= 0.7 * len(last)):
            return ""
        tail = last[-max(24, int(len(last) * 0.6)):]
        if len(tail) >= 24 and line.startswith(tail):
            return line[len(tail):].lstrip()
    return line


class Thoughts:
    """Turns a growing caption buffer into finished thoughts, each one handed out once."""

    def __init__(self):
        self.text = None    # buffer as last seen; None until the first event, whose backlog is skipped
        self.cut = 0        # text[:cut] is sent (or was backlog)
        self.sent = ""      # that sent prefix, to re-find after a rewrite
        self.changed = self.began = None

    def update(self, t: str, now: float):
        if self.text is None:
            self.text, self.cut, self.sent = t, len(t), t
            return
        if t == self.text:
            return
        if not t.startswith(self.text[:self.cut]):
            self.cut = min(refind(self.sent, t), len(t))
            self.sent = t[:self.cut]
        self.text, self.changed = t, now
        if self.began is None and t[self.cut:].strip():
            self.began = now

    def due(self, now: float):
        if self.changed is None:
            return None
        pending = self.text[self.cut:].strip()
        if not pending:
            self.changed = self.began = None
            return None
        if (now - self.changed) * 1000 < SILENCE_MS and (now - self.began) * 1000 < HARD_MS:
            return None
        self.cut, self.sent = len(self.text), self.text
        self.changed = self.began = None
        return pending


def deliver(line: str, who: str, last: list):
    line = trim_repeat(" ".join(line.split()), last[0])[-MAX_CHARS:].strip()
    if not line:
        return
    if is_echo(line):
        log(f"echo dropped: {line[:80]!r}")
        return
    last[0] = line
    if send_to_agent(envelope(who, line)):
        log(f"-> {BOSS_AGENT}: {line[:80]!r}")


def events(host: str):
    h, p = host.rsplit(":", 1)
    c = http.client.HTTPConnection(h, int(p), timeout=30)
    c.request("GET", "/events", headers={"Accept": "text/event-stream"})
    r, buf = c.getresponse(), b""
    while chunk := r.read1(4096):
        buf += chunk
        while b"\n\n" in buf:
            raw, buf = buf.split(b"\n\n", 1)
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data: "):
                yield json.loads(line[6:]).get("text") or ""


def serve():
    cfg = config()
    who = cfg.get("from") or "the owner (live mic)"
    thoughts, lock, last = Thoughts(), threading.Lock(), [""]

    def shipper():
        while True:
            time.sleep(0.05)
            try:   # nothing that goes wrong with one thought may stop the next ones
                with lock:
                    line = thoughts.due(time.time())
                if line:
                    deliver(line, who, last)
            except Exception as e:  # noqa: BLE001
                log(f"ERROR ship: {type(e).__name__}: {e}")

    threading.Thread(target=shipper, daemon=True).start()
    log(f"up: captions {cfg['sse']} -> {BOSS_AGENT}")
    while True:
        try:
            for t in events(cfg["sse"]):
                with lock:
                    thoughts.update(t, time.time())
        except Exception as e:  # noqa: BLE001 - the caption server restarts; so do we
            log(f"captions dropped ({type(e).__name__}: {e}); retry in 3 s")
        with lock:
            thoughts.__init__()   # a new connection replays the buffer: skip it as backlog
        time.sleep(3)


def say(text: str) -> int:
    text = " ".join(text.split())
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    kept = SAID.read_text().splitlines()[-2:] if SAID.exists() else []
    SAID.write_text("\n".join(kept + [text]) + "\n")   # before speaking: its echo arrives mid-playback
    return subprocess.run(config()["say"] + [text]).returncode


def main(argv):
    if argv[:1] == ["serve"]:
        serve()
    elif argv[:1] == ["say"] and len(argv) > 1:
        return say(" ".join(argv[1:]))
    elif argv[:1] == ["status"]:
        print(json.dumps({"config": json.loads(CONFIG.read_text()) if CONFIG.exists() else None,
                          "said": SAID.read_text().splitlines() if SAID.exists() else []}, indent=2))
    else:
        print(__doc__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
