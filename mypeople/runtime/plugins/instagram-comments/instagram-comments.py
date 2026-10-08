#!/usr/bin/env python3
"""Instagram comments plugin: Meta pushes each new comment here, straight into the tab of the agent
this plugin owns, and that agent answers it.

Push, not polling. The Facebook app's `instagram` webhook (field `comments`) POSTs every new
comment on the account's posts to this server. Each one is checked against the app secret and
written to a spool on disk.

Plugin on means its agent is up, the way the Boss is always up: one persistent Claude session
(Grok 4.7 by default) with its own role (agent/CLAUDE.md, the persona, the reply-to-ig skill) in a
container of its own,
because it answers public comments on its own and anyone commenting can try to steer it. Before
every delivery the plugin makes sure that container runs and the session is ready, then pastes

    [IG REPLY] comment_id=<ID> user=<username> permalink=<POST URL>: <text>

into its tab. Never through the Boss. A comment that arrives while the agent is still starting
stays in the spool and is retried, so a cold start drops nothing.

Config (env, or the file MYPEOPLE_CONFIG_PATH names, default ~/.config/mypeople/queue.env):

    INSTAGRAM_COMMENTS=1
    INSTAGRAM_APP_SECRET=...          # verifies Meta's X-Hub-Signature-256
    INSTAGRAM_VERIFY_TOKEN=...        # answers Meta's subscribe handshake
    INSTAGRAM_TOKEN=...               # read-only use: looks up a post's permalink
    INSTAGRAM_USER_ID=1784...         # the account; its own comments are never forwarded
    INSTAGRAM_WEBHOOK_PORT=8796       # local port; expose it with `tailscale funnel`
    INSTAGRAM_AGENT_PERSONA=path      # who the agent talks like; kept out of the repo
    INSTAGRAM_AGENT_NAME=ig-agent     # optional: container name
    INSTAGRAM_AGENT_BACKEND=grok      # optional: grok (default) or claude; the rest of the fleet is unaffected
    INSTAGRAM_AGENT_MODEL=grok-4.7    # optional: default per backend
    INSTAGRAM_AGENT_CREDENTIALS=path  # optional: default ~/.grok/auth.json or ~/.claude/.credentials.json

    instagram-comments.py serve    receive + deliver forever (what supervise.sh / systemd runs)
    instagram-comments.py status   pending spool and whether the agent is up
"""
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME")
               or Path.home() / ".local/share/mypeople")
STATE_DIR = Path(os.environ.get("INSTAGRAM_COMMENTS_STATE_DIR") or INSTALL / "state" / "instagram-comments")
SPOOL = STATE_DIR / "spool.jsonl"
SEEN = STATE_DIR / "seen.json"
GRAPH = "https://graph.facebook.com/v21.0/"
RETRY_SECS = 30   # only while something is undelivered; an empty spool waits for the next push
SNIPPET = 400
AGENT_DIR = Path(__file__).resolve().parent / "agent"
IMAGE = "mypeople-instagram-agent"
# The agent's CLI: how it starts, which login it needs, and what its tab shows once it takes input.
BACKENDS = {
    "grok": {"cmd": "grok --permission-mode bypassPermissions -m {model}", "model": "grok-4.7",
             "creds": (".grok/auth.json", "/home/node/.grok/auth.json"), "ready": "always-approve"},
    "claude": {"cmd": "claude --dangerously-skip-permissions --model {model}", "model": "claude-opus-5-5",
               "creds": (".claude/.credentials.json", "/home/node/.claude/.credentials.json"),
               "ready": "bypass permissions on"},
}
LOCK = threading.Lock()
WAKE = threading.Event()


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S ") + "[instagram-comments] " + msg, flush=True)


def cfg(key, default=""):
    """Env first, then the config file -- the same precedence as the rest of the runtime."""
    if os.environ.get(key):
        return os.environ[key]
    path = os.environ.get("MYPEOPLE_CONFIG_PATH") or str(Path.home() / ".config/mypeople/queue.env")
    try:
        for line in open(path):
            line = line.strip()
            line = line[7:] if line.startswith("export ") else line
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return default


# ---------------------------------------------------------------- what Meta sends
def signature_ok(body, header):
    secret = cfg("INSTAGRAM_APP_SECRET").encode()
    want = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return bool(secret) and hmac.compare_digest(want, header or "")


def comments_in(payload, own_id):
    """The new comments in one webhook body, minus the account's own."""
    out = []
    if payload.get("object") != "instagram":
        return out
    for entry in payload.get("entry") or []:
        for ch in entry.get("changes") or []:
            v = ch.get("value") or {}
            if ch.get("field") != "comments" or not v.get("id"):
                continue
            who = v.get("from") or {}
            if own_id and str(who.get("id")) == str(own_id):
                continue
            out.append({"id": str(v["id"]), "text": v.get("text") or "", "user": who.get("username") or "?",
                        "media": str((v.get("media") or {}).get("id") or "")})
    return out


def format_comment(c, permalink):
    text = " ".join(c["text"].split())
    if len(text) > SNIPPET:
        text = text[:SNIPPET - 1] + "…"
    return "[IG REPLY] comment_id=%s user=%s permalink=%s: %s" % (
        c["id"], c["user"], permalink or "media:" + c["media"], text or "(no text)")


# ---------------------------------------------------------------- spool
def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def spool_add(comments):
    """Append comments not seen before. Meta retries a delivery it thinks failed; a comment is
    spooled once."""
    with LOCK:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        seen = set(read_json(SEEN, []))
        fresh = [c for c in comments if c["id"] not in seen]
        if fresh:
            with open(SPOOL, "a") as f:
                for c in fresh:
                    f.write(json.dumps(c) + "\n")
                f.flush()
                os.fsync(f.fileno())
            tmp = SEEN.with_suffix(".tmp")
            # ponytail: seen ids grow forever (~25 bytes each); trim to the last N if it ever matters
            tmp.write_text(json.dumps(sorted(seen | {c["id"] for c in fresh})))
            tmp.replace(SEEN)
    return fresh


def spool_pending():
    with LOCK:
        try:
            return [json.loads(l) for l in SPOOL.read_text().splitlines() if l.strip()]
        except OSError:
            return []


def spool_drop(ids):
    with LOCK:
        keep = [l for l in (SPOOL.read_text().splitlines() if SPOOL.exists() else [])
                if l.strip() and json.loads(l)["id"] not in ids]
        tmp = SPOOL.with_suffix(".tmp")
        tmp.write_text("".join(l + "\n" for l in keep))
        tmp.replace(SPOOL)


# ---------------------------------------------------------------- delivery
def http_json(method, url, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def permalink(media_id, cache={}):
    if media_id and media_id not in cache:
        try:
            q = urllib.parse.urlencode({"fields": "permalink", "access_token": cfg("INSTAGRAM_TOKEN")})
            cache[media_id] = http_json("GET", GRAPH + media_id + "?" + q).get("permalink", "")
        except Exception:
            return ""   # not cached: the next comment on this post tries again
    return cache.get(media_id, "")


def agent_name():
    return cfg("INSTAGRAM_AGENT_NAME", "ig-agent")


def docker(*args, stdin=None, check=True):
    r = subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, timeout=600)
    if check and r.returncode != 0:
        raise RuntimeError("docker %s: %s" % (args[0], (r.stderr or r.stdout).strip()[:200]))
    return r.stdout.strip()


def backend():
    b = BACKENDS[cfg("INSTAGRAM_AGENT_BACKEND", "grok")]
    return dict(b, cmd=b["cmd"].format(model=cfg("INSTAGRAM_AGENT_MODEL") or b["model"]))


def create_agent(name):
    """Build the image and create the container with its token, persona and the CLI's login."""
    b = backend()
    with tempfile.TemporaryDirectory() as ctx:
        shutil.copytree(AGENT_DIR, ctx, dirs_exist_ok=True)
        shutil.copy(cfg("INSTAGRAM_AGENT_PERSONA"), Path(ctx) / "persona.md")
        docker("build", "-q", "-t", IMAGE, ctx)
    docker("create", "--name", name, "--restart", "unless-stopped",
           "-e", "META_ACCESS_TOKEN=" + cfg("INSTAGRAM_TOKEN"), "-e", "IG_AGENT_CMD=" + b["cmd"], IMAGE)
    host_file, box_file = b["creds"]
    creds = json.loads(Path(cfg("INSTAGRAM_AGENT_CREDENTIALS") or Path.home() / host_file).read_text())
    if "claudeAiOauth" in creds:   # only the Claude login, not the other OAuth grants that file carries
        creds = {"claudeAiOauth": creds["claudeAiOauth"]}
    with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
        json.dump(creds, f)
        f.flush()
        os.chmod(f.name, 0o600)
        docker("cp", f.name, name + ":" + box_file)


def ensure_agent():
    """Plugin on means its agent is up: start (or first create) the container. True once the
    Claude session in it takes input."""
    name = agent_name()
    state = docker("inspect", "-f", "{{.State.Running}}", name, check=False)
    if state not in ("true", "false"):
        log("creating agent container %s" % name)
        create_agent(name)
        state = "false"
    if state == "false":
        docker("start", name)
    pane = docker("exec", name, "tmux", "capture-pane", "-p", "-t", "ig:agent", check=False)
    return backend()["ready"] in pane


def deliver(text):
    """Paste one line into the agent's tab and submit it, as `mp send` does for a local pane."""
    if not ensure_agent():
        return False
    name = agent_name()
    docker("exec", "-i", name, "tmux", "load-buffer", "-b", "ig", "-", stdin=text)
    docker("exec", name, "tmux", "paste-buffer", "-d", "-b", "ig", "-t", "ig:agent")
    time.sleep(0.5)   # let the paste land before Enter, or Claude takes it as a newline
    docker("exec", name, "tmux", "send-keys", "-t", "ig:agent", "Enter")
    return True


def forward_loop():
    agent = agent_name()
    while True:
        pending = spool_pending()
        done = set()
        for c in pending:
            try:
                if deliver(format_comment(c, permalink(c["media"]))):
                    done.add(c["id"])
                else:
                    break
            except Exception as e:   # agent not up yet: keep it spooled, retry later
                log("delivery to %s waiting: %s" % (agent, str(e)[:120]))
                break
        if done:
            spool_drop(done)
            log("delivered %d comment(s) to %s" % (len(done), agent))
        WAKE.wait(RETRY_SECS if len(pending) > len(done) else None)
        WAKE.clear()


# ---------------------------------------------------------------- HTTP
class Hook(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        ok = (q.get("hub.mode") == ["subscribe"] and cfg("INSTAGRAM_VERIFY_TOKEN")
              and hmac.compare_digest(q.get("hub.verify_token", [""])[0], cfg("INSTAGRAM_VERIFY_TOKEN")))
        self.reply(200, q["hub.challenge"][0].encode()) if ok and q.get("hub.challenge") else self.reply(403)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            return self.reply(413)
        body = self.rfile.read(n)
        if not signature_ok(body, self.headers.get("X-Hub-Signature-256")):
            log("rejected a POST with a bad signature")
            return self.reply(403)
        try:
            payload = json.loads(body)
            fresh = spool_add(comments_in(payload, cfg("INSTAGRAM_USER_ID")))
        except ValueError:
            return self.reply(400)
        # One line per push proves Meta is reaching us, even for events we do not forward.
        fields = sorted({ch.get("field") or "messaging" for e in payload.get("entry") or []
                         for ch in (e.get("changes") or e.get("messaging") or [{}])})
        log("push %s %s: %d new comment(s)" % (payload.get("object"), ",".join(fields), len(fresh)))
        if fresh:
            WAKE.set()
        self.reply(200, b"ok")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    missing = [k for k in ("INSTAGRAM_APP_SECRET", "INSTAGRAM_VERIFY_TOKEN", "INSTAGRAM_USER_ID",
                           "INSTAGRAM_TOKEN", "INSTAGRAM_AGENT_PERSONA") if not cfg(k)]
    if missing:
        log("missing config: " + ", ".join(missing))
        return 2
    if cmd == "status":
        print("%d comment(s) waiting in %s; agent %s (%s) ready: %s"
              % (len(spool_pending()), SPOOL, agent_name(), backend()["cmd"], ensure_agent()))
        return 0
    port = int(cfg("INSTAGRAM_WEBHOOK_PORT", "8796"))
    try:
        ensure_agent()   # plugin on means its agent is up, before the first comment arrives
    except Exception as e:
        log("agent not up yet: %s" % e)
    threading.Thread(target=forward_loop, daemon=True).start()
    log("listening on 127.0.0.1:%d, delivering to agent %s" % (port, agent_name()))
    ThreadingHTTPServer(("127.0.0.1", port), Hook).serve_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
