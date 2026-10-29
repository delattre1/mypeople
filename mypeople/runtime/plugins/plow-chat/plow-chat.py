#!/usr/bin/env python3
"""Plow Chat plugin: the owner texts the Boss over iMessage/SMS through Plow.

Turn it on with PLOW_CHAT=1 in queue.env; `mypeople up` then keeps it running.
First run has no credentials, so it mints a code: run `plow-chat.py status`
and text "Plow Activate: <code>" to the number it prints, once.

Usage:
  plow-chat.py serve                  run the bridge (supervise.sh entry point)
  plow-chat.py reply "text"           reply in the owner's own chat
  plow-chat.py reply cht_x "text"     reply in that chat (groups)
  plow-chat.py reply cht_x --file a.jpg "text"   send photos/files too (up to 4)
  plow-chat.py react cht_x msg_y like tapback a message (love, laugh, an emoji, remove...)
  plow-chat.py api POST /v1/chats '{...}'  any Plow API call with this line's login
                                     (docs: https://api.plow.co/openapi.json)
  plow-chat.py status                 print activation/bridge state

The Boss is told what plain text loses: the message a text replies to, who it
mentions, and tapbacks people left. A tapback never wakes the Boss on its own;
it rides along with the next text.

One account covers every chat: the owner can add the Plow line to group
threads, and each group is its own chat that reaches the Boss and is answered
in the thread that asked.

In a Plow cloud agent (PLOW_API_BASE set, no saved login) there is nothing to
activate: the VM's own line is the chat, the token comes from the environment
(or the "proxied" placeholder the exe.dev proxy swaps for the real one), and
the chats come from GET /v1/agents/me.

The bridge holds Plow's account websocket (POST /v1/ws/ticket, then /v1/ws): a frame
says which chat changed and that chat is read at once. Every (re)connect, and every
minute, a pass over all chats catches whatever the socket missed; while the socket
is down the bridge polls every PLOW_CHAT_POLL_SECONDS.
"""
import http.client
import json
import mimetypes
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
from mpcommon import WebSocket  # noqa: E402  (the one discord-chat holds Discord's gateway with)

BASE = (os.environ.get("PLOW_CHAT_BASE_URL") or os.environ.get("PLOW_API_BASE")
        or "https://api.plow.co").rstrip("/")
INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME")
               or Path.home() / "mypeople")
STATE_DIR = Path(os.environ.get("PLOW_CHAT_STATE_DIR") or INSTALL / "state" / "plow-chat")
CREDS = STATE_DIR / "creds.json"          # {"token": ..., "chat_uid": ..., "base_url"?: ...}
# A saved login may name its own server: the second bridge supervise.sh runs for a WhatsApp lab
# (whatsapp-linq-api) learns where the lab is from this one file, so a cloud VM needs nothing else.
try:
    BASE = (json.loads(CREDS.read_text()).get("base_url") or BASE).rstrip("/")
except (OSError, ValueError, AttributeError):
    pass
ACTIVATION = STATE_DIR / "activation.json"
ATTACHMENTS = STATE_DIR / "attachments"   # inbound photos/files, for the Boss to open
STATE = STATE_DIR / "state.json"          # {"marks": {chat: [created_at, uid]}, "seeded_chats": [...]}
# How every reply looks, in one file the owner or the Boss edits (Daniel 10-18: "the style of how to reply should be
# part of the message package"). It rides in every message package; "{name}" becomes the agent that gets the text.
STYLE = STATE_DIR / "style.md"
HOST_ID = os.environ.get("HOST_ID") or os.uname().nodename.split(".")[0]
BOSS_AGENT = os.environ.get("BOSS_AGENT") or f"{HOST_ID}/main:Boss"
MP_BIN = os.environ.get("MP_BIN") or str(INSTALL / "bin" / "mp")
# PLOW_CHAT_ROUTER=jev: one JEV decision picks which live agent (mp status) gets each text; the last agent and
# the quoted message are context only. Off, everything goes to the Boss. In an exehome VM, TYPESAFE_URL is the
# proxy integration that adds the key, so the key never enters the VM; on a Mac, TYPESAFE_API_KEY.
ROUTER = os.environ.get("PLOW_CHAT_ROUTER", "")
JEV_URL = os.environ.get("TYPESAFE_URL") or "https://api.typesafe.ai/v1/systemone"
# Tuned on 23 multi-turn conversations (74/74) and 12 hard cases x3 (36/36): follow-ups, topic switches, vague lines,
# agents named mid-chat or in a greeting, and requests that refer back to another agent's answer.
ROUTE_ASK = ("A person texts one WhatsApp chat answered by several AI agents, as in a group chat. Which agent should get "
             "this message? If it calls an agent by name, even in a greeting (hey coach, chef?, Mailer, ...), pick that "
             "agent. If it asks for one agent's kind of work (an email, a recipe, a workout), pick that agent, even "
             "mid-conversation and even when it refers back to what another agent just said. If it asks about this "
             "system, which agents exist, or making, changing or retiring an agent, pick Boss. Only if it has no topic of "
             "its own and names no agent (a greeting, ok, thanks, yes, and?, more, why?, what can you do?): pick the agent "
             "who answered in the last few minutes, or Boss when none did.")
FOLLOW_MINUTES = float(os.environ.get("PLOW_CHAT_FOLLOW_MINUTES", "30"))  # an older answer no longer counts as the conversation
# A voice note is heard before it is routed, so the router and the agent get words, not only a file (Daniel 10-20). The
# server at PLOW_CHAT_STT_URL takes the audio as multipart "audio" and answers {"text": ...} (the homelab's stt-api
# /transcribe/fast). Unset, down or slow: the file still arrives, with a note that the transcript is missing.
STT_URL = os.environ.get("PLOW_CHAT_STT_URL", "")
STT_SECONDS = 30
# Who wrote a reply: each agent's prompt signs its last bubble "~Name" on its own line (Daniel 10-18). Unsigned
# bubbles of the same reply name no one.
SPEAKER = re.compile(r"(?:\n|^)~([A-Z][A-Za-z0-9]{1,19})\s*$")


def signed_by(body: str) -> str:
    named = SPEAKER.search(body or "")
    return named.group(1) if named else ""
# A pass over every chat runs on each (re)connect, every SWEEP_SECONDS on a live socket (Plow's
# events are best-effort, so a frame can go missing), and every POLL_SECONDS while the socket is
# down or a text waits on the Boss.
POLL_SECONDS = float(os.environ.get("PLOW_CHAT_POLL_SECONDS", "5"))
SWEEP_SECONDS = 60
PING_SECONDS = 30  # a socket that answered nothing for two of these is dead, whatever TCP says
SELF = str(Path(__file__).resolve())
# How the Boss runs this plugin. A second bridge, on another Plow account or another Plow-shaped
# server (a WhatsApp number's whatsapp-linq-api), runs with its own state dir and base URL; the
# commands it hands the Boss carry both, or a reply goes out under the wrong login or to the wrong server.
RUN = "".join(f"{k}={shlex.quote(os.environ[k])} " for k in ("PLOW_CHAT_STATE_DIR", "PLOW_CHAT_BASE_URL")
              if os.environ.get(k)) + f"python3 {SELF}"
STATE_LOCK = threading.Lock()
WELCOME = "MyPlow is up. Text me what you want done."


def log(msg: str):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] plow-chat: {msg}",
          flush=True)


READER = []  # this process's one kept-alive connection for reads


def api(method: str, path: str, body=None, token=None, timeout=20):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if method == "GET":
        return read(path, headers, timeout)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def read(path: str, headers: dict, timeout: float):
    """GET over one kept-alive connection: a fresh TLS handshake per read was ~1s of every text's
    wait. Only reads reuse it, because only a read is safe to resend when the server closed the
    idle connection under it."""
    u = urllib.parse.urlsplit(BASE)
    for retry in (False, True):
        if not READER:
            conn = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
            READER.append(conn(u.netloc, timeout=timeout))
        try:
            READER[0].request("GET", u.path + path, headers=headers)
            r = READER[0].getresponse()
            raw = r.read()
            break
        except (OSError, http.client.HTTPException) as e:
            READER.pop().close()
            if retry:
                raise OSError(f"GET {path}: {e!r}") from e
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        if r.status < 400:
            raise
        return r.status, {}


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def write_json(p: Path, obj):
    # Creds hold a user-wide bearer token: 600 from the first byte, never chmod after.
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


def listing(data) -> list:
    return data.get("data") or data.get("messages") or data.get("chats") or [] \
        if isinstance(data, dict) else data


# --- Boss ---

def envelope(who: str, text: str, chat_uid: str, files=(), context=(), mid: str = "", agent: str = "") -> str:
    # Plow seats whoever the carrier reports in the thread, so the Boss is told
    # who actually spoke and which thread — in a group these differ per message.
    sent = "".join(f"\n[attached, open it: {f}]" for f in files)
    said = "".join(f"\n({c})" for c in context)
    tap = (f" To tapback this message, run: {RUN} react {chat_uid} {mid} like "
           f"(or love, laugh, emphasize, question, dislike, or one emoji).") if mid else ""
    # The owner's ask: feedback the moment an agent sees his text, not after the slow turn. It is
    # the first line, so it is the first command run, not one noticed mid-work (Boss, 10-05).
    seen = (f"FIRST, before reading on: {RUN} react {chat_uid} {mid} 👀"
            f"  (marks this text seen for the sender; then read, work and reply)\n") if mid else ""
    # First, because an agent that only hears about reply/react told the owner "I can't create
    # threads" (10-15): it holds this line's login, and with it the whole API.
    able = (f"You hold this line's Plow login and with it the whole Plow API (docs: {BASE}/openapi.json). "
            f"Any call: {RUN} api METHOD /v1/PATH ['JSON body']; e.g. {RUN} api GET /v1/agents/me "
            f"(this line and its chats), or {RUN} api POST /v1/chats '{{\"line_uid\": \"ln_...\", "
            f"\"members\": [\"+1...\"], \"body\": \"first text\", \"idempotency_key\": \"any-unique-id\"}}' "
            f"to start a new thread or group. Do what the person asks with it; never answer that you "
            f"can't without trying. ")
    try:
        style = " ".join(STYLE.read_text().split()).replace("{name}", agent or BOSS_AGENT.rsplit(":", 1)[1])
    except OSError:
        style = ""
    return (
        f"{seen}[plowchat] from {who} in {chat_uid}: {text}{sent}{said}\n"
        f"({able}This is the owner's Plow messages line. The owner texts here, and so "
        f"does anyone they added to this thread. To answer, run: "
        f"{RUN} reply {chat_uid} \"your reply\" — it goes back to that "
        f"same thread, which may be a group. Plain text only, no markdown. To send "
        f"photos or files, add --file PATH before the text, up to 4.{tap})"
        + (f"\n(Reply style: {style})" if style else "")
    )


TAPBACKS = {"love": "loved", "like": "liked", "dislike": "disliked", "laugh": "laughed at",
            "emphasize": "emphasized", "question": "questioned"}


def speaker(sender: dict) -> str:
    if sender.get("type") == "agent" and sender.get("relationship") == "self":
        return "you"  # this line, i.e. the Boss
    return sender.get("display_name") or sender.get("provider_key") or "chat member"


def whose(m: dict) -> str:
    """`your message msg_x: "..."` or `Dan's message msg_x: "..."`."""
    who = speaker(m.get("sender") or {})
    text = " ".join((m.get("body") or "").split())
    text = (text[:500] + "…" if len(text) > 500 else text) or "(a photo or file)"
    owner = "your" if who == "you" else f"{who}'s"
    return f"{owner} message {m.get('uid')}: \"{text}\""


def context(message: dict) -> list:
    """What plain text loses: the message this one answers, and who it tags. [] for neither."""
    lines = []
    if (message.get("reply_to") or {}).get("message"):
        lines.append(f"in reply to {whose(message['reply_to']['message'])}")
    tags = [("you" if t.get("is_me") else t.get("handle") or "someone")
            for t in message.get("mentions") or []]
    if tags:
        lines.append("mentions: " + ", ".join(dict.fromkeys(tags)))
    return lines


def note_tapbacks(msgs: list, chat_uid: str, seed: bool = True) -> int:
    """Hold new tapbacks from people for the Boss's next message; never wake it for one.

    A thumbs-up is not someone writing to the Boss: waking it would make every
    acknowledgement a turn. So it rides along with the next real text, tied to
    the message it landed on.
    ponytail: only the page each poll already fetches (the newest 20 messages)
    is read, so a tapback on an older message is never seen. Upgrade path: the
    reaction_added frames on the account websocket.
    """
    live = {}
    for m in msgs:
        for r in m.get("reactions") or []:
            actor = r.get("actor") or {}
            if actor.get("type") != "member":
                continue  # the Boss's own tapbacks come back on the page too
            verb = TAPBACKS.get(r.get("type")) or f"reacted {r.get('custom_emoji') or ''} to"
            # The type is in the key: turning a like into a heart may keep the reaction's uid.
            key = f"{r.get('uid')}:{r.get('type')}:{r.get('custom_emoji') or ''}"
            # It only ever rides along with a real text, so it must not read as a verdict on that text:
            # "not a new message" under Patrick's "8*5" made the Boss skip a real question (10-06).
            live[key] = (f"earlier tapback in {chat_uid}, separate from the new text above: "
                         f"{speaker(actor)} {verb} {whose(m)}")
    with STATE_LOCK:
        st = read_json(STATE, {})
        # Per chat, exactly the tapbacks on its page now: one window shared by every chat let a
        # quiet chat's old tapbacks age out and come back as news (delattre1/mypeople#40).
        by_chat = st.setdefault("page_tapbacks", {})
        known = by_chat.get(chat_uid)
        new = [k for k in live if k not in (known or [])]
        if not new and known is not None and len(known) == len(live):
            return 0
        # First sight of a chat with seeding on: its tapbacks are history, not news. A chat seen
        # before this per-chat record existed is seeded too, once.
        first = known is None and (seed or chat_uid in st.get("tapback_chats", []))
        news = [] if first else [live[k] for k in new]
        st["pending_tapbacks"] = (st.get("pending_tapbacks", []) + news)[-20:]
        by_chat[chat_uid] = sorted(live)
        write_json(STATE, st)
    return len(news)


def role_about(role: str) -> str:
    """What an agent does, from its role (name@version -> roles/profiles/<name>/<version>.json): the profile's
    one-line metadata.description, else its personality text."""
    try:
        key, version = role.split("@", 1)
        profile = json.loads((INSTALL / "roles" / "profiles" / key / f"{version}.json").read_text())
        if (profile.get("metadata") or {}).get("description"):
            return str(profile["metadata"]["description"])[:600]
        text = (INSTALL / "roles" / profile["spec"]["personalityRef"]).read_text()
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return " ".join(ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#"))[:600]


def live_agents() -> dict:
    """{name: (agent id, what it does)} for this host's live agents: the router's candidates. Created = in, retired = out."""
    out = subprocess.run([sys.executable, MP_BIN, "status"], capture_output=True, text=True, timeout=15).stdout
    return {aid.rsplit(":", 1)[1]: (aid, role_about(role))
            for aid, role in re.findall(r"^(\S+:\S+)\s+\[alive\s*\]\s+(\S+)", out, re.M) if aid.startswith(f"{HOST_ID}/")}


def pick_agent(text: str, message: dict, last: str = "", minutes=None, last_reply: str = "") -> str:
    """The one routing decision: which live agent gets this text. Who answered last (the name the chat's latest
    reply starts with), how many minutes ago and what they said are context only. Anything unsure or broken goes
    to the Boss."""
    if ROUTER != "jev":
        return BOSS_AGENT
    try:
        agents = live_agents()
        if not text:  # a photo, or a voice note nobody could hear, has nothing to classify: it stays with whoever had the chat
            return agents[last][0] if last in agents else BOSS_AGENT
        if len(agents) < 2:
            return BOSS_AGENT
        state = {"message": text[:2000]}
        if last in agents:
            state["last_answered_by"] = last
            if minutes is not None:
                state["minutes_since_that_answer"] = round(minutes, 1)
            if last_reply:
                state["that_answer"] = last_reply[:300]
        quoted = ((message.get("reply_to") or {}).get("message") or {}).get("body")
        if quoted:
            state["replying_to"] = quoted[:500]
        body = {"state": state, "model": "jev-latest", "questions": {"agent": {
            "type": "choice", "instructions": ROUTE_ASK, "criteria": {n: about or n for n, (_, about) in agents.items()}}}}
        headers = {"Content-Type": "application/json"}
        if os.environ.get("TYPESAFE_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["TYPESAFE_API_KEY"]
        req = urllib.request.Request(JEV_URL, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            choice = json.load(r)["answers"]["agent"]["choice"]
    except Exception as e:  # noqa: BLE001 - a router that fails must never lose the text
        log(f"router: {type(e).__name__}: {e}; to the Boss")
        return BOSS_AGENT
    return agents[choice][0] if choice in agents else BOSS_AGENT


def send_to_agent(message: str, agent: str = BOSS_AGENT) -> bool:
    try:
        r = subprocess.run([sys.executable, MP_BIN, "send", agent, message],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"ERROR mp send: {e}")
        return False
    if r.returncode != 0:
        log(f"ERROR mp send rc={r.returncode}: {r.stderr.strip()[:200]}")
        return False
    return True


WARN_AFTER = 120  # seconds a text may wait for a Boss that is coming back before its sender is told
STARTED = time.time()  # a bridge that just started (a reboot, an update) gives the Boss WARN_AFTER too


def message_age(message: dict) -> float:
    try:
        sent = datetime.fromisoformat((message.get("created_at") or "").replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - sent).total_seconds()
    except (ValueError, TypeError):
        return float("inf")  # no readable time: treat it as long overdue


FLOOR = ["", ""]  # a mark below every message: everything after it routes


def order(message: dict) -> list:
    """A message's place in its chat: [created_at as fixed-width UTC, uid]. Parsed, because servers
    differ (api.plow.co sends microseconds, whatsapp-linq-api milliseconds)."""
    ts = message.get("created_at") or ""
    try:
        ts = (datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
              .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    except ValueError:
        pass
    return [ts, message.get("uid") or ""]


def conversation(answer, message) -> tuple:
    """(who answered last, minutes before this text, what they said) while that answer is recent, else ()."""
    if not answer:
        return ()
    try:
        at = lambda m: datetime.fromisoformat((m.get("created_at") or "").replace("Z", "+00:00"))
        minutes = (at(message) - at(answer)).total_seconds() / 60
    except (ValueError, TypeError):
        minutes = None  # no readable times: who answered last still counts
    if minutes is not None and minutes > FOLLOW_MINUTES:
        return ()
    return signed_by(answer["body"]), minutes, answer["body"]


def route_message(message: dict, chat: str = "", last: tuple = ()) -> str:
    """Deliver one inbound message in `chat` (the one being polled) to the Boss, exactly once."""
    if message.get("direction") != "inbound":
        return "skip: outbound"
    mid = message.get("uid") or ""
    text = (message.get("body") or "").strip()
    atts = message.get("attachments") or []
    # A photo with no caption is a message too: skipping it silently read as the
    # Boss ignoring the owner.
    if not mid or not (text or atts):
        return "skip: empty"

    chat_uid = message.get("chat_uid") or chat
    chat = chat or chat_uid
    key = order(message)
    # Claim before sending, so an overlapping pass cannot wake the Boss twice. The claim is the
    # chat's high-water mark: a message routes only if it is newer than the newest one routed.
    # A window of seen ids shared by every chat let a quiet chat's page age out of it and be
    # routed again as new (delattre1/mypeople#40).
    with STATE_LOCK:
        st = read_json(STATE, {})
        marks = st.setdefault("marks", {})
        prev = marks.get(chat)
        if prev is not None and key <= prev:
            return "skip: already routed"
        marks[chat] = key
        write_json(STATE, st)

    who = speaker(message.get("sender") or {})
    files, notes = [], []  # a file that did not arrive is a note, never "open it"
    for a in atts:
        try:
            files.append(download(a))
        except (OSError, ValueError) as e:
            log(f"attachment {a.get('uid')} not saved: {e}")
            notes.append(f"could not download {a.get('filename') or 'attachment'}: {e}")
            continue
        if (a.get("content_type") or mimetypes.guess_type(files[-1])[0] or "").startswith("audio/"):
            try:
                text = f"{text}\n[voice note, transcribed] {transcribe(files[-1])}".strip()
            except Exception as e:  # noqa: BLE001 - a deaf STT must never lose the voice note
                log(f"voice note {a.get('uid')} not transcribed: {e}")
                notes.append(f"voice note not transcribed ({e}): the transcript is missing, only the audio file came")
    with STATE_LOCK:
        taps = read_json(STATE, {}).get("pending_tapbacks", [])
    target = pick_agent(text, message, *last)
    if send_to_agent(envelope(who, text, chat_uid, files, notes + context(message) + taps, mid,
                              target.rsplit(":", 1)[1]), target):
        if taps:
            with STATE_LOCK:
                st = read_json(STATE, {})
                st["pending_tapbacks"] = [t for t in st.get("pending_tapbacks", []) if t not in taps]
                write_json(STATE, st)
        return f"ROUTE {chat_uid} {who}{' -> ' + target.rsplit(':', 1)[1] if ROUTER else ''}: {text[:60]}"
    with STATE_LOCK:  # release the claim so the next pass retries it
        st = read_json(STATE, {})
        marks = st.setdefault("marks", {})
        if marks.get(chat) == key:
            if prev is None:
                marks.pop(chat)
            else:
                marks[chat] = prev
        warned = mid in st.get("warned_ids", [])
        late = min(message_age(message), time.time() - STARTED) >= WARN_AFTER
        if late and not warned:
            st["warned_ids"] = (st.get("warned_ids", []) + [mid])[-200:]
        write_json(STATE, st)
    if not late or warned:
        # A Boss that is coming up (a restart, an in-place update) gets it on the next pass; a text
        # that keeps failing is told once, not every pass.
        return "ERROR mp send failed; retrying next pass"
    # Say it on the phone that just texted, the one path known to work, instead
    # of leaving them waiting on an answer nobody heard.
    try:
        send_message("Your text reached me but I could not wake the team with it. "
                     "Retrying; nothing is lost.", chat_uid)
    except (SystemExit, OSError) as exc:
        log(f"could not warn the sender either: {exc}")
    return "ERROR mp send failed; sender warned, retrying next pass"


def load_creds() -> dict:
    """A saved login wins; otherwise a cloud agent's environment is the login."""
    creds = read_json(CREDS, {})
    if not creds.get("token") and os.environ.get("PLOW_API_BASE"):
        creds = {"token": os.environ.get("PLOW_AGENT_TOKEN") or "proxied",
                 "chat_uid": "", "cloud": True}
    return creds


def download(att: dict) -> str:
    """Save one inbound attachment where the Boss can open it; return its path."""
    if not att.get("url"):  # failed media comes with no url: there is nothing to fetch
        raise ValueError("file unavailable")
    uid = att.get("uid") or "att"
    name = os.path.basename(att.get("filename") or "file")
    dest = ATTACHMENTS / f"{uid}-{name}"
    if dest.exists():
        return str(dest)
    req = urllib.request.Request(BASE + att["url"])  # a signed, short-lived path on the chat
    token = load_creds().get("token")
    if token:
        # Not forwarded on a redirect: a signed storage URL refuses a second credential.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    ATTACHMENTS.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=60) as r:
        dest.write_bytes(r.read())
    return str(dest)


def transcribe(path: str) -> str:
    """What a saved voice note says, from the STT server. Raises when there is none, it fails, or it heard nothing."""
    if not STT_URL:
        raise OSError("no STT server set (PLOW_CHAT_STT_URL)")
    suffix = Path(path).suffix if Path(path).suffix[1:].isalnum() else ""  # the server reads the format from it
    edge = os.urandom(16).hex()
    data = (f'--{edge}\r\nContent-Disposition: form-data; name="audio"; filename="voice{suffix}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + Path(path).read_bytes() + f"\r\n--{edge}--\r\n".encode()
    # To the name's IPv4 address when it has one, the name kept as Host (a proxy routes on it): a VPN exit node can take
    # the IPv6 one, and Daniel's first voice note on Mac 1 died on "No route to host" for server.local (10-20).
    u, url = urllib.parse.urlsplit(STT_URL), STT_URL
    if u.scheme == "http":  # https checks the certificate against the name in the URL
        try:
            ip = socket.getaddrinfo(u.hostname, u.port or 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
            url = u._replace(netloc=f"{ip}:{u.port}" if u.port else ip).geturl()
        except OSError:
            pass  # no IPv4 address: the name as given
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Host": u.netloc, "Content-Type": f"multipart/form-data; boundary={edge}"})
    with urllib.request.urlopen(req, timeout=STT_SECONDS) as r:
        heard = " ".join((json.load(r).get("text") or "").split())
    if not heard:
        raise ValueError("no words heard")
    return heard


def upload(path: str, chat_uid: str, creds: dict) -> str:
    """Declare one outbound file on the chat, PUT its bytes, return its attachment uid."""
    data = Path(path).read_bytes()
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    status, a = api("POST", f"/v1/chats/{chat_uid}/attachments",
                    {"filename": os.path.basename(path), "content_type": ctype,
                     "size_bytes": len(data)}, token=creds["token"])
    if status >= 400:
        raise SystemExit(f"attachment refused {status}: {json.dumps(a)[:200]}")
    req = urllib.request.Request(a["upload_url"], data=data, method="PUT",
                                 headers=a.get("upload_headers") or {})
    urllib.request.urlopen(req, timeout=300).close()
    return a["uid"]


# --- Outbound ---

def send_message(text: str, chat_uid: str = "", creds=None, files=()) -> dict:
    creds = creds or load_creds()
    if not creds.get("token"):
        raise SystemExit("no Plow Chat credentials yet: activation is not done")
    # No thread named = the owner's own 1:1: the chat activation resolved, or
    # in the cloud the first chat on this agent's line.
    chat_uid = chat_uid or creds["chat_uid"] or next(iter(list_chats(creds)), "")
    body = {"body": text}
    if files:
        body["attachment_uids"] = [upload(f, chat_uid, creds) for f in files]
    status, data = api("POST", f"/v1/chats/{chat_uid}/messages", body, token=creds["token"])
    if status >= 400:
        raise SystemExit(f"send failed {status}: {json.dumps(data)[:200]}")
    return data


def react(chat_uid: str, message_uid: str, kind: str, creds=None) -> dict:
    """Tapback one message (a TAPBACKS name or one emoji), or `remove` this line's tapback."""
    if kind == "remove":
        body = {"operation": "remove"}
    elif kind in TAPBACKS:
        body = {"operation": "add", "type": kind}
    elif kind and not kind.isascii() and len(kind) <= 8:  # an emoji, not a misspelt name
        body = {"operation": "add", "type": "custom", "custom_emoji": kind}
    else:
        body = None
    if not (body and chat_uid.startswith("cht_") and message_uid.startswith("msg_")):
        raise SystemExit(f"usage: {SELF} react cht_x msg_y "
                         f"{'|'.join(TAPBACKS)}|EMOJI|remove")
    creds = creds or load_creds()
    status, data = api("POST", f"/v1/chats/{chat_uid}/messages/{message_uid}/reactions",
                       body, token=creds["token"])
    if status >= 400:
        raise SystemExit(f"tapback failed {status}: {json.dumps(data)[:200]}")
    return data


# --- Activation ---

def mint_activation() -> dict:
    status, data = api("POST", "/v1/auth/activate",
                       {"name": "MyPeople Boss", "provision_chat": False})
    if status >= 400 or "activation_secret" not in data:
        raise RuntimeError(f"activate failed {status}: {json.dumps(data)[:200]}")
    write_json(ACTIVATION, data)
    log(f"activation code: text \"Plow Activate: {data['display_code']}\" to {data['send_to']}")
    return data


def last_message_ts(token: str, chat_uid: str) -> str:
    _, data = api("GET", f"/v1/chats/{chat_uid}/messages", token=token)
    return max((m.get("created_at") or "" for m in listing(data)), default="")


def resolve_chat_uid(token: str, redeem: dict, last_ts=last_message_ts) -> str:
    """The chat this account actually talks in.

    An account keeps every chat a past activation provisioned, most of them
    dead, so pick by the newest message rather than the first listed.
    """
    chat = redeem.get("chat") or {}
    if chat.get("uid"):
        return chat["uid"]
    _, data = api("GET", "/v1/chats", token=token)
    chats = listing(data)
    live = [c for c in chats if c.get("status") == "active" and c.get("uid")] or chats
    if not live:
        raise RuntimeError(f"no chat on this account: {json.dumps(data)[:200]}")
    return max(live, key=lambda c: last_ts(token, c["uid"]))["uid"]


def activation_loop() -> dict:
    """Hold a live code until the owner texts it; codes expire, so re-mint."""
    a = read_json(ACTIVATION, {})
    if not a.get("activation_secret"):
        a = mint_activation()
    while True:
        status, data = api("POST", "/v1/auth/activate/redeem",
                           {"activation_secret": a["activation_secret"]})
        if status == 410 or data.get("status") == "expired":
            log("activation code expired, minting a fresh one")
            a = mint_activation()
        elif data.get("status") == "verified" and data.get("token"):
            token = data["token"]
            creds = {"token": token, "chat_uid": resolve_chat_uid(token, data)}
            write_json(CREDS, creds)
            ACTIVATION.unlink(missing_ok=True)
            log(f"activated: chat {creds['chat_uid']}")
            return creds
        time.sleep(5)


# --- Bridge ---

def list_chats(creds: dict) -> list:
    if creds.get("cloud"):
        # Live read every pass: a group the line joins later is a new chat. Not the legacy agents/cloud/me:
        # Plow keeps that legacy path only until the images move off it (plow api/plow/main.py).
        status, data = api("GET", "/v1/agents/me", token=creds["token"])
        if status >= 400:
            log(f"/me failed {status}: {json.dumps(data)[:160]}")
            return []
        return [c["uid"] for c in data.get("chats") or [] if c.get("uid")]
    status, data = api("GET", "/v1/chats", token=creds["token"])
    if status >= 400:
        # A listing hiccup must not cost the owner their own 1:1.
        log(f"chat listing failed {status}: {json.dumps(data)[:160]}")
        return [creds["chat_uid"]]
    return [c["uid"] for c in listing(data) if c.get("uid")] or [creds["chat_uid"]]


def poll_chat(creds: dict, chat_uid: str, seed: bool = True) -> int:
    """Route new messages in one chat, seeding it silently the first time.

    Returns how many it routed, or -1 while one still waits (the read or the Boss failed).
    Seeding is per chat: a group joined months in carries its own history, and
    replaying it would hand the Boss a fake inbox. Only chats already there when
    the bridge starts are seeded; one that appears while it runs was started by
    a text, and swallowing that text would leave the sender unanswered.
    """
    # The literal text: Plow's default body is markdown, so an owner's "jane_doe" reached agents as
    # "jane\_doe" (10-29). Sends are already verbatim.
    status, data = api("GET", f"/v1/chats/{chat_uid}/messages?format=text_decorations", token=creds["token"])
    if status >= 400:
        log(f"read failed {chat_uid} {status}: {json.dumps(data)[:160]}")
        return -1
    msgs = listing(data)
    with STATE_LOCK:
        st = read_json(STATE, {})
        seeded = set(st.get("seeded_chats", []))
        first = chat_uid not in seeded
        marks = st.setdefault("marks", {})
        if first and not seed:
            # The floor mark routes everything on its page, and gives it a mark at once. Without
            # one, a chat whose first page was only our own send (Plow's send-first POST
            # /v1/chats) passed next time for a chat from before marks, which marks its whole
            # page handled, and its first reply never reached the Boss.
            st["seeded_chats"] = sorted(seeded | {chat_uid})
            marks[chat_uid] = FLOOR
            write_json(STATE, st)
            first = False
        mark = None
        if first:
            mark = max((order(m) for m in msgs), default=FLOOR)  # empty at startup: same floor
        elif chat_uid in seeded and chat_uid not in marks and msgs:
            # Seen before marks existed: the mark is the newest message the old id list says was
            # routed, or, if they all aged out of it, the newest on the page (no replay).
            legacy = set(st.get("seen_ids", []))
            done = [order(m) for m in msgs if m.get("uid") in legacy]
            mark = max(done or [order(m) for m in msgs])
        if mark or first:
            if mark:
                marks[chat_uid] = mark
            if first:
                st["seeded_chats"] = sorted(seeded | {chat_uid})
            write_json(STATE, st)
    note_tapbacks(msgs, chat_uid, seed)
    if first:
        log(f"seeded {len(msgs)} existing message(s) in {chat_uid} as already handled")
        if creds.get("cloud") and not creds.get("owner_handle"):
            # A fresh cloud agent's first text was the signup phrase, which the
            # seed just swallowed; without this the new owner hears nothing.
            # A MyPlow routed to its person by Build an Agent (route sets owner_handle) greets them itself, by
            # name and about their ask (first-text.py): a generic hello first reads as a second, worse greeting
            # (Daniel 10-28).
            try:
                send_message(WELCOME, chat_uid, creds)
            except SystemExit as e:  # a failed hello must not take the bridge down
                log(f"welcome not sent: {e}")
        return 0
    routed, last = 0, None
    for m in sorted(msgs, key=order):
        if m.get("direction") == "outbound" and signed_by(m.get("body")):
            last = m  # who answered last, for the router
        r = route_message(m, chat_uid, conversation(last, m))
        if not r.startswith("skip"):
            log(r)
        if r.startswith("ERROR"):
            return -1  # routing a newer one would move the mark past this one, and it would be lost
        routed += r.startswith("ROUTE")
    return routed


def sweep(creds: dict, seed: bool = False) -> bool:
    """One pass over every chat; False while a text still waits on the Boss."""
    return min([poll_chat(creds, uid, seed) for uid in list_chats(creds)], default=0) >= 0


def open_socket(creds: dict) -> WebSocket:
    # No chat_id: an account ticket carries every chat, a group made after it connected included.
    status, data = api("POST", "/v1/ws/ticket", {}, token=creds["token"])
    if status >= 400 or not data.get("ticket"):
        raise OSError(f"ws ticket {status}: {json.dumps(data)[:160]}")
    ws = WebSocket(f"{BASE.replace('http', 'ws', 1)}/v1/ws?ticket={urllib.parse.quote(data['ticket'])}")
    # The server speaks first with {"type": "connected"}: a ticket it refuses is accepted, then closed with 4401.
    first = ws.recv(15)
    if json.loads(first or "{}").get("type") != "connected":
        raise OSError(f"websocket opened without connected: {first!r:.100}")
    return ws


def listen(creds: dict, ws: WebSocket, due: float):
    """Read each chat the moment a frame says it changed, until the socket dies (it raises).

    A frame only names the chat: the read routes through the same marks as a pass, so the text of
    a frame the server dropped still arrives, in order, with the next frame or pass.
    """
    ping = time.time() + PING_SECONDS
    while True:
        raw = ws.recv(max(0.05, min(due, ping) - time.time()))
        if raw is not None:
            event = json.loads(raw)
            chat = event.get("chat_id")
            # Delivery receipts of the Boss's own replies change nothing to route.
            if chat and event.get("event_type") != "message_status_updated" \
                    and poll_chat(creds, chat, False) < 0:
                due = min(due, time.time() + POLL_SECONDS)
        now = time.time()
        if now >= due:
            due = now + (SWEEP_SECONDS if sweep(creds) else POLL_SECONDS)
        if now >= ping:
            if now - ws.heard > 2 * PING_SECONDS:
                raise OSError(f"websocket silent for {now - ws.heard:.0f}s")
            ws.send(b"", 0x9)
            ping = now + PING_SECONDS


def bridge_loop(creds: dict):
    log(f"bridge up, routing to {BOSS_AGENT}")
    seed, fails, retry_at = True, 0, 0.0
    while True:
        ws = None
        if time.time() >= retry_at:
            try:
                ws = open_socket(creds)
                fails = 0
                log("websocket connected")
            except (OSError, ValueError) as e:
                fails += 1
                retry_at = time.time() + min(POLL_SECONDS * 2 ** fails, 300)
                log(f"websocket down ({e}); polling every {POLL_SECONDS:g}s until it is back")
        try:
            # After connecting, so a text sent during this catch-up also comes as a frame (the marks
            # make that second sight a no-op). Without a socket, this pass is the whole bridge.
            ok = sweep(creds, seed)
            seed = False
            if ws:
                listen(creds, ws, time.time() + (SWEEP_SECONDS if ok else POLL_SECONDS))
        except (OSError, ValueError) as e:
            log(f"websocket lost: {e}" if ws else f"poll error: {e}")
        finally:
            if ws:
                ws.sock.close()
        time.sleep(1 if ws else POLL_SECONDS)


HELP = ("-h", "--help")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    args = sys.argv[2:]
    # An option is never message text: two engineers' `reply --help` reached the owner's chat as
    # the literal text "--help" (10-05). -h/--help on any subcommand prints this and does nothing
    # else (on `serve` it used to start a second bridge), and reply/react refuse any other leading
    # "--" word instead of sending it.
    if cmd in HELP or (args and args[0] in HELP):
        print(__doc__.strip())
        return
    if cmd == "reply":
        chat_uid = args.pop(0) if args and args[0].startswith("cht_") else ""
        files = []
        while args and (args[0] in HELP or args[0].startswith("--")):
            if args[0] in HELP:
                print(__doc__.strip())
                return
            if args[0] != "--file" or len(args) < 2:
                raise SystemExit(f"{args[0]!r} is not an option of reply, nothing sent. "
                                 f"usage: {SELF} reply [cht_x] [--file PATH ...] \"text\"")
            files.append(args[1])
            args = args[2:]
        if not args and not files:
            raise SystemExit(f"usage: {SELF} reply [cht_x] [--file PATH ...] \"text\"")
        print(json.dumps(send_message(" ".join(args), chat_uid, files=files)))
    elif cmd == "react":
        flag = next((a for a in args if a in HELP or a.startswith("--")), None)
        if flag in HELP:
            print(__doc__.strip())
            return
        if flag:
            raise SystemExit(f"{flag!r} is not an option of react, nothing sent. "
                             f"usage: {SELF} react cht_x msg_y like|love|...|EMOJI|remove")
        args = args + ["", "", ""]
        print(json.dumps(react(*args[:3])))
    elif cmd == "api":
        if len(args) < 2 or not args[1].startswith("/v1/"):
            raise SystemExit(f"usage: {SELF} api GET|POST|PUT|PATCH|DELETE /v1/PATH ['JSON body']")
        status, data = api(args[0].upper(), args[1], json.loads(args[2]) if len(args) > 2 else None,
                           token=load_creds().get("token"))
        print(json.dumps(data, indent=2))
        if status >= 400:
            raise SystemExit(f"HTTP {status}")
    elif cmd == "status":
        creds, act = load_creds(), read_json(ACTIVATION, {})
        print(json.dumps({
            "activated": bool(creds.get("token")),
            "cloud": bool(creds.get("cloud")),
            "chat_uid": creds.get("chat_uid"),
            "text_this": f"Plow Activate: {act['display_code']}" if act.get("display_code") else None,
            "to": act.get("send_to"),
            "seeded_chats": read_json(STATE, {}).get("seeded_chats"),
        }, indent=2))
    elif cmd == "serve":
        creds = load_creds()
        creds = creds if creds.get("token") else activation_loop()
        bridge_loop(creds)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
