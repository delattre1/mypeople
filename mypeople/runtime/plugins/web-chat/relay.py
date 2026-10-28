#!/usr/bin/env python3
"""relay.py: the public end of the web-chat plugin. It runs on a small cloud VM (exe.dev), never on the owner's Mac.

It serves the page (page.html, next to it) and the visitor API, and keeps each visitor's thread. The owner's MyPlow
reaches it OUTBOUND only (web-chat.py serve long-polls /agent/inbox), so the Mac serves nothing to the internet.

Visitors (public):
    GET  /                       the landing page, its phone holding the chat (or the chat alone without landing.html)
    GET  /chat                   the chat
    POST /messages {text, page?} their message; the first one makes them a visitor: {token, message}, token shown once
    POST /react {message, emoji} their tapback on a message in their thread (one of iMessage's six, or "" to take it back)
    GET  /messages               their thread (Authorization: Bearer <token>)
    GET  /events?token=          their live thread: server-sent events message / reaction / tapback / typing

The owner's MyPlow (Authorization: Bearer <agent key>; only its sha256 is here, in AGENT_KEY_SHA256), and any MyPlow
a visitor was routed to (its key's sha256 is on that visitor). Each key sees only its own visitors: the owner's are the
ones routed nowhere.
    GET  /agent/inbox?after=<seq>&wait=25    after seq: {seq, visitor, kind: "message", id, text, page, first, tapbacks} or, once
                                             per handover, {seq, visitor, kind: "handover"} (no text: never the visitor's words).
                                             tapbacks: the visitor's tapbacks since their previous message, [{emoji, on, on_mine}]
                                             (on = the start of the message they reacted to), as Plow Chat hands tapbacks over
    POST /agent/reply {visitor, text}        shows on their page at once
    POST /agent/react {visitor, message, emoji}
    GET  /agent/thread?visitor=<id>          that visitor's thread, oldest first
    POST /agent/route {visitor, key_sha256}  owner only: from now on that key has the visitor (null: back to the owner)

    AGENT_KEY_SHA256=<64 hex> RELAY_DATA=~/web-chat RELAY_PORT=8000 python3 relay.py
"""
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DATA = Path(os.environ.get("RELAY_DATA") or Path.home() / "web-chat")
PAGE = Path(__file__).with_name("page.html")          # the chat, at /chat
LANDING = Path(__file__).with_name("landing.html")    # the landing page around it, at / (the chat alone when absent)
VERSIONS = ("a", "b", "c")                            # /?v=a is landing-a.html: other takes on the page, side by side
PORT = int(os.environ.get("RELAY_PORT") or 8000)
SLUG = re.compile(r"[a-z0-9-]{1,40}")
TAPBACKS = ("❤️", "👍", "👎", "😂", "‼️", "❓")  # iMessage's six


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


class Store:
    """sqlite, one connection per call: WAL + a busy timeout, so a reader never makes a write fail (the lab's lesson)."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.db() as db:
            db.execute("pragma journal_mode = wal")
            db.executescript("""
              create table if not exists visitors (id text primary key, sha256 text unique not null, page text, created_at text not null);
              create table if not exists messages (seq integer primary key autoincrement, id text unique not null,
                visitor text not null, mine integer not null, text text not null, created_at text not null, reactions text not null default '[]');
              create index if not exists messages_visitor on messages (visitor, seq);""")
            # Routing: the key that has the visitor (null = the owner), and handover items in the inbox (kind).
            # routed_seq: the holder sees this visitor's messages from that seq on (the past is in /agent/thread).
            # tapback: the visitor's own tapback on a message; tapbacks: when each was made (after_seq), for the agent.
            db.execute("create table if not exists tapbacks (seq integer primary key autoincrement, visitor text not null, "
                       "message_id text not null, emoji text not null, after_seq integer not null)")
            for table, column in (("visitors", "agent_sha256 text"), ("visitors", "routed_seq integer not null default 0"),
                                  ("messages", "kind text not null default 'message'"), ("messages", "tapback text")):
                if column.split()[0] not in [r["name"] for r in db.execute(f"pragma table_info({table})")]:
                    db.execute(f"alter table {table} add column {column}")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # autocommit; closed after each use
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def new_visitor(self, page):
        token, vid = secrets.token_urlsafe(24), f"w{secrets.token_hex(4)}"
        with self.db() as db:
            db.execute("insert into visitors (id, sha256, page, created_at) values (?, ?, ?, ?)", (vid, sha(token), page, now()))
        return vid, token

    def visitor(self, token):
        with self.db() as db:
            row = token and db.execute("select * from visitors where sha256 = ?", (sha(token),)).fetchone()
        return dict(row) if row else None

    def add(self, vid, mine, text):
        mid = f"m{secrets.token_hex(5)}"
        with self.db() as db:
            db.execute("insert into messages (id, visitor, mine, text, created_at) values (?, ?, ?, ?, ?)", (mid, vid, int(mine), text, now()))
            return dict(db.execute("select * from messages where id = ?", (mid,)).fetchone())

    def thread(self, vid, limit=200):
        with self.db() as db:
            rows = db.execute("select * from messages where visitor = ? and kind = 'message' order by seq desc limit ?", (vid, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def inbox(self, after, key):
        """What the visitors this key has wrote after seq, and its handovers. key None = the owner (unrouted visitors)."""
        with self.db() as db:
            rows = db.execute("""select m.seq, m.id, m.visitor, m.kind, m.text, m.created_at, v.page,
                not exists (select 1 from messages p where p.visitor = m.visitor and p.kind = 'message' and p.seq < m.seq) as first
                from messages m join visitors v on v.id = m.visitor
                where m.mine = 1 and m.seq > ? and m.seq >= v.routed_seq and v.agent_sha256 is ? order by m.seq limit 50""", (after, key))
            out = []
            for r in rows:
                if r["kind"] == "handover":
                    out.append({"seq": r["seq"], "visitor": r["visitor"], "kind": "handover"})
                    continue
                # The visitor's tapbacks made after their previous message and still standing, reach the agent with this one.
                prev = db.execute("select coalesce(max(seq), 0) from messages where visitor = ? and mine = 1 and kind = 'message' and seq < ?",
                                  (r["visitor"], r["seq"])).fetchone()[0]
                taps = db.execute("""select t.emoji, m.text, m.mine from tapbacks t join messages m on m.id = t.message_id
                    where t.visitor = ? and t.after_seq >= ? and t.after_seq < ? and t.emoji != '' and m.tapback = t.emoji
                    group by t.message_id order by max(t.seq)""", (r["visitor"], prev, r["seq"])).fetchall()
                out.append({**{k: r[k] for k in r.keys()},
                            "tapbacks": [{"emoji": t["emoji"], "on": t["text"][:80], "on_mine": not t["mine"]} for t in taps]})
            return out

    def holder(self, vid):
        """(exists, key sha256 that has the visitor or None for the owner)."""
        with self.db() as db:
            row = db.execute("select agent_sha256 from visitors where id = ?", (vid,)).fetchone()
        return (row is not None, row["agent_sha256"] if row else None)

    def route(self, vid, key):
        """Give the visitor to key (None: back to the owner); the new key gets one handover item."""
        with self.db() as db:
            db.execute("begin immediate")
            if key:
                seq = db.execute("insert into messages (id, visitor, mine, text, created_at, kind) values (?, ?, 1, '', ?, 'handover')",
                                 (f"h{secrets.token_hex(5)}", vid, now())).lastrowid
            else:  # back to the owner: from the next message on
                seq = (db.execute("select max(seq) from messages").fetchone()[0] or 0) + 1
            db.execute("update visitors set agent_sha256 = ?, routed_seq = ? where id = ?", (key, seq, vid))
            db.execute("commit")


    def tapback(self, vid, mid, emoji):
        """The visitor's tapback on a message of their thread ("" takes it back); False if it isn't theirs."""
        with self.db() as db:
            db.execute("begin immediate")
            n = db.execute("update messages set tapback = ? where id = ? and visitor = ? and kind = 'message'", (emoji or None, mid, vid)).rowcount
            if n:
                db.execute("insert into tapbacks (visitor, message_id, emoji, after_seq) values (?, ?, ?, (select coalesce(max(seq), 0) from messages))",
                           (vid, mid, emoji))
            db.execute("commit")
        return n == 1

    def react(self, vid, mid, emoji):
        with self.db() as db:
            n = db.execute("update messages set reactions = ? where id = ? and visitor = ?", (json.dumps([emoji] if emoji else []), mid, vid)).rowcount
        return n == 1

    def exists(self, vid):
        with self.db() as db:
            return db.execute("select 1 from visitors where id = ?", (vid,)).fetchone() is not None


def view(m):
    return {"id": m["id"], "mine": bool(m["mine"]), "text": m["text"], "created_at": m["created_at"],
            "reactions": json.loads(m["reactions"]), "tapback": m.get("tapback"), "files": []}


class Relay:
    def __init__(self, data=DATA, agent_sha=None):
        self.store = Store(Path(data) / "relay.sqlite")
        self.agent_sha = agent_sha if agent_sha is not None else os.environ.get("AGENT_KEY_SHA256", "")
        self.streams = {}                    # visitor id -> set of queues, one per open page
        self.lock = threading.Lock()
        self.arrived = threading.Condition()  # a visitor wrote: wake the agent's long poll
        self.hits = {}

    def push(self, vid, event, data):
        with self.lock:
            for q in self.streams.get(vid, ()):
                q.put((event, data))

    def limited(self, key, most, seconds):
        """ponytail: in memory, gone on restart, one process. A shared store if the relay ever runs twice."""
        t = time.time()
        with self.lock:
            if len(self.hits) > 20000:
                self.hits.clear()
            recent = [x for x in self.hits.get(key, []) if x > t - seconds] + [t]
            self.hits[key] = recent
        return len(recent) > most

    def agent(self, handler):
        """"owner", or the sha256 of any other key: a new MyPlow polls before the owner routes it a visitor, and a key
        sees nothing until one is routed to it (its inbox is an empty long-poll; every visitor call is a 404)."""
        got = handler.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not got:
            return None
        got = sha(got)
        return "owner" if self.agent_sha and hmac.compare_digest(got, self.agent_sha) else got


class Fail(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def handler_for(relay):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # no visitor text or tokens in the log: method, path and status only
            pass

        def send_json(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 16384:
                raise Fail(413, "too long")
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise Fail(400, "send JSON") from None

        def client(self):
            return (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or self.client_address[0]

        def visitor(self, token=None):
            v = relay.store.visitor(token or self.headers.get("Authorization", "").removeprefix("Bearer ").strip())
            if not v:
                raise Fail(401, "unknown visitor: start a new chat")
            return v

        def run(self, method):
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            try:
                if method == "GET" and url.path in ("/", "/chat"):
                    v = q.get("v", [""])[0]
                    alt = LANDING.with_name(f"landing-{v}.html") if v in VERSIONS else None
                    page = (alt if url.path == "/" and alt and alt.exists()
                            else LANDING if url.path == "/" and LANDING.exists() else PAGE).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    return self.wfile.write(page)
                if method == "POST" and url.path == "/messages":
                    return self.post_message()
                if method == "POST" and url.path == "/react":
                    return self.post_tapback()
                if method == "GET" and url.path == "/messages":
                    return self.send_json(200, {"messages": [view(m) for m in relay.store.thread(self.visitor()["id"])]})
                if method == "GET" and url.path == "/events":
                    return self.events(self.visitor((q.get("token") or [""])[0]))
                if url.path.startswith("/agent/"):
                    who = relay.agent(self)
                    if not who:
                        raise Fail(404, "not found")
                    if relay.limited(f"agent {self.client()}", 120, 60):   # a real bridge polls a few times a minute
                        raise Fail(429, "slow down")
                    return self.agent_call(method, url.path, q, who)
                raise Fail(404, "not found")
            except Fail as e:
                self.send_json(e.status, {"error": {"status": e.status, "message": str(e)}})
            except Exception as e:  # noqa: BLE001 - an answer, not a dropped connection; the type only, never a visitor's text
                print(f"relay error on {method} {url.path}: {type(e).__name__}", flush=True)
                self.send_json(500, {"error": {"status": 500, "message": "relay error"}})

        def post_message(self):
            b = self.body()
            text = b.get("text").strip() if isinstance(b.get("text"), str) else ""
            page = b.get("page")
            if not text or len(text) > 2000:
                raise Fail(422, "text is 1 to 2000 characters")
            if page is not None and not SLUG.fullmatch(str(page)):
                raise Fail(422, "page is the slug of the page they came from")
            ip, token = self.client(), None
            if self.headers.get("Authorization"):
                v = self.visitor()
            else:
                # 60 an hour: a household (or a demo room) shares one address, and each page version and each Start over is
                # a new chat; 5 an hour turned the sixth away (10-28). ponytail: per address only, a shared store if it ever
                # needs to stop a real flood.
                if relay.limited(f"new {ip}", 60, 3600):
                    raise Fail(429, "too many new chats from here; try again in an hour")
                vid, token = relay.store.new_visitor(page)
                v = {"id": vid}
            if relay.limited(f"msg {v['id']}", 12, 60) or relay.limited(f"msg {ip}", 60, 60):
                raise Fail(429, "slow down a little")
            m = relay.store.add(v["id"], True, text)
            relay.push(v["id"], "message", view(m))
            with relay.arrived:
                relay.arrived.notify_all()
            self.send_json(201, {**({"token": token} if token else {}), "message": view(m)})

        def post_tapback(self):
            v, b = self.visitor(), self.body()
            emoji = b.get("emoji") or ""
            if emoji and emoji not in TAPBACKS:
                raise Fail(422, "a tapback is one of " + " ".join(TAPBACKS) + ', or "" to take it back')
            if relay.limited(f"msg {v['id']}", 30, 60):
                raise Fail(429, "slow down a little")
            if not relay.store.tapback(v["id"], str(b.get("message") or ""), emoji):
                raise Fail(404, "no such message in your chat")
            relay.push(v["id"], "tapback", {"message_id": b.get("message"), "emoji": emoji})
            self.send_json(200, {"message": b.get("message"), "tapback": emoji or None})

        def events(self, v):
            q = queue.Queue()
            with relay.lock:
                relay.streams.setdefault(v["id"], set()).add(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.wfile.write(b": open\n\n")
                self.wfile.flush()
                while True:
                    try:
                        event, data = q.get(timeout=25)
                        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except OSError:  # the page went away
                pass
            finally:
                with relay.lock:
                    relay.streams.get(v["id"], set()).discard(q)
                self.close_connection = True

        def agent_call(self, method, path, q, who):
            key = None if who == "owner" else who   # the owner's visitors are the ones routed to no key

            def mine(vid):  # a visitor this key has; anyone else's reads as unknown
                found, holder = relay.store.holder(vid)
                if not found or holder != key:
                    raise Fail(404, f"no visitor {vid}")
                return vid

            if method == "GET" and path == "/agent/inbox":
                after, wait = int((q.get("after") or ["0"])[0]), min(float((q.get("wait") or ["0"])[0]), 50)
                end = time.time() + wait
                rows = relay.store.inbox(after, key)
                while not rows and time.time() < end:
                    with relay.arrived:
                        relay.arrived.wait(end - time.time())
                    rows = relay.store.inbox(after, key)
                return self.send_json(200, {"messages": rows})
            if method == "GET" and path == "/agent/thread":
                vid = mine((q.get("visitor") or [""])[0])
                return self.send_json(200, {"visitor": vid, "messages": [view(m) for m in relay.store.thread(vid)]})
            b = self.body()
            if method == "POST" and path == "/agent/route":
                new = b.get("key_sha256")
                if who != "owner":
                    raise Fail(403, "only the owner routes visitors")
                if new is not None and not re.fullmatch(r"[0-9a-f]{64}", str(new)):
                    raise Fail(422, "key_sha256 is the 64-hex sha256 of the new key, or null")
                vid = str(b.get("visitor") or "")
                if not relay.store.holder(vid)[0]:
                    raise Fail(404, f"no visitor {vid}")
                relay.store.route(vid, new)
                with relay.arrived:
                    relay.arrived.notify_all()
                return self.send_json(200, {"visitor": vid, "routed": bool(new)})
            vid = mine(str(b.get("visitor") or ""))
            if method == "POST" and path == "/agent/reply":
                text = str(b.get("text") or "").strip()
                if not text:
                    raise Fail(422, "empty reply")
                m = relay.store.add(vid, False, text)
                relay.push(vid, "message", view(m))
                return self.send_json(201, {"visitor": vid, "message": m["id"], "live": len(relay.streams.get(vid, ()))})
            if method == "POST" and path == "/agent/react":
                emoji = str(b.get("emoji") or "")
                if not relay.store.react(vid, str(b.get("message") or ""), emoji):
                    raise Fail(404, "no such message from that visitor")
                relay.push(vid, "reaction", {"message_id": b.get("message"), "emoji": emoji})
                return self.send_json(200, {"visitor": vid, "message": b.get("message")})
            raise Fail(404, "not found")

        def do_GET(self):
            self.run("GET")

        def do_POST(self):
            self.run("POST")

    return Handler


def serve(relay=None, port=PORT, host="0.0.0.0"):
    relay = relay or Relay()
    server = ThreadingHTTPServer((host, port), handler_for(relay))
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    if not re.fullmatch(r"[0-9a-f]{64}", os.environ.get("AGENT_KEY_SHA256", "")):
        raise SystemExit("AGENT_KEY_SHA256 must be the sha256 (64 hex) of the agent key")
    s = serve()
    print(f"web-chat relay on :{s.server_address[1]}, data in {DATA}", flush=True)
    s.serve_forever()
