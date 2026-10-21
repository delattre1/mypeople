#!/usr/bin/env python3
"""A2A: an agent on one MyPlow messages an agent on another MyPlow with plain `mp send`.

    mp send <host>/<session>:<tab> "..."     <host> is a paired MyPlow: the message crosses the relay

mp send hands a message for another host to the queue, which holds it for that host's client. For a
paired host this bridge is that client: it takes the host's tasks from the local queue and puts them in
the relay, and the peer's bridge hands each to its own queue as an ordinary send, stamped

    [from <sender's full id>, on another MyPlow] <text>

by the receiver from the pairing, never from the text. The reply is plain mp send to that id, and the
Stop hook's "finished" notice comes back the same way. The peer's bridge sends back how its delivery
went, so mp send prints the same "sent" as a local send. Only `send` crosses: peek, spawn, kill and the
rest for a paired host are refused.

Relay (`a2a.py relay`): a store-and-forward mailbox over HTTPS, so a MyPlow behind any home network only
makes outgoing calls. Each pair has one box per direction, named by a token only the pair can derive
(the relay keeps its hash), and every message is signed with the pair key and checked on arrival, so not
even the relay can forge one. It can read the text: TLS covers the wire, not the relay.
Loop guard: 20 messages between two agents, each within 30 minutes of the last, and further sends are
refused and incoming ones dropped until 30 quiet minutes.

Pairing, by the owner, once per peer:

    mp pair                  prints a one-time code (24 h)
    mp pair <code>           run on the other MyPlow: pairs the two
    mp pair ls | mp pair rm <host>

Config (env or queue.env): A2A_RELAY, default ours below. supervise.sh runs the bridge once a pairing or
an invite exists (state/a2a/keys.json).

    a2a.py serve             the bridge
    a2a.py relay [port]      the relay (A2A_RELAY_DB: its sqlite file)
"""
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SELF = Path(__file__).resolve()
sys.path.insert(0, str(SELF.parents[2] / "bin"))
import mpcommon as C  # noqa: E402

# ponytail: one relay on our home server (delattre-server, Tailscale Funnel on its own port 8443). Its
# ceiling is that box; the upgrade is the same file on a VPS and a new default here.
RELAY = "https://delattre-server.mulley-firefighter.ts.net:8443/a2a"
STATE_DIR = Path(os.environ.get("A2A_STATE_DIR") or Path(C.CFG["INSTALL_DIR"]) / "state" / "a2a")
INVITE_TTL = 24 * 3600
MAX_HOPS, QUIET = 20, 30 * 60
RECEIPT_WAIT = 25           # mp send waits 30 s for its result
KEEP = 7 * 24 * 3600        # a message the relay or a bridge still holds after this is dropped
MAX_MSG, MAX_BOX, MAX_ROWS = 16 * 1024, 200, 20000
# ponytail: each bridge asks the relay every POLL seconds, so a message waits up to ~1 s and the relay
# takes one request per second per MyPlow. Many MyPlows would want a long-poll /fetch instead.
POLL = 1
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")
AGENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}(/[A-Za-z0-9._@-]{1,64}:[A-Za-z0-9._@-]{1,64})?")


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S ") + "[a2a] " + msg, flush=True)


def mac(key, *parts):
    return hmac.new(bytes.fromhex(key), "\0".join(parts).encode(), hashlib.sha256).hexdigest()


def box(key, frm, to):
    """The token of the box messages from host frm to host to go through: only the pair can make it."""
    return mac(key, "box", frm, to)


def sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


_getaddrinfo = socket.getaddrinfo


def resolve(host, *a, **k):
    """socket.getaddrinfo, plus public DNS for a *.ts.net name the system refuses: Tailscale's MagicDNS refuses
    another tailnet's ts.net names, so a MyPlow on a machine in any other tailnet could not find the relay.
    TLS is still checked against the name. ponytail: only because the relay's name is a Funnel (ts.net) one;
    a relay on an ordinary domain drops this."""
    try:
        return _getaddrinfo(host, *a, **k)
    except socket.gaierror:
        if not str(host).endswith(".ts.net"):
            raise
        ips = public_ips(host)
        if not ips:
            raise
        return _getaddrinfo(ips[0], *a, **k)


_public = {}     # host -> (addresses, when asked): one public lookup per 5 minutes, not one per request


def public_ips(host):
    ips, at = _public.get(host, ([], 0))
    if time.time() - at > 300:
        req = urllib.request.Request("https://1.1.1.1/dns-query?type=A&name=" + host,
                                     headers={"Accept": "application/dns-json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            ips = [x["data"] for x in json.load(r).get("Answer") or [] if x.get("type") == 1]
        _public[host] = (ips, time.time())
    return ips


def post(url, body, timeout=10):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


# ---------------------------------------------------------------- relay
def relay(port=8499, db_path=None, bind="127.0.0.1"):
    db_path = db_path or os.environ.get("A2A_RELAY_DB") or str(Path.home() / ".local/share/a2a-relay/relay.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
    db.execute("create table if not exists m (id integer primary key, box text, at real, msg text)")
    db.execute("create index if not exists m_box on m (box)")
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def answer(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.answer(200, {"ok": True})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n)) if 0 < n <= 4 * MAX_MSG else None
            except ValueError:
                req = None
            if not isinstance(req, dict):
                return self.answer(400, {"error": "bad request"})
            if self.path.endswith("/put"):
                return self.put(req)
            if self.path.endswith("/fetch"):
                return self.fetch(req)
            self.answer(404, {"error": "not found"})

        def put(self, req):
            token, msg = req.get("box"), req.get("msg")
            if not (isinstance(token, str) and isinstance(msg, str) and 32 <= len(token) <= 128):
                return self.answer(400, {"error": "bad request"})
            if len(msg) > MAX_MSG:
                return self.answer(413, {"error": "message over %d bytes" % MAX_MSG})
            with lock:
                db.execute("delete from m where at < ?", (time.time() - KEEP,))
                if db.execute("select count(*) from m where box = ?", (sha(token),)).fetchone()[0] >= MAX_BOX:
                    return self.answer(429, {"error": "box full"})
                if db.execute("select count(*) from m").fetchone()[0] >= MAX_ROWS:
                    return self.answer(507, {"error": "relay full"})
                cur = db.execute("insert into m (box, at, msg) values (?, ?, ?)", (sha(token), time.time(), msg))
            self.answer(200, {"id": cur.lastrowid})

        def fetch(self, req):
            """The messages in the boxes named, after deleting the ones acknowledged (they were handled)."""
            tokens, ack = req.get("boxes"), req.get("ack") or []
            if not (isinstance(tokens, list) and isinstance(ack, list) and len(tokens) <= 500
                    and all(isinstance(t, str) for t in tokens) and all(isinstance(i, int) for i in ack)):
                return self.answer(400, {"error": "bad request"})
            boxes = {sha(t): t for t in tokens}
            if not boxes:
                return self.answer(200, {"msgs": []})
            marks = ",".join("?" * len(boxes))
            with lock:
                for i in ack:
                    db.execute("delete from m where id = ? and box in (%s)" % marks, (i, *boxes))
                rows = db.execute("select id, box, msg from m where box in (%s) order by id limit 100" % marks,
                                  tuple(boxes)).fetchall()
            self.answer(200, {"msgs": [{"id": i, "box": boxes[b], "msg": m} for i, b, m in rows]})

    log("relay on %s:%s, %s" % (bind, port, db_path))
    ThreadingHTTPServer((bind, int(port)), Handler).serve_forever()


# ---------------------------------------------------------------- the bridge
class Bridge:
    def __init__(self, host=None, state_dir=None, queue_url=None, secret=None, relay_url=None):
        self.host = host or C.CFG["HOST_ID"]
        self.dir = Path(state_dir or STATE_DIR)
        self.queue_url = queue_url or C.CFG["QUEUE_URL"]
        self.secret = secret if secret is not None else C.CFG.get("QUEUE_SECRET", "")
        self.relay = relay_url or os.environ.get("A2A_RELAY") or C.CFG.get("A2A_RELAY") or RELAY
        self.acks = {}        # relay -> ids handled, acknowledged with the next fetch
        self.waiting = {}     # signature -> (task id, sent at): our send, until the peer's receipt
        self.inflight = {}    # local task id -> (peer, signature): a delivery, until its local result
        self.down = set()     # relays that failed last time: logged once, retried quietly
        st = self.read("state.json", {})
        self.seen, self.conv = st.get("seen", []), st.get("conv", {})

    # -- files: keys.json is shared with `mp pair`, so it is changed under a lock
    def read(self, name, default):
        try:
            return json.loads((self.dir / name).read_text())
        except (OSError, ValueError):
            return default

    def write(self, name, obj):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(obj))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.dir / name)

    def change_keys(self, fn):
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / "keys.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            keys = self.read("keys.json", {})
            keys.setdefault("peers", {})
            keys.setdefault("invites", {})
            out = fn(keys)
            self.write("keys.json", keys)
            return out

    def queue(self, method, path, body=None):
        return C.http_json(method, self.queue_url + path, body, {"X-Queue-Secret": self.secret}, timeout=8)

    def sign(self, key, body):
        return json.dumps({"b": body, "s": mac(key, json.dumps(body, sort_keys=True))})

    def hop(self, a, b, count=True):
        """Count one message between agents a and b; False past the loop guard (nothing counted)."""
        k = "|".join(sorted((a, b)))
        n, at = self.conv.get(k, (0, 0))
        if time.time() - at > QUIET:
            n = 0
        if n >= MAX_HOPS:
            return False
        if count:
            self.conv[k] = [n + 1, time.time()]
        return True

    # -- pairing
    def invite(self):
        s = secrets.token_hex(32)
        self.change_keys(lambda k: k["invites"].__setitem__(mac(s, "hello"), {"s": s, "at": time.time()}))
        code = json.dumps({"h": self.host, "s": s, "r": self.relay}, separators=(",", ":"))
        return "mpp-" + base64.urlsafe_b64encode(code.encode()).decode().rstrip("=")

    def accept(self, code):
        try:
            raw = code.strip().removeprefix("mpp-")
            c = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            host, s, relay_url = c["h"], c["s"], c["r"]
            bytes.fromhex(s)
        except (ValueError, KeyError, TypeError):
            raise SystemExit("refused: that is not a pairing code (mp pair prints one)")
        if not HOST.fullmatch(host) or host == self.host:
            raise SystemExit("refused: that code is this MyPlow's own (%s)" % host)
        if host in self.read("keys.json", {}).get("peers", {}):
            raise SystemExit("refused: %s is paired already; to pair it again, mp pair rm %s first" % (host, host))
        n = secrets.token_hex(16)
        try:
            post(relay_url.rstrip("/") + "/put", {"box": mac(s, "hello"), "msg": json.dumps(
                {"h": self.host, "n": n, "sig": mac(s, "hello", self.host, n)})})
        except (OSError, ValueError) as e:
            raise SystemExit("not paired: the relay %s did not take it (%s). Nothing changed; try again." % (relay_url, e))
        self.change_keys(lambda k: k["peers"].__setitem__(host, {"key": mac(s, "key", n), "relay": relay_url}))
        return ("paired with %s: its agents are reachable as %s/<session>:<tab>, once its MyPlow is on to read "
                "this pairing (the code is now spent)" % (host, host))

    def hello(self, token, msg):
        def take(keys):
            inv = keys["invites"].pop(token, None)
            try:
                h = json.loads(msg)
                ok = inv and hmac.compare_digest(h["sig"], mac(inv["s"], "hello", h["h"], h["n"]))
            except (ValueError, KeyError, TypeError):
                ok = False
            if not ok:
                if inv:
                    keys["invites"][token] = inv      # not a real answer to it: the invite stays open
                return None
            if not HOST.fullmatch(h["h"]) or h["h"] == self.host or h["h"] in keys["peers"]:
                # A name already paired would take that peer's messages: refused, and the code is spent.
                log("refused a pairing from a MyPlow calling itself %r: that name is taken here" % h["h"])
                return None
            keys["peers"][h["h"]] = {"key": mac(inv["s"], "key", h["n"]), "relay": self.relay}
            return h["h"]
        host = self.change_keys(take)
        if host:
            log("paired with %s" % host)

    # -- one pass: fetch and deliver, send what the queue holds for peers, settle receipts
    def step(self):
        def expire(k):
            k["invites"] = {t: i for t, i in k["invites"].items() if time.time() - i["at"] < INVITE_TTL}
            return k
        keys = self.read("keys.json", {})
        if any(time.time() - i["at"] >= INVITE_TTL for i in keys.get("invites", {}).values()):
            keys = self.change_keys(expire)
        peers = keys.get("peers", {})
        boxes = {}      # relay -> {token: (peer or None for an invite)}
        for h, p in peers.items():
            boxes.setdefault(p["relay"], {})[box(p["key"], h, self.host)] = h
        for t in keys.get("invites", {}):
            boxes.setdefault(self.relay, {})[t] = None
        up = set()
        for url, names in boxes.items():
            ack = self.acks.pop(url, [])
            try:
                msgs = post(url.rstrip("/") + "/fetch", {"boxes": list(names), "ack": ack})["msgs"]
            except (OSError, ValueError, KeyError) as e:
                self.acks[url] = ack
                if url not in self.down:
                    log("relay %s unreachable, retrying quietly: %s" % (url, e))
                self.down.add(url)
                continue
            if url in self.down:
                log("relay %s back" % url)
            self.down.discard(url)
            up.add(url)
            for m in msgs:
                h = names[m["box"]]
                if h is None:
                    self.hello(m["box"], m["msg"])
                elif not self.take(h, peers[h], m["msg"]):
                    continue        # the local queue is down: it stays in the relay for the next pass
                self.acks.setdefault(url, []).append(m["id"])
        for h, p in peers.items():
            if p["relay"] in up:
                self.send(h, p)
        self.settle(peers)
        self.seen = self.seen[-1000:]
        self.write("state.json", {"seen": self.seen, "conv": {
            k: v for k, v in self.conv.items() if time.time() - v[1] < QUIET}})

    def take(self, h, peer, raw):
        """Handle one message from peer h. False only when it should be fetched again (queue down)."""
        try:
            env = json.loads(raw)
            body, sig = env["b"], env["s"]
            good = (isinstance(body, dict) and isinstance(body.get("ts"), (int, float))
                    and hmac.compare_digest(sig, mac(peer["key"], json.dumps(body, sort_keys=True))))
        except (ValueError, KeyError, TypeError):
            good = False
        if not good:
            log("dropped a message from %s's box: bad signature" % h)
            return True
        if sig in self.seen or time.time() - body["ts"] > KEEP:
            return True
        if body.get("kind") == "receipt":
            w = self.waiting.pop(body.get("re"), None)
            if w:
                self.queue("POST", "/task/result", {"task_id": w[0], "ok": bool(body.get("ok")),
                                                     "result": str(body.get("result"))[:500]})
            self.seen.append(sig)
            return True
        frm, to = str(body.get("from")), str(body.get("to"))
        if not (AGENT.fullmatch(frm) and AGENT.fullmatch(to) and C.parse_agent_id(frm)[0] == h
                and C.parse_agent_id(to)[0] == self.host and "/" in to):
            log("dropped a message from %s: from %s to %s" % (h, frm, to))
            return True
        if not self.hop(to, frm, count=False):
            log("dropped a message from %s to %s: loop guard (%d in a row)" % (frm, to, MAX_HOPS))
            self.seen.append(sig)
            return True
        payload = {"message": "[from %s, on another MyPlow] %s" % (frm, body.get("text", ""))}
        if body.get("reply"):
            payload["reply_to"] = frm
        code, out = self.queue("POST", "/task/submit", {"type": "send", "target_agent": to, "payload": payload})
        if code != 200 or not isinstance(out, dict) or not out.get("task_id"):
            return False
        self.hop(to, frm)
        self.inflight[out["task_id"]] = (h, sig, time.time())
        self.seen.append(sig)
        return True

    def send(self, h, peer):
        """Every task the local queue holds for peer h: a send crosses, anything else is refused."""
        code, tasks = self.queue("GET", "/task/poll?hostname=" + h)
        for t in tasks if code == 200 and isinstance(tasks, list) else []:
            tid, to, payload = t["task_id"], t.get("target_agent", ""), t.get("payload") or {}
            if C.parse_agent_id(to)[0] != h:
                # A task with no host goes to any poller, and it means this host: hand it back, named so.
                self.queue("POST", "/task/submit", {"type": t["type"], "target_agent": "%s/%s" % (self.host, to),
                                                    "payload": payload})
                continue
            if t["type"] != "send":
                self.done(tid, False, "refused: %s is another MyPlow, paired for mp send only" % h)
                continue
            frm = payload.get("reply_to") or ""
            if C.parse_agent_id(frm)[0] != self.host:
                frm = ""
            if not self.hop(frm or self.host, to):
                self.done(tid, False, "refused: %s and %s exchanged %d messages, each within 30 minutes of the "
                                      "last (loop guard); it reopens after 30 quiet minutes" % (frm or self.host, to, MAX_HOPS))
                continue
            body = {"kind": "send", "from": frm or self.host, "to": to, "text": payload.get("message", ""),
                    "reply": bool(frm), "ts": time.time(), "n": secrets.token_hex(8)}
            msg = self.sign(peer["key"], body)
            try:
                post(peer["relay"].rstrip("/") + "/put", {"box": box(peer["key"], self.host, h), "msg": msg})
            except (OSError, ValueError) as e:
                self.done(tid, False, "not sent: the relay refused or is unreachable (%s)" % e)
                continue
            self.waiting[json.loads(msg)["s"]] = (tid, time.time())

    def settle(self, peers):
        for sig, (tid, at) in list(self.waiting.items()):
            if time.time() - at > RECEIPT_WAIT:
                del self.waiting[sig]
                self.done(tid, True, "queued: that MyPlow is not reading its mailbox now; it gets the message "
                                     "when it is back")
        for tid, (h, sig, at) in list(self.inflight.items()):
            code, st = self.queue("GET", "/task/" + tid)
            if code == 200 and isinstance(st, dict) and st.get("ok") is not None and h in peers:
                body = {"kind": "receipt", "re": sig, "ok": bool(st["ok"]), "result": str(st.get("result"))[:500],
                        "ts": time.time(), "n": secrets.token_hex(8)}
                try:
                    post(peers[h]["relay"].rstrip("/") + "/put", {"box": box(peers[h]["key"], self.host, h),
                                                                  "msg": self.sign(peers[h]["key"], body)})
                except (OSError, ValueError):
                    continue        # tried again next pass
            elif time.time() - at < 60:
                continue
            del self.inflight[tid]

    def done(self, tid, ok, result):
        self.queue("POST", "/task/result", {"task_id": tid, "ok": ok, "result": result})

    def serve(self):
        log("up as %s, relay %s" % (self.host, self.relay))
        while True:
            try:
                self.step()
            except Exception as e:      # one bad pass must not end the bridge
                log("pass failed: %r" % e)
            time.sleep(POLL)


def pair(argv):
    b = Bridge()
    if not argv:
        print("Pairing code for %s, good for 24 h and one use. On the other MyPlow, its owner runs:\n\n"
              "    mp pair %s\n" % (b.host, b.invite()))
    elif argv == ["ls"]:
        keys = b.read("keys.json", {})
        print("paired: %s\nopen invites: %d" % (", ".join(sorted(keys.get("peers", {}))) or "none",
                                                len(keys.get("invites", {}))))
    elif argv[:1] == ["rm"] and len(argv) == 2:
        gone = b.change_keys(lambda k: k["peers"].pop(argv[1], None))
        print(("unpaired %s" if gone else "%s was not paired") % argv[1])
    elif len(argv) == 1:
        print(b.accept(argv[0]))
    else:
        raise SystemExit("usage: mp pair | mp pair <code> | mp pair ls | mp pair rm <host>")


def main(argv):
    cmd, args = (argv[1] if len(argv) > 1 else "serve"), argv[2:]
    socket.getaddrinfo = resolve        # this process only: the bridge and mp pair reach the relay by name
    if cmd == "serve":
        Bridge().serve()
    elif cmd == "relay":
        relay(*args[:1])
    elif cmd == "pair":
        pair(args)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
