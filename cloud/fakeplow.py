#!/usr/bin/env python3
"""A fake Plow API + login server for proving cloud/update.py without a real line or phone.
cloud/publish.sh runs it as the gate a release passes before the `myplow` pin moves to it.

What the cloud MyPlow calls, and nothing more:
  GET  /v1/agents/cloud/me                -> the one rig chat
  GET  /v1/chats/<uid>/messages           -> its messages (inbound the rig injected + agent replies)
  POST /v1/chats/<uid>/messages           -> an agent reply (recorded as outbound)
  GET  /v1/auth/index-identity            -> a fake assertion
  GET  /v1/agent-images/<slug>            -> the pin the rig sets
  POST /claude-bank/token                 -> the owner's real Claude login, read from a mounted file
  POST /claude-bank/beacon                -> boot/update step lines (kept for the proof)
Rig control (the test harness only):
  POST /rig/pin {"image"}   POST /rig/inbound {"body"}   GET /rig/messages   GET /rig/beacons
The login is read per request and never logged or echoed anywhere else.
"""
import itertools
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHAT = "cht_rig"
TOKEN_FILE = os.environ.get("RIG_TOKEN_FILE", "/secret/claude-setup-token")
LOCK = threading.Lock()
MESSAGES, BEACONS = [], []
PIN = {"image": os.environ.get("RIG_PIN", "")}
BANK = {"status": int(os.environ.get("RIG_BANK_STATUS", "200"))}
SEQ = itertools.count(1)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def add(direction, body):
    with LOCK:
        m = {"uid": f"msg_{next(SEQ):04d}", "direction": direction, "body": body, "created_at": now(),
             "chat_uid": CHAT, "attachments": [],
             "sender": {"display_name": "Daniel (rig)", "provider_key": "+10000000000"}
             if direction == "inbound" else {"type": "agent"}}
        MESSAGES.append(m)
        return m


class H(BaseHTTPRequestHandler):
    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {"_raw": raw.decode(errors="replace")}

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/v1/agents/cloud/me":
            return self.send(200, {"agent": {"uid": "agt_rig"}, "chats": [{"uid": CHAT}]})
        if p == f"/v1/chats/{CHAT}/messages" or p == "/rig/messages":
            with LOCK:
                return self.send(200, {"data": list(MESSAGES)})
        if p == "/v1/auth/index-identity":
            return self.send(200, {"assertion": "rig-assertion"})
        if p.startswith("/v1/agent-images/"):
            return self.send(200, {"slug": p.rsplit("/", 1)[1], "image": PIN["image"] or None})
        if p == "/rig/beacons":
            with LOCK:
                return self.send(200, {"data": list(BEACONS)})
        self.send(404, {"error": f"rig has no {p}"})

    def do_POST(self):
        p, b = self.path.split("?")[0], self.body()
        if p == f"/v1/chats/{CHAT}/messages":
            return self.send(201, add("outbound", b.get("body", "")))
        if p == "/claude-bank/token":
            if BANK["status"] != 200:  # play a stranger: Daniel's login server refuses this agent
                return self.send(BANK["status"], {"error": "not the bank owner's agent"})
            with open(TOKEN_FILE) as f:
                return self.send(200, {"token": f.read().strip()})
        if p == "/claude-bank/skills":
            return self.send(BANK["status"] if BANK["status"] != 200 else 404, {})
        if p == "/rig/bank":
            BANK["status"] = int(b["status"])
            return self.send(200, BANK)
        if p == "/claude-bank/beacon":
            with LOCK:
                BEACONS.append({"at": now(), "line": b.get("_raw", json.dumps(b))[:300]})
            return self.send(200, {})
        if p == "/rig/pin":
            PIN["image"] = b["image"]
            return self.send(200, PIN)
        if p == "/rig/inbound":
            return self.send(201, add("inbound", b["body"]))
        self.send(404, {"error": f"rig has no POST {p}"})

    def log_message(self, fmt, *args):  # request lines only: no bodies, so no login
        print(time.strftime("%H:%M:%S"), self.command, self.path.split("?")[0], flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
