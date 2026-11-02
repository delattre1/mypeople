#!/usr/bin/env python3
"""Voice plugin: what the owner says at their live mic reaches the Boss, and the Boss answers out loud.

Turn it on by writing state/voice/config.json; `mypeople up` then keeps it running:
  {"sse": "HOST:PORT",                  a live-caption server whose GET /events streams {"text": ...}
   "say": ["/path/to/speak", "--flag"], the command that speaks one line (the text is added last)
   "from": "Ana (studio mic)",          who is talking, as the Boss is told
   "cue": "\"hey Boss\""}               optional: how the owner addresses the Boss; lines without it are left alone

Usage:
  voice.py serve        follow the captions; each finished thought is one mp send to the Boss
  voice.py say "text"   speak a reply out loud (the command every voice message carries)
  voice.py status       print the config and the lines last spoken

Like plow-chat, each message is a package: the words, who said them, and the exact command that
answers in the same medium (spoken at the mic -> spoken back on the speaker). The mic is live, so
most of it is the owner talking to their stream or the room; the package says so, and the Boss
answers only what is meant for it.

A thought ships once his sentence ends ('.?!' and SILENCE_MS without new words), after LONG_PAUSE_MS of silence
mid-sentence, or HARD_MS after it began, then cut at his longest pause so the next line starts where he took a breath;
it never starts mid-word. The caption text
grows, but the model can rewrite or trim it; text already sent is re-found in the new buffer so it
is never sent twice. The one filter: the speaker sits near the mic, so the Boss's own spoken reply
comes back in the captions. What arrives while `say` plays (and TAIL_S after) is skipped, and a
later thought whose word pairs are mostly pairs `say` spoke in the last minute is dropped.
"""
import difflib
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
SPEAKING = STATE_DIR / "speaking"   # "<start> <end>" of the last `say`; no end yet while it plays
HOST_ID = os.environ.get("HOST_ID") or os.uname().nodename.split(".")[0]
BOSS_AGENT = os.environ.get("BOSS_AGENT") or f"{HOST_ID}/main:Boss"
MP_BIN = os.environ.get("MP_BIN") or str(INSTALL / "bin" / "mp")
RUN = f"{sys.executable} {Path(__file__).resolve()}"   # the Boss answers with the python that runs the bridge
SILENCE_MS = 1500   # a finished sentence ('.', '?', '!') ships after this pause
LONG_PAUSE_MS = 3500   # an unfinished one waits for a longer pause: 11-01 a 1.5 s pause mid-sentence (even mid-word:
                       # "me" ... "lhor") shipped half a thought, and Daniel got cut-up lines all day
HARD_MS = 30000     # a thought that never pauses still ships
CUT_PAUSE_S = 0.4   # at the cap, a gap in the words this long is a place to cut; shorter is just the model's pace
CUT_MIN_WORDS = 4   # never leave a stub of a line at the cap
SENTENCE_END = re.compile(r"[.?!…][\"')\]]*\s*$")
ECHO_S = 60         # how long a spoken reply can come back through the mic
TAIL_S = 2.5        # the captions lag the speaker: its echo keeps arriving this long after it stops. Used only to
                    # hold a line until the echo is complete so it can be stripped; the mic is never muted (11-01)
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


def envelope(who: str, text: str, cue: str = "") -> str:
    when = (f"They address you by saying {cue} (or close to it: the transcript garbles names); a line "
            f"without that is them talking to someone else, so do nothing." if cue else
            "Answer only when they are talking to you, and otherwise do nothing.")
    return (f"[voice] from {who}: {text}\n"
            f"(This is the owner's live mic, transcribed as they speak. Most of it is them talking to their "
            f"stream, the room or other agents, not to you. {when} To answer out loud, run: {RUN} say "
            f"\"your reply\" — it plays on the speaker next to them. Short spoken sentences in the language "
            f"they used, no markdown.)")


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
    return len(p) >= 2 and sum(x in said for x in p) >= 0.4 * len(p)   # garbled echoes kept ~48%; his own lines ~6%


def strip_echo(line: str) -> str:
    """The line minus the words `say` just spoke (11-01: the mic stays open while the speaker plays, so his words
    and the speaker's echo arrive in one buffer). A word goes when most of its letters line up, in order, with the
    recently spoken text (the captions garble it: 'Arrumado' comes back as 'o Madu')."""
    try:
        if time.time() - SAID.stat().st_mtime > ECHO_S:
            return line
        said = SAID.read_text()
    except OSError:
        return line
    words = line.split()
    norm = [re.sub(r"\W", "", w.lower()) for w in words]
    joined, spans, pos = "", [], 0
    for w in norm:
        spans.append((pos, pos + len(w)))
        joined += w + " "
        pos += len(w) + 1
    ref = " ".join(re.sub(r"\W", "", w.lower()) for w in said.split())
    covered = set()
    for b in difflib.SequenceMatcher(None, joined, ref, autojunk=False).get_matching_blocks():
        if b.size >= 6:   # a run of the speaker's text, not a short word ('vou', 'pra') that happens to match
            covered.update(range(b.a, b.a + b.size))
    keep = [w for w, n, (a, z) in zip(words, norm, spans) if not n or sum(i in covered for i in range(a, z)) < .5 * (z - a)]
    return " ".join(keep)


def speaking(now: float) -> bool:
    """While `say` plays and TAIL_S after, what reaches the captions is mostly the speaker. 10-31: an echo
    mixed into the owner's words, or garbled past the pair filter, reached the Boss as the owner's speech."""
    try:
        start, _, end = SPEAKING.read_text().partition(" ")
        start, end = float(start), (float(end) if end.strip() else now)
    except (OSError, ValueError):
        return False
    return start <= now <= end + TAIL_S and now - start < 90   # a `say` that died never mutes for long


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
        self.held = ""      # the owner's words from before the speaker began, still to send
        self.ready = ""     # a finished thought to send at once (the speaker began talking)
        self.before_speaker = False
        self.overlap = False     # the line being built heard the speaker too: strip its words before sending
        self.was_overlap = False  # that flag for the line due() just returned
        self.in_speaker = False
        self.changed = self.began = None
        self.marks = []     # (when, buffer length) at each caption change since the line began: where his pauses are

    def update(self, t: str, now: float, muted: bool = False):
        if self.text is None:
            self.text, self.cut, self.sent = t, len(t), t
            return
        if t == self.text:
            return
        if muted and not self.in_speaker:   # the speaker starts: what he said before it is a finished thought
            # 11-01: held words used to wait and ship glued to the speaker's echo and were dropped with it.
            pend = (self.held + " " + self.text[self.cut:]).strip()
            if re.search(r"\w", pend):
                self.ready = (self.ready + " " + pend).strip()
            self.held = ""
            self.cut, self.sent = len(self.text), self.text
            self.changed = self.began = None
            self.marks = []
        self.in_speaker = muted
        if muted:   # 11-01 (Daniel): the mic stays open while the speaker plays; its words are stripped at send
            self.overlap = True
        if not t.startswith(self.text[:self.cut]):
            self.cut = min(refind(self.sent, t), len(t))
            self.sent = t[:self.cut]
            self.marks = []   # the buffer moved: lengths no longer point at the same words
        self.marks.append((now, len(self.text)))   # the pause, if any, ended here: the words before it end at this length
        self.text, self.changed = t, now
        if self.began is None and t[self.cut:].strip():
            self.began = now

    def due(self, now: float):
        self.before_speaker = bool(self.ready)   # said before the speaker began: it cannot be the speaker's echo
        self.was_overlap = False
        if self.ready:
            r, self.ready = self.ready.lstrip(",.;:?!… "), ""
            return r or None
        if self.changed is None:
            return None
        start = self.cut
        while 0 < start < len(self.text) and self.text[start - 1].isalnum() and self.text[start].isalnum():
            start -= 1   # the model finished a word we already sent half of ("me" -> "melhor"): send the whole word
        pending = (self.held + " " + self.text[start:]).strip()
        pending = pending.lstrip(",.;:?!… ")   # punctuation the model added to sent text is not a new thought
        if not re.search(r"\w", pending):
            self.cut, self.sent = len(self.text), self.text   # only punctuation changed: nothing to send
            if not self.held:
                self.changed = self.began = None
            return None
        quiet = (now - self.changed) * 1000
        wait = SILENCE_MS if SENTENCE_END.search(pending) else LONG_PAUSE_MS
        if self.in_speaker:
            wait = max(wait, TAIL_S * 1000)   # let the speaker's echo finish arriving, so all of it gets stripped
        if quiet < wait and (now - self.began) * 1000 < HARD_MS:
            return None
        split = self.longest_pause(start) if quiet < wait else None   # at the cap he's mid-speech: cut where he breathed
        self.was_overlap, self.overlap = self.overlap, False
        if split:
            line = (self.held + " " + self.text[start:split]).strip().lstrip(",.;:?!… ")
            self.cut, self.sent, self.held = split, self.text[:split], ""
            self.began = next((w for w, n in self.marks if n > split), now)
            self.marks = [m for m in self.marks if m[1] >= split]
            return line
        self.cut, self.sent, self.held = len(self.text), self.text, ""
        self.changed = self.began = None
        self.marks = []
        return pending

    def longest_pause(self, start: int):
        """Where to cut a line at the cap: the end of the words before his longest pause, if it's at a word boundary."""
        best, best_gap = None, CUT_PAUSE_S
        for (a, _), (b, n) in zip(self.marks, self.marks[1:]):
            enough = n < len(self.text) and len(self.text[start:n].split()) >= CUT_MIN_WORDS
            if b - a >= best_gap and enough and not (self.text[n - 1].isalnum() and self.text[n].isalnum()):
                best, best_gap = n, b - a
        return best


def deliver(line: str, who: str, last: list, cue: str = "", before_speaker: bool = False, overlap: bool = False):
    line = trim_repeat(" ".join(line.split()), last[0])[-MAX_CHARS:].strip()
    if overlap:   # heard while the speaker played: keep his words, drop the speaker's
        kept = strip_echo(line)
        if len(re.findall(r"\w+", kept)) < 2:
            log(f"echo stripped: {line[:80]!r}")
            return
        line = kept
    if not line:
        return
    if not (before_speaker or overlap) and is_echo(line):   # 11-01: his words often repeat the reply that follows them
        log(f"echo dropped: {line[:80]!r}")
        return
    last[0] = line
    if send_to_agent(envelope(who, line, cue)):
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
    who, cue = cfg.get("from") or "the owner (live mic)", cfg.get("cue") or ""
    thoughts, lock, last = Thoughts(), threading.Lock(), [""]

    def shipper():
        while True:
            time.sleep(0.05)
            try:   # nothing that goes wrong with one thought may stop the next ones
                with lock:
                    now = time.time()
                    thoughts.in_speaker = speaking(now)   # also between caption events
                    line = thoughts.due(now)
                    before, overlap = thoughts.before_speaker, thoughts.was_overlap
                if line:
                    deliver(line, who, last, cue, before, overlap)
            except Exception as e:  # noqa: BLE001
                log(f"ERROR ship: {type(e).__name__}: {e}")

    threading.Thread(target=shipper, daemon=True).start()
    log(f"up: captions {cfg['sse']} -> {BOSS_AGENT}")
    while True:
        try:
            for t in events(cfg["sse"]):
                now = time.time()
                with lock:
                    thoughts.update(t, now, muted=speaking(now))
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
    start = time.time()
    SPEAKING.write_text(f"{start} ")
    try:
        return subprocess.run(config()["say"] + [text]).returncode   # returns once playback ends
    finally:
        SPEAKING.write_text(f"{start} {time.time()}")


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
