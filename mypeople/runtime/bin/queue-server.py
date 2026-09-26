#!/usr/bin/env python3
"""MyPlow queue-server: HTTP queue + registry + reaper + /dashboard + /roster.
Binds BIND_ADDR:HUD_PORT (9900). Reverse-proxies TODO routes to TODO_PORT so both
front doors serve both pages (symmetric cross-nav, §ITEM-2). Python stdlib only."""
import os, sys, json, time, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpcommon as C

CFG = C.CFG
INSTALL_DIR = CFG["INSTALL_DIR"]
HOST_ID = CFG["HOST_ID"]
HUD_PORT = int(CFG["HUD_PORT"])
TODO_PORT = int(CFG["TODO_PORT"])
DEAD_AFTER = float(CFG["QUEUE_DEAD_AFTER"])
CLIENT_TTL = DEAD_AFTER  # stale-client expiry (~4-5 heartbeats)
ROSTER_PATH = os.path.join(INSTALL_DIR, "run", "roster.json")
STATUS_DIR = os.path.join(INSTALL_DIR, "status")
DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

START = time.time()
LOCK = threading.RLock()
CLIENTS = {}     # hostname -> record
AGENTS = {}      # agent_id -> record
TASKS = {}       # task_id -> record
# A client's attach_base is now operator-authoritative (set via TTYD_PUBLIC_URL; there is no
# auto-derivation). Only reject bind-any / empty addresses that can never be a usable client URL.
# Loopback (localhost/127.0.0.1) is honored — it is exactly right for the single-host,
# published-ports posture where the browser reaches ttyd on the same host.
NONROUTABLE = {"0.0.0.0", "127.0.0.1", "localhost", "::", ""}


def now():
    return time.time()


def load_roster():
    r = C.read_json(ROSTER_PATH, {}) or {}
    return r if isinstance(r, dict) else {}


def status_for(agent_id):
    _, sess, tab = C.parse_agent_id(agent_id)
    p = os.path.join(STATUS_DIR, "mc-%s" % sess, "%s.json" % tab)
    return C.read_json(p, None)


def display_status(raw):
    # canonical map: idle->idle, blocked->blocked, working|starting->working, missing->ready
    if not raw:
        return "ready"
    s = raw.get("status", "")
    if s == "idle":
        return "idle"
    if s == "blocked":
        return "blocked"
    if s in ("working", "starting"):
        return "working"
    return "ready"


def agent_row(aid, rec):
    host = rec.get("host", "")
    client = CLIENTS.get(host, {})
    ab = client.get("attach_base", "") or ""
    # never advertise a non-routable attach_base
    from urllib.parse import urlparse
    try:
        h = urlparse(ab).hostname if ab else ""
    except Exception:
        h = ""
    if h in NONROUTABLE:
        ab = ""
    tgt = rec.get("tmux_target") or C.tmux_target(aid)
    attach_url = ("%s/?arg=-t&arg=%s" % (ab, tgt)) if ab else ""
    roster = load_roster()
    rrec = roster.get(aid, {})
    spawn_cmd = rec.get("spawn_cmd") or rrec.get("spawn_cmd", "")
    model = rec.get("model") or rrec.get("model", "")
    st = status_for(aid)
    summary = (st or {}).get("summary") or rec.get("summary", "")
    state = rec.get("state", "alive")
    status = display_status(st)
    if state == "unauthenticated":
        # The lifecycle hook fires on prompt-submit, so an agent that received a ping it can never
        # answer is frozen at "working" forever. Reporting that alongside a dead credential is the
        # false green this card exists to kill (8a6ebe4e48): it is blocked, on login.
        status = "blocked"
        summary = rec.get("auth_detail") or "not authenticated — run `claude auth login` on this node"
    row = {
        "agent_id": aid,
        "host": host,
        "session": rec.get("session", C.parse_agent_id(aid)[1]),
        "tab": rec.get("tab", C.parse_agent_id(aid)[2]),
        "backend": rec.get("backend", "claude"),
        "state": state,
        "boss_id": rec.get("boss_id", ""),
        "is_master": rec.get("is_master", False),
        "summary": summary,
        "status": status,
        "ts": rec.get("ts", now()),
        "tmux_target": tgt,
        "attach_base": ab,
        "attach_url": attach_url,
        "spawn_cmd": spawn_cmd,
        "revive_cmd": "mp revive %s" % aid,
        "model": model,
    }
    return row


def reaper():
    while True:
        time.sleep(5)
        with LOCK:
            t = now()
            dead_hosts = [h for h, c in CLIENTS.items() if t - c.get("last_seen", 0) > CLIENT_TTL]
            for h in dead_hosts:
                del CLIENTS[h]
            # prune agents whose host is gone/stale
            for aid in list(AGENTS.keys()):
                rec = AGENTS[aid]
                h = rec.get("host", "")
                c = CLIENTS.get(h)
                if not c or (t - c.get("last_seen", 0) > DEAD_AFTER):
                    del AGENTS[aid]
            # expire delivered tasks older than 5m
            for tid in list(TASKS.keys()):
                if TASKS[tid].get("done_ts") and t - TASKS[tid]["done_ts"] > 300:
                    del TASKS[tid]


def upsert_agents(host, agents):
    for a in agents or []:
        aid = a.get("agent_id")
        if not aid:
            continue
        h, sess, tab = C.parse_agent_id(aid)
        rec = AGENTS.get(aid, {})
        rec.update({
            "agent_id": aid,
            "host": a.get("host") or host or h,
            "session": a.get("session") or sess,
            "tab": a.get("tab") or tab,
            "backend": a.get("backend", rec.get("backend", "claude")),
            "state": a.get("state", "alive"),
            "boss_id": a.get("boss_id", rec.get("boss_id", "")),
            "is_master": a.get("is_master", rec.get("is_master", False)),
            "auth_detail": a.get("auth_detail", ""),
            "summary": a.get("summary", rec.get("summary", "")),
            "spawn_cmd": a.get("spawn_cmd", rec.get("spawn_cmd", "")),
            "model": a.get("model", rec.get("model", "")),
            "tmux_target": a.get("tmux_target") or C.tmux_target(aid),
            "ts": now(),
        })
        AGENTS[aid] = rec


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---- helpers ----
    def _send(self, code, obj=None, ctype="application/json", raw=None, extra_headers=None):
        if raw is None:
            raw = json.dumps(obj).encode("utf-8") if obj is not None else b""
        elif isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _auth(self):
        ok, ident = C.caller_identity(self.headers)
        return ok

    def _page_headers(self):
        return {
            "Set-Cookie": "mp_session=%s; HttpOnly; Path=/; SameSite=Lax" % C.mint_session(),
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }

    def do_HEAD(self):
        p = self.path.split("?", 1)[0]
        if p == "/dashboard" or p.startswith("/dashboard/"):
            self.send_response(200)
            for k, v in self._page_headers().items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p in ("/", "/todos", "/terminal-graph") or p.startswith("/todo/"):
            return C.proxy_request(self, "127.0.0.1", TODO_PORT)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- routing ----
    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/health":
            return self._send(200, {"status": "ok", "uptime": int(now() - START)})
        if p == "/favicon.ico":
            return self._send(204, raw=b"")
        if p == "/dashboard" or p.startswith("/dashboard/"):
            try:
                with open(DASHBOARD_HTML, "rb") as f:
                    html = C.render_page(f.read().decode("utf-8"))
            except Exception:
                html = C.render_page("<h1>MyPlow - HUD</h1>")
            return self._send(200, raw=html, ctype="text/html; charset=utf-8",
                              extra_headers=self._page_headers())
        # TODO routes -> proxy to todo-server (symmetric front doors)
        if p in ("/", "/todos", "/terminal-graph") or p.startswith("/todo/"):
            return C.proxy_request(self, "127.0.0.1", TODO_PORT)
        # gated JSON
        if p in ("/agents", "/clients", "/roster"):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            with LOCK:
                if p == "/agents":
                    return self._send(200, [agent_row(a, r) for a, r in AGENTS.items()])
                if p == "/clients":
                    return self._send(200, [
                        {"hostname": h, "attach_base": c.get("attach_base", ""),
                         "substrate_ready": c.get("substrate_ready", False),
                         "last_seen": c.get("last_seen", 0), "purpose": c.get("purpose", "mypeople"),
                         "node_type": c.get("node_type", "system-agent"),
                         "recording_url": c.get("recording_url", ""), "state": c.get("state", "ready")}
                        for h, c in CLIENTS.items()])
                if p == "/roster":
                    roster = load_roster()
                    out = []
                    for aid, rr in roster.items():
                        out.append({
                            "agent_id": aid, "host": rr.get("host", ""),
                            "backend": rr.get("backend", "claude"),
                            "boss_id": rr.get("boss_id", ""),
                            "retired": rr.get("retired", False),
                            "retire_reason": rr.get("retire_reason", ""),
                            "retired_ts": rr.get("retired_ts"),
                            "session_id": rr.get("session_id", ""),
                            # the ownership contract: /todo/owner refuses any agent whose row does
                            # not say it was born an owner OF THAT CARD, so both must be projected
                            # here or every assignment is rejected as ineligible.
                            "lifecycle": rr.get("lifecycle", "legacy"),
                            "owner_task_id": rr.get("owner_task_id", ""),
                            "spawn_cmd": rr.get("spawn_cmd", ""),
                            "revive_cmd": "mp revive %s" % aid,
                            "cwd": rr.get("cwd", ""), "model": rr.get("model", ""),
                            "summary": rr.get("summary", ""),
                            # which personality this agent was actually born with. Empty for a
                            # legacy agent spawned before the role mount existed -- consumers
                            # render that as "-" so an unmounted agent is visibly unmounted.
                            "role": rr.get("role", ""),
                            "role_ref": rr.get("role_ref", ""),
                            "role_digest": rr.get("role_digest", ""),
                            # the announced state, not just "is it in the table" -- an
                            # unauthenticated agent is present but cannot answer (8a6ebe4e48)
                            "state": (AGENTS[aid].get("state", "alive")
                                      if aid in AGENTS else "dead"),
                        })
                    return self._send(200, out)
        if p.startswith("/task/poll"):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            hn = (q.get("hostname", [""])[0])
            with LOCK:
                out = []
                for tid, t in TASKS.items():
                    if t.get("delivered"):
                        continue
                    th = t.get("target_host") or ""
                    if th and th != hn:
                        continue
                    t["delivered"] = True
                    out.append({"task_id": tid, "type": t["type"],
                                "target_agent": t.get("target_agent", ""), "payload": t.get("payload", {})})
                return self._send(200, out)
        if p.startswith("/task/"):
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            tid = p.split("/task/", 1)[1]
            with LOCK:
                t = TASKS.get(tid)
                if not t:
                    return self._send(404, {"error": "no_task"})
                return self._send(200, {"task_id": tid, "type": t["type"], "ok": t.get("ok"),
                                        "result": t.get("result"), "delivered": t.get("delivered", False)})
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        # TODO routes -> proxy
        if p.startswith("/todo/"):
            return C.proxy_request(self, "127.0.0.1", TODO_PORT)
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        body = self._body()
        if p == "/heartbeat":
            hn = body.get("hostname", "")
            with LOCK:
                ab = body.get("attach_base", "") or ""
                from urllib.parse import urlparse
                try:
                    h = urlparse(ab).hostname if ab else ""
                except Exception:
                    h = ""
                if h in NONROUTABLE:
                    ab = ""
                c = CLIENTS.get(hn, {})
                c.update({"hostname": hn, "attach_base": ab,
                          "substrate_ready": body.get("substrate_ready", False),
                          "purpose": body.get("purpose", "mypeople"),
                          "node_type": body.get("node_type", "system-agent"),
                          "recording_url": body.get("recording_url", ""),
                          "state": body.get("state", "ready"), "last_seen": now()})
                CLIENTS[hn] = c
                upsert_agents(hn, body.get("agents", []))
            return self._send(200, {"ok": True})
        if p == "/agents/register":
            aid = body.get("agent_id")
            if not aid:
                return self._send(400, {"error": "no_agent_id"})
            with LOCK:
                upsert_agents(body.get("host", ""), [body])
            return self._send(200, {"ok": True})
        if p == "/agents/unregister":
            aid = body.get("agent_id")
            with LOCK:
                AGENTS.pop(aid, None)
            return self._send(200, {"ok": True})
        if p == "/task/submit":
            typ = body.get("type") or body.get("action")  # tolerate legacy alias on dispatch side
            if not typ:
                return self._send(400, {"error": "no_type"})
            tid = uuid.uuid4().hex[:16]
            ta = body.get("target_agent", "")
            th = C.parse_agent_id(ta)[0] if ta else ""
            with LOCK:
                TASKS[tid] = {"task_id": tid, "type": typ, "target_agent": ta,
                              "target_host": th, "payload": body.get("payload", {}),
                              "delivered": False, "ok": None, "result": None, "ts": now()}
            return self._send(200, {"task_id": tid})
        if p == "/task/result":
            tid = body.get("task_id")
            with LOCK:
                t = TASKS.get(tid)
                if t:
                    t["ok"] = body.get("ok")
                    t["result"] = body.get("result")
                    t["done_ts"] = now()
            return self._send(200, {"ok": True})
        if p == "/revive":
            aid = body.get("agent_id")
            roster = load_roster()
            if aid in roster:
                tid = uuid.uuid4().hex[:16]
                th = C.parse_agent_id(aid)[0]
                with LOCK:
                    TASKS[tid] = {"task_id": tid, "type": "revive", "target_agent": aid,
                                  "target_host": th, "payload": {}, "delivered": False,
                                  "ok": None, "result": None, "ts": now()}
                return self._send(200, {"ok": True, "agent_id": aid, "task_id": tid})
            return self._send(404, {"ok": False, "error": "unknown_agent"})
        return self._send(404, {"error": "not_found"})


def main():
    threading.Thread(target=reaper, daemon=True).start()
    srv = ThreadingHTTPServer((CFG["BIND_ADDR"], HUD_PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("queue-server on %s:%d\n" % (CFG["BIND_ADDR"], HUD_PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
