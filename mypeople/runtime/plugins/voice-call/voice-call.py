#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=13"]
# ///
"""Voice Call plugin: the owner phones his Boss and talks to it, live.

A bridge, like plow-chat: the Boss is the brain on every turn, reached through
mp. The plugin only carries the voice. The owner's Plow line forwards its calls
to a Twilio number; Twilio asks this plugin what to do. A caller on
VOICE_CALL_ALLOW who keys the PIN gets a two-way audio stream; anyone else hears
one sentence and is hung up on, and nothing about them is recorded,
transcribed or logged except that a call was rejected. Twilio's record of their
call is deleted once it ends.

Twilio's webhooks carry a secret in their URL (HTTP Basic auth, the
credentials only Twilio's copy of the number's config holds), and a right PIN
opens the voice only after Twilio's own API confirms that CallSid is a live
call to our number from that caller. The audio stream is let in once, with a
one-time token from that answer.

On the call: his speech goes to streaming speech-to-text (Deepgram Flux), each
finished turn goes to the Boss as `[CALL t7] ...`, and the Boss answers with
`voice-call.py say t7 "..."`, which is spoken (ElevenLabs) at his next pause,
never over him, or sent by iMessage (the plow-chat plugin) once he hangs up.
While the Boss works he hears a short "Hm" at once, "one sec" at 8 s, "still on
it" at 30 s and an offer to text it at 2 min. If he talks over the voice, it
stops.

His calls are never recorded and nothing he says is logged; the server logs
per-turn latency only. The one exception is a synthetic test call: our own
Twilio number calling during a test window (`voice-call.py test-window 30`),
which expires by itself after at most an hour. Only those are recorded, because
the voice on them is ours.

Turn it on with VOICE_CALL=1, VOICE_CALL_ALLOW=+1555... and VOICE_CALL_PUBLIC_URL
in queue.env, point a tailscale funnel's /voice/twilio at VOICE_CALL_PORT and
/voice/stream at VOICE_CALL_PORT+1 (only those two: `say` listens on PORT+2,
which nothing serves), then once:
  voice-call.py keys     store the Twilio account SID + API key, and the Deepgram and ElevenLabs keys
  voice-call.py setup    point the Twilio number here and make the PIN. With no number yet it
                         stops and prints which to buy and the price: a purchase needs the
                         owner's yes, and only `setup --buy +1...` after that yes buys it

Usage:
  voice-call.py serve                 run the bridge (supervise.sh entry point)
  voice-call.py say t3 "text"         speak into the call that asked t3 (iMessage if it ended)
  voice-call.py test-window MIN       let our own Twilio number in for MIN minutes (max 60, 0 closes)
  voice-call.py latency rec.wav       reply gaps in a dual-channel call recording
"""
import array
import base64
import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME")
               or Path.home() / "mypeople")
STATE_DIR = Path(os.environ.get("VOICE_CALL_STATE_DIR") or INSTALL / "state" / "voice-call")
CONFIG = STATE_DIR / "config.json"  # 600: keys and PIN, written by keys/setup
CLIPS_DIR = STATE_DIR / "clips"      # the short canned lines, rendered once in the call's voice
PORT = int(os.environ.get("VOICE_CALL_PORT", "8796"))  # Twilio webhooks; PORT+1 is the media stream
SAY_PORT = PORT + 2  # `say` from the Boss: its own port, so no Funnel on PORT can ever publish it
PUBLIC_URL = os.environ.get("VOICE_CALL_PUBLIC_URL", "").rstrip("/")
ALLOW = {n.strip() for n in os.environ.get("VOICE_CALL_ALLOW", "").split(",") if n.strip()}
OWNER = os.environ.get("VOICE_CALL_OWNER", "Daniel")
LANG = os.environ.get("VOICE_CALL_LANG", "pt")  # his language until his first turn says otherwise
TTS_MODEL = os.environ.get("VOICE_CALL_TTS_MODEL", "eleven_v4_turbo")
STT_MODEL = os.environ.get("VOICE_CALL_STT_MODEL", "flux-general-multi")
TEST_WINDOW_MAX_MIN = 60  # a test number left on the allowlist is a back door: it expires by itself
AREA_CODE = os.environ.get("VOICE_CALL_AREA_CODE", "650")
HOST_ID = os.environ.get("HOST_ID") or os.uname().nodename.split(".")[0]
BOSS_AGENT = os.environ.get("BOSS_AGENT") or f"{HOST_ID}/main:Boss"
MP_BIN = os.environ.get("MP_BIN") or str(INSTALL / "bin" / "mp")
SELF = str(Path(__file__).resolve())
PLOW_CHAT = str(Path(__file__).resolve().parent.parent / "plow-chat" / "plow-chat.py")
NUDGES = ((8, "one_sec"), (30, "still_on_it"), (120, "offer_text"))  # seconds without a Boss word
CLIPS = {
    "hello": {"pt": f"Oi, {OWNER}.", "en": f"Hi, {OWNER}."},
    "ack": {"pt": "Hum, tá.", "en": "Mm, okay."},
    "one_sec": {"pt": "Só um segundo.", "en": "One sec."},
    "still_on_it": {"pt": "Ainda tô nisso, já te falo.", "en": "Still on it, I'll tell you as soon as I have it."},
    "offer_text": {"pt": "Isso vai demorar. Quer ficar na linha, ou te mando por mensagem?",
                   "en": "This one's slow. Stay on, or should I text you the answer?"},
}


def log(msg: str):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] voice-call: {msg}", flush=True)


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    # Holds API keys and the PIN: 600 from the first byte, never chmod after.
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(cfg, f)
    os.replace(tmp, CONFIG)


def http(req: urllib.request.Request, timeout=20):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def twilio(cfg, method, path, form=None):
    data = urllib.parse.urlencode(form).encode() if form else None
    url = path if path.startswith("https://") else f"https://api.twilio.com/2010-04-01/Accounts/{cfg['twilio_sid']}{path}"
    req = urllib.request.Request(url, data=data, method=method)
    # An API key, not the account's Auth Token: it can be revoked alone, and it is the one we hold.
    auth = base64.b64encode(f"{cfg['twilio_key']}:{cfg['twilio_secret']}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    return http(req)


# --- who may call: checked at Twilio, before a second of his audio leaves Twilio ---

# Twilio signs webhooks with the Auth Token alone, which we do not hold. So each webhook URL carries
# Basic auth credentials only Twilio's config of our number has (Twilio sends them after a 401), and
# a grant also needs Twilio's API to vouch for the call.
def hook_base(cfg) -> str:
    return PUBLIC_URL.replace("https://", f"https://twilio:{cfg['hook_secret']}@", 1)


def hook_auth_ok(header: str, cfg) -> bool:
    want = "Basic " + base64.b64encode(f"twilio:{cfg['hook_secret']}".encode()).decode()
    return bool(cfg.get("hook_secret")) and hmac.compare_digest((header or "").encode(), want.encode())


def live_call(cfg, sid: str, caller: str) -> bool:
    """Twilio's own record: a call on our account, ringing or up, to our number, from this caller."""
    code, c = twilio(cfg, "GET", f"/Calls/{urllib.parse.quote(sid)}.json")
    return (code == 200 and c.get("account_sid") == cfg["twilio_sid"] and c.get("to") == cfg["twilio_number"]
            and c.get("from") == caller and c.get("status") in ("ringing", "in-progress"))


REJECTED = set()  # CallSids of strangers, deleted from Twilio's log once they end
ATTEMPTS = {}     # CallSid -> wrong PINs so far
GRANTS = {}       # one-time media-stream token -> (expires, call_sid, is_test)


def twiml(*verbs) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(verbs) + "</Response>"


def gather(cfg, before="") -> str:
    # No prompt: the owner's contact dials the PIN by itself (number,,PIN) right after pickup.
    return twiml(before, f'<Gather input="dtmf" numDigits="{len(cfg["pin"])}" timeout="8" '
                         f'actionOnEmptyResult="true" action="{hook_base(cfg)}/voice/twilio/pin"/>')


def test_call(p: dict, cfg: dict, now=None) -> bool:
    """Our own Twilio number calling inside an open test window: a synthetic call, never a person."""
    return (bool(cfg.get("twilio_number")) and p.get("From") == cfg["twilio_number"]
            and (now or time.time()) < cfg.get("test_until", 0))


def on_twilio(path: str, p: dict, cfg: dict) -> str:
    sid = p.get("CallSid", "")
    if path == "/voice/twilio":
        if p.get("From") not in ALLOW and not test_call(p, cfg):
            REJECTED.add(sid)
            log("rejected a call")  # deliberately nothing else: not the number, not a second of audio
            return twiml("<Say>This line takes texts only. Goodbye.</Say>", "<Hangup/>")
        if test_call(p, cfg):
            log(f"synthetic test call {sid} from our own number (test window open)")
        return gather(cfg)
    if path == "/voice/twilio/pin":
        if hmac.compare_digest(p.get("Digits", ""), cfg["pin"]):
            ATTEMPTS.pop(sid, None)
            if not ((p.get("From") in ALLOW or test_call(p, cfg)) and live_call(cfg, sid, p.get("From"))):
                log(f"call {sid}: right PIN but Twilio does not vouch for this call; hung up")
                return twiml("<Hangup/>")
            token = secrets.token_urlsafe(24)
            GRANTS[token] = (time.time() + 60, sid, test_call(p, cfg))
            log(f"call {sid}: PIN ok, opening the voice stream")
            stream = PUBLIC_URL.replace("https://", "wss://", 1) + "/voice/stream"
            return twiml(f'<Connect><Stream url="{stream}"><Parameter name="token" value="{token}"/>'
                         f"</Stream></Connect>")
        ATTEMPTS[sid] = ATTEMPTS.get(sid, 0) + 1
        log(f"call {sid}: wrong PIN ({ATTEMPTS[sid]}/3)")
        if ATTEMPTS[sid] < 3:
            return gather(cfg, "<Say>Try again.</Say>")
        ATTEMPTS.pop(sid, None)
        threading.Thread(target=text_owner, daemon=True, args=(
            f"Someone called the Boss line from {p.get('From')} and got the PIN wrong 3 times. Hung up.",)).start()
        return twiml("<Hangup/>")
    if path == "/voice/twilio/status":
        if sid in REJECTED and p.get("CallStatus") in ("completed", "busy", "failed", "no-answer", "canceled"):
            REJECTED.discard(sid)
            threading.Thread(target=scrub, args=(cfg, sid), daemon=True).start()
        return twiml()
    return twiml("<Hangup/>")


def take_grant(token: str, now=None):
    """A media stream is let in once, within a minute of a right PIN, and only with its token."""
    grant = GRANTS.pop(token, None)
    return grant if grant and grant[0] > (now or time.time()) else None


def scrub(cfg, sid):
    for _ in range(3):
        code, _ = twilio(cfg, "DELETE", f"/Calls/{sid}.json")
        if code in (204, 404):
            return
        time.sleep(30)
    log("ERROR could not delete a rejected call's record at Twilio")


# --- the Boss ---

def send_to_boss(message: str) -> bool:
    try:
        r = subprocess.run([sys.executable, MP_BIN, "send", BOSS_AGENT, message],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"ERROR mp send: {e}")
        return False
    if r.returncode != 0:
        log(f"ERROR mp send rc={r.returncode}: {r.stderr.strip()[:200]}")
    return r.returncode == 0


def envelope(ticket: str, lang: str, text: str, interrupted: bool, test=False) -> str:
    cut = " (He cut off your last reply to say this.)" if interrupted else ""
    who = (f"SYNTHETIC TEST CALL from our own number, not {OWNER}: answer it like a real call, act on nothing"
           if test else f"{OWNER}, live on the phone")
    return (f'[CALL {ticket}] {who} ({lang}): "{text}"{cut}\n'
            f"(Answer with: python3 {SELF} say {ticket} \"...\" — he hears it within a second. One or two "
            f"spoken sentences in his language, no markdown or links. He is waiting on the line, so answer "
            f"first and work after; for longer work say a short ack now and the result later with the same "
            f"command. If the call has ended it reaches him by iMessage.)")


def text_owner(text: str) -> bool:
    r = subprocess.run([sys.executable, PLOW_CHAT, "reply", text], capture_output=True, text=True, timeout=60)
    return r.returncode == 0


# --- one live call ---

TICKETS, LOCK = {}, threading.Lock()
COUNTER = iter(range(1, 1 << 62))


class Call:
    """Who talks when, on one call. pump() runs on its own thread; the rest arrive from the
    media stream, the speech-to-text stream and `say`. The voice never talks over him."""

    def __init__(self, play, clear, boss=send_to_boss, now=time.monotonic, test=False):
        self.play, self.clear, self.boss, self.now = play, clear, boss, now
        self.lock = threading.Lock()
        self.lines = queue.SimpleQueue()   # (ticket, text) from the Boss, waiting for a pause
        self.user_talking = self.speaking = self.interrupted = False
        self.lang, self.live, self.test = LANG, True, test
        self.waiting = None                # [ticket, his turn ended at, nudges given] until the Boss speaks
        self.said_at = {}                  # ticket -> when the Boss's line arrived, for the latency log
        self.queued = ["hello"]            # canned lines to play at the next pause

    def user_started(self):
        with self.lock:
            self.user_talking = True
            if self.speaking:              # he talks over the voice: it stops at once
                self.speaking, self.interrupted = False, True
                self.clear()

    def user_ended(self, text: str, lang: str = ""):
        with self.lock:
            self.user_talking = False
            if not text.strip():
                return
            self.lang = lang if lang in ("pt", "en") else self.lang
            ticket, cut, self.interrupted = f"t{next(COUNTER)}", self.interrupted, False
            self.waiting = [ticket, self.now(), 0]
            self.queued.append("ack")
        with LOCK:
            TICKETS[ticket] = self
        threading.Thread(target=self.boss, args=(envelope(ticket, self.lang, text, cut, self.test),), daemon=True).start()
        log(f"{ticket} sent to the Boss")

    def say_later(self, ticket: str, text: str):
        self.said_at[ticket] = self.now()
        self.lines.put((ticket, text))

    def played(self):
        with self.lock:
            self.speaking = False

    def pump(self):
        with self.lock:
            if self.user_talking or self.speaking or not self.live:
                return
            try:
                ticket, text = self.lines.get_nowait()
            except queue.Empty:
                ticket = None
            if ticket:
                if self.waiting and self.waiting[0] == ticket:
                    boss_s = self.said_at.get(ticket, self.now()) - self.waiting[1]
                    log(f"{ticket}: Boss answered {boss_s:.1f}s after his last word")
                    self.waiting = None
                self.queued.clear()  # an answer beats any "one sec"
                self.speaking = True
                return self.play("text", text, self.lang, ticket)
            if self.waiting:
                age, given = self.now() - self.waiting[1], self.waiting[2]
                if given < len(NUDGES) and age >= NUDGES[given][0]:
                    self.waiting[2] += 1
                    self.queued.append(NUDGES[given][1])
            if self.queued:
                self.speaking = True
                return self.play("clip", self.queued.pop(0), self.lang, None)


def tts(cfg, text: str):
    """ElevenLabs, straight to Twilio's 8 kHz mu-law, chunk by chunk as it is generated."""
    from websockets.sync.client import connect
    # ponytail: one socket per reply costs a TLS handshake (~0.1 s); a socket kept open per call removes it.
    url = (f"wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input?model_id={TTS_MODEL}"
           f"&output_format=ulaw_8000")
    with connect(url, additional_headers={"xi-api-key": cfg["tts_key"]}) as ws:
        # the dialogue socket refuses a first message that does not register its voices
        ws.send(json.dumps({"voices": [cfg["voice_id"]], "inputs": [{"text": text, "voice_id": cfg["voice_id"]}]}))
        ws.send(json.dumps({"close_socket": True}))
        for raw in ws:
            m = json.loads(raw)
            if m.get("audio"):
                yield base64.b64decode(m["audio"])
            if m.get("is_final"):
                return


def clip(cfg, name: str, lang: str) -> bytes:
    path = CLIPS_DIR / f"{cfg['voice_id']}-{name}-{lang}.ulaw"
    if not path.exists():
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(tts(cfg, CLIPS[name][lang])))
    return path.read_bytes()


class Stream:
    """One Twilio media stream: his audio to speech-to-text, the voice back to him."""

    def __init__(self, ws, cfg):
        self.ws, self.cfg, self.sid = ws, cfg, None
        self.send_lock, self.mark = threading.Lock(), 0
        self.call = None
        self.gen = 0  # bumped by every interruption: a line from an older gen stops sending
        self.first_audio = {}

    def to_twilio(self, msg: dict):
        with self.send_lock:
            self.ws.send(json.dumps(dict(msg, streamSid=self.sid)))

    def clear(self):
        self.gen += 1
        self.to_twilio({"event": "clear"})

    def play(self, kind, payload, lang, ticket):
        threading.Thread(target=self._play, args=(kind, payload, lang, ticket, self.gen), daemon=True).start()

    def _play(self, kind, payload, lang, ticket, gen):
        try:
            chunks = [clip(self.cfg, payload, lang)] if kind == "clip" else tts(self.cfg, payload)
            for chunk in chunks:
                if gen != self.gen:
                    return
                if ticket and ticket not in self.first_audio:
                    self.first_audio[ticket] = time.monotonic()
                    said = self.call.said_at.get(ticket)
                    if said:
                        log(f"{ticket}: voice started {self.first_audio[ticket] - said:.2f}s after the Boss answered")
                self.to_twilio({"event": "media", "media": {"payload": base64.b64encode(chunk).decode()}})
            self.mark += 1
            self.to_twilio({"event": "mark", "mark": {"name": f"m{self.mark}"}})
        except Exception as e:  # a failed line must not hang the call
            log(f"ERROR speaking: {e!r}")
            self.call.played()

    def run(self):
        from websockets.sync.client import connect
        stt = None
        try:
            for raw in self.ws:
                m = json.loads(raw)
                ev = m.get("event")
                if ev == "start":
                    st = m["start"]
                    grant = take_grant((st.get("customParameters") or {}).get("token", ""))
                    if not grant or st.get("callSid") != grant[1] or st.get("accountSid") != self.cfg["twilio_sid"]:
                        log("a media stream without a valid token: closed")
                        return
                    self.sid = st["streamSid"]
                    if grant[2]:  # synthetic test call: the only kind ever recorded
                        threading.Thread(target=twilio, daemon=True, args=(
                            self.cfg, "POST", f"/Calls/{grant[1]}/Recordings.json", {"RecordingChannels": "dual"})).start()
                    q = urllib.parse.urlencode([("model", STT_MODEL), ("encoding", "mulaw"), ("sample_rate", 8000),
                                                ("language_hint", "pt"), ("language_hint", "en")])  # unhinted, it heard pt as Dutch
                    stt = connect(f"wss://api.deepgram.com/v2/listen?{q}",
                                  additional_headers={"Authorization": f"Token {self.cfg['stt_key']}"})
                    self.call = Call(self.play, self.clear, test=grant[2])
                    threading.Thread(target=self.listen, args=(stt,), daemon=True).start()
                    threading.Thread(target=self.pace, daemon=True).start()
                    buf = b""
                    log(f"call {grant[1]} joined")
                elif ev == "media" and stt:
                    buf += base64.b64decode(m["media"]["payload"])
                    if len(buf) >= 640:  # Flux wants ~80 ms per message; Twilio sends 20 ms
                        stt.send(buf)
                        buf = b""
                elif ev == "mark" and self.call:
                    self.call.played()
                elif ev == "stop":
                    return
        finally:
            if self.call:
                self.call.live = False
            if stt:
                try:
                    stt.send(json.dumps({"type": "CloseStream"}))
                    stt.close()
                except Exception:
                    pass
            log("call ended")

    def listen(self, stt):
        try:
            for raw in stt:
                m = json.loads(raw)
                if m.get("type") != "TurnInfo":
                    continue
                if m.get("event") in ("StartOfTurn", "TurnResumed"):
                    self.call.user_started()
                elif m.get("event") == "EndOfTurn":
                    self.call.user_ended(m.get("transcript", ""), ((m.get("languages") or [""])[0])[:2])
        except Exception as e:
            if self.call.live:
                log(f"ERROR speech-to-text: {e!r}")

    def pace(self):
        while self.call.live:
            self.call.pump()
            time.sleep(0.05)


# --- servers ---

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path, cfg, port = self.path.split("?")[0], load_config(), self.server.server_address[1]
        if port == PORT and path.startswith("/voice/twilio"):
            if not hook_auth_ok(self.headers.get("Authorization"), cfg):
                return self.reply(401, headers={"WWW-Authenticate": 'Basic realm="voice-call"'})
            params = dict(urllib.parse.parse_qsl(body.decode(), keep_blank_values=True))
            return self.reply(200, on_twilio(path, params, cfg), "text/xml")
        if (port == SAY_PORT and path == "/voice/say"
                and hmac.compare_digest(self.headers.get("X-Say-Token") or "", cfg["say_token"])):
            req = json.loads(body)
            call = TICKETS.get(req.get("ticket"))
            if call and call.live:
                call.say_later(req["ticket"], req["text"])
                log(f"{req['ticket']} queued for his next pause")
                return self.reply(200, '{"result": "queued"}', "application/json")
            # a late answer to a synthetic call is ours to drop: it must never iMessage him
            return self.reply(200, '{"result": "%s"}' % ("test-over" if call and call.test else "no-call"), "application/json")
        self.reply(404)

    def reply(self, code, body="", ctype="text/plain", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass  # request lines are not logged: callers are other people


def serve():
    from websockets.sync.server import serve as ws_serve
    cfg = load_config()
    if not cfg.get("pin") or not cfg.get("stt_key") or not cfg.get("hook_secret"):
        sys.exit("not set up: run `voice-call.py keys` then `voice-call.py setup`")
    for name, langs in CLIPS.items():  # render the canned lines now, not on his first call
        for lang in langs:
            clip(cfg, name, lang)
    for port in (PORT, SAY_PORT):
        threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever, daemon=True).start()
    log(f"serving {PUBLIC_URL}/voice on 127.0.0.1:{PORT}+{PORT + 1}, {len(ALLOW)} allowed caller(s)")
    with ws_serve(lambda ws: Stream(ws, load_config()).run(), "127.0.0.1", PORT + 1) as server:
        server.serve_forever()


def cmd_say(ticket: str, text: str):
    cfg = load_config()
    req = urllib.request.Request(f"http://127.0.0.1:{SAY_PORT}/voice/say", method="POST",
                                 data=json.dumps({"ticket": ticket, "text": text}).encode())
    req.add_header("X-Say-Token", cfg.get("say_token", ""))
    try:
        _, out = http(req, timeout=5)
    except OSError:
        out = {}
    if out.get("result") == "queued":
        print(f"{ticket}: queued on the live call, spoken at his next pause")
    elif out.get("result") == "test-over":
        print(f"{ticket}: that synthetic test call is over; dropped, not texted")
    elif text_owner(text):
        print(f"{ticket}: the call is over, sent by iMessage instead")
    else:
        sys.exit(f"{ticket}: the call is over and the iMessage failed too")


# --- one-time setup ---

def cmd_keys():
    import getpass
    cfg = load_config()
    for key, env, label in (("twilio_sid", "TWILIO_ACCOUNT_SID", "Twilio Account SID (AC...)"),
                            ("twilio_key", "TWILIO_API_KEY", "Twilio API key SID (SK...)"),
                            ("twilio_secret", "TWILIO_API_SECRET", "Twilio API key secret"),
                            ("stt_key", "DEEPGRAM_API_KEY", "Deepgram API key"),
                            ("tts_key", "ELEVENLABS_API_KEY", "ElevenLabs API key"),
                            ("voice_id", "ELEVENLABS_VOICE_ID", "ElevenLabs voice id for the Boss")):
        cfg[key] = os.environ.get(env) or getpass.getpass(f"{label}: ").strip() or cfg.get(key, "")
    save_config(cfg)
    print(f"saved to {CONFIG}")



def cmd_test_window(minutes: int):
    cfg = load_config()
    minutes = max(0, min(minutes, TEST_WINDOW_MAX_MIN))
    cfg["test_until"] = time.time() + minutes * 60 if minutes else 0
    save_config(cfg)
    print(f"test window {'open for ' + str(minutes) + ' min' if minutes else 'closed'}: "
          f"{cfg.get('twilio_number')} {'may' if minutes else 'may not'} call in")


def cmd_setup(buy=""):
    cfg = load_config()
    if not (cfg.get("twilio_key") and cfg.get("stt_key") and cfg.get("tts_key") and PUBLIC_URL):
        sys.exit("run `voice-call.py keys` first and set VOICE_CALL_PUBLIC_URL")
    cfg.setdefault("pin", f"{secrets.randbelow(10**6):06d}")
    cfg.setdefault("say_token", secrets.token_urlsafe(24))
    cfg.setdefault("hook_secret", secrets.token_urlsafe(24))
    save_config(cfg)
    if buy and cfg.get("twilio_number"):
        sys.exit(f"already set up with {cfg['twilio_number']}: not buying {buy}")
    if not cfg.get("twilio_number"):
        if not buy:  # a number is a purchase: setup never buys one on its own
            code, found = twilio(cfg, "GET", f"/AvailablePhoneNumbers/US/Local.json?AreaCode={AREA_CODE}&VoiceEnabled=true")
            pick = ((found.get("available_phone_numbers") or [{}])[0]).get("phone_number") or f"a {AREA_CODE} number"
            _, prices = twilio(cfg, "GET", "https://pricing.twilio.com/v1/PhoneNumbers/Countries/US")
            price = next((f"{x['current_price']} {prices.get('price_unit', 'USD')}" for x in prices.get("phone_number_prices", [])
                          if x.get("number_type") == "local"), "price unknown")
            sys.exit(f"No Twilio number yet, and buying one needs the owner's yes first. Ask for: {pick}, "
                     f"{price} a month, recurring. After the yes: voice-call.py setup --buy {pick}")
        if not re.fullmatch(r"\+\d{8,15}", buy):
            sys.exit(f"--buy takes the number to buy, like +16505550100, not {buy!r}")
        code, bought = twilio(cfg, "POST", "/IncomingPhoneNumbers.json", {"PhoneNumber": buy, "FriendlyName": "voice-call"})
        if code not in (200, 201):
            sys.exit(f"buying {buy} failed: {code} {bought.get('message')}")
        cfg["twilio_number"], cfg["twilio_number_sid"] = bought["phone_number"], bought["sid"]
        save_config(cfg)  # bought: never lose it to a later failure
    # Every run re-points the number, so a new PUBLIC_URL or hook secret takes effect.
    code, out = twilio(cfg, "POST", f"/IncomingPhoneNumbers/{cfg['twilio_number_sid']}.json", {
        "VoiceUrl": f"{hook_base(cfg)}/voice/twilio", "VoiceMethod": "POST",
        "StatusCallback": f"{hook_base(cfg)}/voice/twilio/status", "StatusCallbackMethod": "POST"})
    if code != 200:
        sys.exit(f"pointing {cfg['twilio_number']} here failed: {code} {out.get('message')}")
    print(f"ready: {cfg['twilio_number']}. The owner's contact dials <line>,,<PIN> (PIN in {CONFIG}).")


# --- measuring ---

def latency(path: str, frame_ms=20, pause_ms=700):
    """Reply gaps in a Twilio dual recording: channel 1 is the caller, channel 2 the agent.
    A caller turn ends after pause_ms of quiet; the gap runs to the agent's next sound."""
    with wave.open(path) as w:
        if w.getnchannels() != 2 or w.getsampwidth() != 2:
            raise ValueError("need a 16-bit two-channel recording")
        rate, pcm = w.getframerate(), array.array("h", w.readframes(w.getnframes()))
    n = rate * frame_ms // 1000

    def loud(ch):
        samples = pcm[ch::2]
        energy = [sum(abs(s) for s in samples[i:i + n]) / n for i in range(0, len(samples) - n + 1, n)]
        floor = sorted(energy)[len(energy) // 10] if energy else 0
        return [e > max(4 * floor, 300) for e in energy]

    caller, agent = loud(0), loud(1)
    gaps, quiet, pause = [], 0, pause_ms // frame_ms
    for i, c in enumerate(caller):
        quiet = 0 if c else quiet + 1
        if quiet == pause and any(caller[max(0, i - pause - 25):i - pause + 1]):
            end = i - pause + 1
            if any(agent[max(0, end - 5):end]):
                continue  # the agent was already talking: not a reply gap
            nxt = next((j for j in range(end, len(agent)) if agent[j]), None)
            if nxt is not None and not any(caller[end:nxt]):
                gaps.append((nxt - end) * frame_ms / 1000)
    return gaps


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "serve":
        serve()
    elif cmd == "say" and len(sys.argv) == 4:
        cmd_say(sys.argv[2], sys.argv[3])
    elif cmd == "keys":
        cmd_keys()
    elif cmd == "setup" and (len(sys.argv) == 2 or (len(sys.argv) == 4 and sys.argv[2] == "--buy")):
        cmd_setup(sys.argv[3] if len(sys.argv) == 4 else "")
    elif cmd == "test-window" and len(sys.argv) == 3:
        cmd_test_window(int(sys.argv[2]))
    elif cmd == "latency" and len(sys.argv) == 3:
        gaps = sorted(latency(sys.argv[2]))
        if not gaps:
            sys.exit("no reply gaps found")
        print(f"{len(gaps)} replies: p50 {gaps[len(gaps) // 2]:.2f}s, "
              f"p95 {gaps[min(len(gaps) - 1, int(len(gaps) * 0.95))]:.2f}s, max {gaps[-1]:.2f}s")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
