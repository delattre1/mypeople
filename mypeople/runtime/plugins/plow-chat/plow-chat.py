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
the chats come from GET /v1/agents/cloud/me.
"""
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = (os.environ.get("PLOW_CHAT_BASE_URL") or os.environ.get("PLOW_API_BASE")
        or "https://api.plow.co").rstrip("/")
INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME")
               or Path.home() / "mypeople")
STATE_DIR = Path(os.environ.get("PLOW_CHAT_STATE_DIR") or INSTALL / "state" / "plow-chat")
CREDS = STATE_DIR / "creds.json"          # {"token": ..., "chat_uid": ...}
ACTIVATION = STATE_DIR / "activation.json"
ATTACHMENTS = STATE_DIR / "attachments"   # inbound photos/files, for the Boss to open
STATE = STATE_DIR / "state.json"          # {"seen_ids": [...], "seeded_chats": [...]}
HOST_ID = os.environ.get("HOST_ID") or os.uname().nodename.split(".")[0]
BOSS_AGENT = os.environ.get("BOSS_AGENT") or f"{HOST_ID}/main:Boss"
MP_BIN = os.environ.get("MP_BIN") or str(INSTALL / "bin" / "mp")
# ponytail: polls every chat each POLL_SECONDS instead of holding Plow's
# websocket, so the plugin needs no dependency beyond the stdlib. Ceiling: a
# text lands up to POLL_SECONDS late and each pass costs 1 + chats requests;
# upgrade path is the account websocket (POST /v1/ws/ticket) via `websockets`.
POLL_SECONDS = float(os.environ.get("PLOW_CHAT_POLL_SECONDS", "5"))
SELF = str(Path(__file__).resolve())
STATE_LOCK = threading.Lock()
WELCOME = "MyPlow is up. Text me what you want done."


def log(msg: str):
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] plow-chat: {msg}",
          flush=True)


def api(method: str, path: str, body=None, token=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


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

def envelope(who: str, text: str, chat_uid: str, files=(), context=(), mid: str = "") -> str:
    # Plow seats whoever the carrier reports in the thread, so the Boss is told
    # who actually spoke and which thread — in a group these differ per message.
    sent = "".join(f"\n[attached, open it: {f}]" for f in files)
    said = "".join(f"\n({c})" for c in context)
    tap = (f" To tapback this message, run: python3 {SELF} react {chat_uid} {mid} like "
           f"(or love, laugh, emphasize, question, dislike, or one emoji).") if mid else ""
    # The owner's ask: feedback the moment an agent sees his text, not after the slow turn. It is
    # the first line, so it is the first command run, not one noticed mid-work (Boss, 10-05).
    seen = (f"FIRST, before reading on: python3 {SELF} react {chat_uid} {mid} 👀"
            f"  (marks this text seen for the sender; then read, work and reply)\n") if mid else ""
    return (
        f"{seen}[plowchat] from {who} in {chat_uid}: {text}{sent}{said}\n"
        f"(This is the owner's Plow messages line. The owner texts here, and so "
        f"does anyone they added to this thread. To answer, run: "
        f"python3 {SELF} reply {chat_uid} \"your reply\" — it goes back to that "
        f"same thread, which may be a group. Plain text only, no markdown. To send "
        f"photos or files, add --file PATH before the text, up to 4.{tap})"
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
        chats = st.get("tapback_chats", [])
        seen = st.get("seen_tapbacks", [])
        new = [k for k in live if k not in seen]
        if not new and chat_uid in chats:
            return 0
        # First sight of a chat with seeding on: its tapbacks are history, not news.
        news = [live[k] for k in new] if chat_uid in chats or not seed else []
        st["pending_tapbacks"] = (st.get("pending_tapbacks", []) + news)[-20:]
        st["seen_tapbacks"] = (seen + new)[-1000:]
        st["tapback_chats"] = sorted(set(chats) | {chat_uid})
        write_json(STATE, st)
    return len(news)


def send_to_boss(message: str) -> bool:
    try:
        r = subprocess.run([sys.executable, MP_BIN, "send", BOSS_AGENT, message],
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


def route_message(message: dict) -> str:
    """Deliver one inbound message to the Boss, exactly once."""
    if message.get("direction") != "inbound":
        return "skip: outbound"
    mid = message.get("uid") or ""
    text = (message.get("body") or "").strip()
    atts = message.get("attachments") or []
    # A photo with no caption is a message too: skipping it silently read as the
    # Boss ignoring the owner.
    if not mid or not (text or atts):
        return "skip: empty"

    # Claim before sending, so an overlapping pass cannot wake the Boss twice.
    with STATE_LOCK:
        st = read_json(STATE, {})
        seen = list(st.get("seen_ids", []))
        if mid in seen:
            return "skip: already routed"
        st["seen_ids"] = (seen + [mid])[-1000:]
        write_json(STATE, st)

    who = speaker(message.get("sender") or {})
    chat_uid = message.get("chat_uid") or ""
    files = []
    for a in atts:
        try:
            files.append(download(a))
        except (OSError, ValueError) as e:
            log(f"attachment {a.get('uid')} not saved: {e}")
            files.append(f"(could not download {a.get('filename') or 'attachment'}: {e})")
    with STATE_LOCK:
        taps = read_json(STATE, {}).get("pending_tapbacks", [])
    if send_to_boss(envelope(who, text, chat_uid, files, context(message) + taps, mid)):
        if taps:
            with STATE_LOCK:
                st = read_json(STATE, {})
                st["pending_tapbacks"] = [t for t in st.get("pending_tapbacks", []) if t not in taps]
                write_json(STATE, st)
        return f"ROUTE {chat_uid} {who}: {text[:60]}"
    with STATE_LOCK:  # release the claim so the next pass retries it
        st = read_json(STATE, {})
        st["seen_ids"] = [i for i in st.get("seen_ids", []) if i != mid]
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
        # Live read every pass: a group the line joins later is a new chat.
        status, data = api("GET", "/v1/agents/cloud/me", token=creds["token"])
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

    Seeding is per chat: a group joined months in carries its own history, and
    replaying it would hand the Boss a fake inbox. Only chats already there when
    the bridge starts are seeded; one that appears while it runs was started by
    a text, and swallowing that text would leave the sender unanswered.
    """
    status, data = api("GET", f"/v1/chats/{chat_uid}/messages", token=creds["token"])
    if status >= 400:
        log(f"read failed {chat_uid} {status}: {json.dumps(data)[:160]}")
        return 0
    msgs = listing(data)
    with STATE_LOCK:
        st = read_json(STATE, {})
        seeded = set(st.get("seeded_chats", []))
        first = chat_uid not in seeded
        if first and not seed:
            st["seeded_chats"] = sorted(seeded | {chat_uid})
            write_json(STATE, st)
            first = False
        if first:
            seen = list(st.get("seen_ids", []))
            seen += [m["uid"] for m in msgs if m.get("uid") and m["uid"] not in seen]
            st["seen_ids"] = seen[-1000:]
            st["seeded_chats"] = sorted(seeded | {chat_uid})
            write_json(STATE, st)
    note_tapbacks(msgs, chat_uid, seed)
    if first:
        log(f"seeded {len(msgs)} existing message(s) in {chat_uid} as already handled")
        if creds.get("cloud"):
            # A fresh cloud agent's first text was the signup phrase, which the
            # seed just swallowed; without this the new owner hears nothing.
            try:
                send_message(WELCOME, chat_uid, creds)
            except SystemExit as e:  # a failed hello must not take the bridge down
                log(f"welcome not sent: {e}")
        return 0
    routed = 0
    for m in sorted(msgs, key=lambda m: m.get("created_at") or ""):
        r = route_message(m)
        if not r.startswith("skip"):
            log(r)
        routed += r.startswith("ROUTE")
    return routed


def bridge_loop(creds: dict):
    log(f"bridge up, routing to {BOSS_AGENT}")
    seed = True
    while True:
        try:
            for uid in list_chats(creds):
                poll_chat(creds, uid, seed)
            seed = False
        except (OSError, ValueError) as e:
            log(f"poll error: {e}")
        time.sleep(POLL_SECONDS)


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
        bridge_loop(creds if creds.get("token") else activation_loop())
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
