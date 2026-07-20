#!/usr/bin/env python3
"""mypeople shared helpers: config, auth/session, json io, tmux delivery, http proxy.
Python 3 stdlib only."""
import os, sys, json, hmac, hashlib, base64, time, socket, subprocess, threading, urllib.request, urllib.parse
import shlex, signal
import fcntl

def config_path():
    explicit = os.environ.get("MYPEOPLE_CONFIG_PATH")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    home = os.environ.get("MYPEOPLE_HOME")
    if home:
        return os.path.join(os.path.abspath(os.path.expanduser(home)), "config", "queue.env")
    return os.path.expanduser("~/.config/mypeople/queue.env")


CONFIG_PATH = config_path()

def load_env():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
                    v = v[1:-1]
                cfg[k.strip()] = v
    # Live env overrides the file, including fleet/client-only keys not present in old configs.
    known = set(cfg) | {
        "INSTALL_DIR", "HOST_ID", "HUD_PORT", "TODO_PORT", "TTYD_PORT",
        "TTYD_BROWSER_PORT", "TTYD_RO_PORT", "BIND_ADDR",
        "QUEUE_URL", "QUEUE_SECRET", "TTYD_PUBLIC_URL", "DEFAULT_ENG_MODEL",
        "DEFAULT_BACKEND", "DEFAULT_CLAUDE_MODEL", "DEFAULT_CODEX_MODEL", "DEFAULT_GROK_MODEL",
        "MYPEOPLE_RECORD",
        "QUEUE_DEAD_AFTER", "HEARTBEAT_INTERVAL", "UPSTREAM_QUEUE_URL",
        "UPSTREAM_QUEUE_SECRET", "NODE_PURPOSE", "NODE_TYPE", "NODE_RECORDING_URL",
    }
    for k in known:
        if os.environ.get(k) is not None:
            cfg[k] = os.environ[k]
    # a few defaults
    cfg.setdefault("INSTALL_DIR", os.path.expanduser("~/mypeople"))
    cfg.setdefault("HOST_ID", socket.gethostname().split(".")[0])
    cfg.setdefault("HUD_PORT", "9900")
    cfg.setdefault("TODO_PORT", "9933")
    cfg.setdefault("TTYD_PORT", "7681")
    cfg.setdefault("TTYD_BROWSER_PORT", cfg["TTYD_PORT"])
    # Read-only ttyd: the Terminal Graph's tiles are views, not consoles. Stock ttyd is readonly
    # unless -W, so this is a second plain ttyd rather than a special build.
    cfg.setdefault("TTYD_RO_PORT", str(int(cfg["TTYD_PORT"]) + 1))
    cfg.setdefault("BIND_ADDR", "0.0.0.0")
    cfg.setdefault("QUEUE_URL", "http://127.0.0.1:%s" % cfg["HUD_PORT"])
    cfg.setdefault("DEFAULT_ENG_MODEL", "claude-opus-4-8")
    cfg.setdefault("DEFAULT_BACKEND", "claude")
    cfg.setdefault("DEFAULT_CLAUDE_MODEL", cfg["DEFAULT_ENG_MODEL"])
    cfg.setdefault("DEFAULT_CODEX_MODEL", "")
    cfg.setdefault("QUEUE_DEAD_AFTER", "45")
    cfg.setdefault("HEARTBEAT_INTERVAL", "10")
    return cfg

CFG = load_env()

# ---------- running version (single source: package __version__) ----------
# The runtime is copied out of the package into INSTALL_DIR and runs under a bare interpreter,
# so `import mypeople` is not reachable from here in a real install. firstrun.materialize()
# stamps INSTALL_DIR/VERSION from __version__; that file is this runtime's channel to it.
VERSION_UNKNOWN = "dev"

def version():
    """Version of the install actually serving this process. Never raises: a page must not
    500 over a version string."""
    env = os.environ.get("MYPEOPLE_VERSION")
    if env and env.strip():
        return env.strip()
    try:
        with open(os.path.join(CFG["INSTALL_DIR"], "VERSION")) as f:
            v = f.read().strip()
        if v:
            return v
    except Exception:
        pass
    # running straight from the source tree / an editable install
    try:
        import mypeople
        return mypeople.__version__
    except Exception:
        pass
    return VERSION_UNKNOWN


# ---------- page rendering (shared by both front doors) ----------
_VERSION_BADGE = """
<style>
#mp-version-badge{position:fixed;right:8px;bottom:8px;z-index:2147483000;
 font:500 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.02em;
 color:#8b949e;background:rgba(22,27,34,.82);border:1px solid rgba(139,148,158,.22);
 border-radius:999px;padding:4px 8px;pointer-events:none;user-select:none;
 backdrop-filter:blur(4px);opacity:.75}
@media print{#mp-version-badge{display:none}}
</style>
<div id="mp-version-badge" title="MyPeople version serving this page">__MP_VERSION__</div>
"""

# Shown on every page while this node has no AI login. It is deliberately loud and at the top of
# the document: the failure it describes ("nothing happens when I ask for anything") is otherwise
# indistinguishable from the product being broken.
_LOGIN_BANNER_TMPL = """
<style>
#mp-login-banner{position:sticky;top:0;z-index:2147483001;
 font:500 13px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif;
 color:#f0d8a8;background:#4a3410;border-bottom:1px solid #8a6520;padding:10px 16px}
#mp-login-banner b{color:#ffd479}
#mp-login-banner code{display:inline-block;margin:2px 6px 0 0;padding:2px 7px;
 font:500 12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
 color:#ffe9bd;background:rgba(0,0,0,.35);border:1px solid rgba(255,212,121,.25);border-radius:5px;
 user-select:all}
</style>
<div id="mp-login-banner">
  <b>Login required.</b> This node has no AI login yet, so no agent can run.
  Run it inside this node: __MP_LOGIN_STEPS__
  <span style="opacity:.8">The Boss starts by itself ~15s after login — no restart needed.</span>
</div>
"""

def auth_state():
    """This node's login state, as published by the package (firstrun.write_auth_state).

    Absent file => say nothing. An install that predates the file, or a runtime driven without
    the CLI, must not grow a scary banner just because it cannot find a status file.
    """
    try:
        with open(os.path.join(CFG["INSTALL_DIR"], "status", "auth.json")) as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _login_banner():
    """The 'you still have to log in' notice, or '' when this node is fine."""
    state = auth_state()
    if not state or state.get("authenticated") is not False:
        return ""
    steps = "".join(
        "<code>%s</code>" % html_escape(line) for line in (state.get("howto") or []))
    return _LOGIN_BANNER_TMPL.replace("__MP_LOGIN_STEPS__", steps)


def html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_page(html):
    """Substitute the page placeholders every UI page shares, and make the running version
    visible on it. Injecting here (rather than in each .html) is what makes 'every page'
    true by construction — including pages added later."""
    html = html.replace("__TTYD_PORT__", str(CFG["TTYD_BROWSER_PORT"]))
    html = html.replace("__HOST_ID__", CFG["HOST_ID"])
    banner = _login_banner()
    if banner and "id=\"mp-login-banner\"" not in html:
        if "<body" in html:
            idx = html.index("<body")
            end = html.index(">", idx) + 1
            html = html[:end] + banner + html[end:]
        else:
            html = banner + html
    if "id=\"mp-version-badge\"" not in html:
        badge = _VERSION_BADGE
        if "</body>" in html:
            html = html.replace("</body>", badge + "</body>", 1)
        else:
            html += badge
    return html.replace("__MP_VERSION__", "v" + version())


# ---------- session cookie (stateless, HMAC-signed; NOT the secret) ----------
def _hmac(msg):
    return hmac.new(CFG.get("QUEUE_SECRET", "").encode(), msg.encode(), hashlib.sha256).hexdigest()[:24]

def mint_session():
    rnd = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    return "%s.%s" % (rnd, _hmac(rnd))

def valid_session(tok):
    if not tok or "." not in tok:
        return False
    rnd, sig = tok.rsplit(".", 1)
    return hmac.compare_digest(sig, _hmac(rnd))

def parse_cookies(header):
    out = {}
    if not header:
        return out
    for part in header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k.strip()] = v.strip()
    return out

def caller_identity(headers):
    """Returns (authorized:bool, identity:str) where identity in {machine,browser,none}."""
    secret = CFG.get("QUEUE_SECRET", "")
    hs = headers.get("X-Queue-Secret")
    if secret and hs and hmac.compare_digest(hs, secret):
        return True, "machine"
    cookies = parse_cookies(headers.get("Cookie", ""))
    if valid_session(cookies.get("mp_session", "")):
        return True, "browser"
    return False, "none"

# ---------- atomic json io ----------
def atomic_write(path, data_bytes):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), int(time.time() * 1000))
    with open(tmp, "wb") as f:
        f.write(data_bytes)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

# ---------- agent_id helpers ----------
def parse_agent_id(aid):
    """<host>/<sess>:<tab> -> (host, sess, tab). Robust to missing pieces."""
    host = sess = tab = ""
    if "/" in aid:
        host, rest = aid.split("/", 1)
    else:
        rest = aid
    if ":" in rest:
        sess, tab = rest.split(":", 1)
    else:
        sess = rest
    return host, sess, tab


def _notification_route_paths(target_agent):
    digest = hashlib.sha256(target_agent.encode("utf-8")).hexdigest()
    root = os.path.join(CFG["INSTALL_DIR"], "run", "notification-routes")
    return os.path.join(root, digest + ".json"), os.path.join(root, digest + ".lock")


def _locked_notification_routes(target_agent, mutate):
    """Atomically update the per-agent FIFO used to route the next Stop notification."""
    path, lock_path = _notification_route_paths(target_agent)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        routes = read_json(path, []) or []
        if not isinstance(routes, list):
            routes = []
        result, routes = mutate(routes)
        if routes:
            write_json(path, routes)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return result


def enqueue_notification_route(target_agent, reply_to):
    """Queue reply_to as the recipient of target_agent's next Stop. Returns a route token."""
    if not target_agent or not reply_to or target_agent == reply_to:
        return ""
    token = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")

    def append(routes):
        routes.append({"token": token, "reply_to": reply_to, "ts": time.time()})
        return token, routes

    return _locked_notification_routes(target_agent, append)


def cancel_notification_route(target_agent, token):
    """Remove a route when its corresponding message could not be delivered."""
    if not target_agent or not token:
        return False

    def cancel(routes):
        kept = [route for route in routes if route.get("token") != token]
        return len(kept) != len(routes), kept

    return _locked_notification_routes(target_agent, cancel)


def claim_notification_route(target_agent):
    """Consume and return the oldest per-message reply target for this agent."""
    if not target_agent:
        return ""

    def claim(routes):
        while routes:
            route = routes.pop(0)
            reply_to = route.get("reply_to", "") if isinstance(route, dict) else ""
            if reply_to:
                return reply_to, routes
        return "", routes

    return _locked_notification_routes(target_agent, claim)


def tmux_target(aid):
    _, sess, tab = parse_agent_id(aid)
    return "mc-%s:%s" % (sess, tab)


# ---------- terminal recorder lifecycle ----------
def recorder_identity(sess, tab, host=None):
    """Return the immutable identity used to validate a recorder before signalling it."""
    host = host or CFG["HOST_ID"]
    return {
        "session": "rec-%s" % tab,
        "target": "mc-%s:%s" % (sess, tab),
        "cast": os.path.expanduser("~/recordings/%s-%s.cast" % (host, tab)),
    }


def _ps(pid, field):
    try:
        r = subprocess.run(["ps", "-p", str(int(pid)), "-o", field + "="],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def _process_alive(pid):
    return bool(_ps(pid, "pid"))


def _recorder_pid_matches(pid, target, cast):
    """Reject PID reuse and unrelated asciinema jobs using the complete command identity."""
    if os.path.basename(_ps(pid, "comm")) != "asciinema":
        return False
    try:
        tokens = shlex.split(_ps(pid, "command"))
    except ValueError:
        return False
    if tokens:
        tokens[0] = os.path.basename(tokens[0])
    return tokens == ["asciinema", "rec", "--quiet", "--append", "-c", "TMUX=",
                      "tmux", "attach", "-rt", target, cast]


def _recorder_child_pids(pid):
    try:
        r = subprocess.run(["pgrep", "-P", str(int(pid))], capture_output=True, text=True,
                           timeout=2)
        return [int(x) for x in r.stdout.split() if x.isdigit()]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _recorder_child_matches(pid, parent_pid, target):
    if _ps(pid, "ppid").strip() != str(int(parent_pid)):
        return False
    try:
        tokens = shlex.split(_ps(pid, "command"))
    except ValueError:
        return False
    if tokens:
        tokens[0] = os.path.basename(tokens[0])
    return tokens == ["tmux", "attach", "-rt", target]


def current_recorder(sess, tab, host=None):
    """Describe the validated recorder reachable through the current tmux server."""
    ident = recorder_identity(sess, tab, host)
    try:
        r = subprocess.run(
            ["tmux", "list-panes", "-t", "=" + ident["session"],
             "-F", "#{pane_pid}|#{pid}|#{pane_dead}"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return None
        row = next((line for line in r.stdout.splitlines() if line), "")
        pane_pid, server_pid, dead = row.split("|", 2)
        pid = int(pane_pid)
        if dead != "0" or not _recorder_pid_matches(pid, ident["target"], ident["cast"]):
            return None
        return dict(ident, pid=pid, server_pid=int(server_pid), started_ts=time.time())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _wait_for_exit(pids, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(_process_alive(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return not any(_process_alive(pid) for pid in pids)


def stop_recorder(sess, tab, metadata=None, host=None, include_current=True):
    """Gracefully stop only command-validated recorders for one agent.

    The durable PID is a fallback for a recorder stranded on an old tmux server/socket.  Every
    signal is guarded by target+cast command validation, so PID reuse or an unrelated live recorder
    cannot be touched.
    """
    ident = recorder_identity(sess, tab, host)
    candidates = []
    if include_current:
        current = current_recorder(sess, tab, host)
        if current:
            candidates.append(current["pid"])
    if isinstance(metadata, dict):
        try:
            pid = int(metadata.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        if (pid and metadata.get("target") == ident["target"] and
                metadata.get("cast") == ident["cast"]):
            candidates.append(pid)
    candidates = list(dict.fromkeys(candidates))
    validated = [pid for pid in candidates
                 if _recorder_pid_matches(pid, ident["target"], ident["cast"])]
    child_terms = []
    for pid in validated:
        for child in _recorder_child_pids(pid):
            if _recorder_child_matches(child, pid, ident["target"]):
                try:
                    os.kill(child, signal.SIGTERM)
                    child_terms.append(child)
                except ProcessLookupError:
                    pass
    _wait_for_exit(validated)
    parent_terms = []
    for pid in validated:
        if (_process_alive(pid) and
                _recorder_pid_matches(pid, ident["target"], ident["cast"])):
            try:
                os.kill(pid, signal.SIGTERM)
                parent_terms.append(pid)
            except ProcessLookupError:
                pass
    _wait_for_exit(parent_terms)
    survivors = [pid for pid in validated
                 if _process_alive(pid) and
                 _recorder_pid_matches(pid, ident["target"], ident["cast"])]
    return {"requested_ts": time.time(), "validated": validated,
            "child_terms": child_terms, "parent_terms": parent_terms,
            "survivors": survivors}

def export_repo_path(cfg=None):
    """PER-INSTANCE git backup repo (§3 boardgit). Path carries an instance discriminator
    (TODO_PORT + sha1($INSTALL_DIR)[:8]) so two installs differing in INSTALL_DIR OR port never
    collide onto one repo. Overridable via EXPORT_REPO."""
    cfg = cfg or CFG
    override = os.environ.get("EXPORT_REPO") or cfg.get("EXPORT_REPO")
    if override:
        return override
    host = cfg.get("HOST_ID", "node")
    port = cfg.get("TODO_PORT", "9933")
    disc = hashlib.sha1(cfg.get("INSTALL_DIR", "").encode()).hexdigest()[:8]
    return os.path.expanduser("~/.mypeople/board-backup/%s-%s-%s" % (host, port, disc))

# ---------- tmux message delivery (bracketed paste + double Enter, with retry) ----------
def tmux_send_message(target, message):
    """Deliver a message into a tmux pane's composer and submit it.
    target = mc-<sess>:<tab>. Returns True on success."""
    if message is None or not str(message).strip():
        return False
    message = str(message)
    env = dict(os.environ)
    env.pop("TMUX", None)  # never target the caller's pane
    ok = False
    for attempt in range(3):
        r = subprocess.run(["tmux", "has-session", "-t", target.split(":")[0]],
                           env=env, capture_output=True)
        if r.returncode != 0:
            time.sleep(0.3)
            continue
        # Literal paste + one submit. Multi-line composers get a conditional retry only when
        # the backend still shows the bracketed-paste marker after the first Enter.
        subprocess.run(["tmux", "send-keys", "-t", target, "-l", message], env=env, capture_output=True)
        time.sleep(0.15)
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], env=env, capture_output=True)
        if "\n" in message:
            time.sleep(0.4)
            pane = subprocess.run(["tmux", "capture-pane", "-p", "-t", target, "-S", "-30"],
                                  env=env, capture_output=True, text=True)
            if "[Pasted text" in (pane.stdout or ""):
                subprocess.run(["tmux", "send-keys", "-t", target, "Enter"],
                               env=env, capture_output=True)
        ok = True
        break
    return ok

def tmux_capture(target, lines=200):
    env = dict(os.environ)
    env.pop("TMUX", None)
    r = subprocess.run(["tmux", "capture-pane", "-p", "-t", target, "-S", "-%d" % lines],
                       env=env, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""

# ---------- HTTP client (to central/other daemons) ----------
def http_json(method, url, body=None, headers=None, timeout=8):
    data = None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception:
        return 0, None

# ---------- reverse proxy (stream one request to an inner port) ----------
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host"}

def proxy_request(handler, inner_host, inner_port):
    """Forward handler's current request to inner_host:inner_port, stream response back.
    Preserves Cookie / X-Queue-Secret / Set-Cookie so auth + sessions survive the proxy."""
    path = handler.path
    url = "http://%s:%s%s" % (inner_host, inner_port, path)
    length = int(handler.headers.get("Content-Length", 0) or 0)
    body = handler.rfile.read(length) if length else None
    fwd = {}
    for k, v in handler.headers.items():
        if k.lower() in HOP:
            continue
        fwd[k] = v
    req = urllib.request.Request(url, data=body, headers=fwd, method=handler.command)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        status = resp.status
        raw = resp.read()
        rhdrs = resp.getheaders()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()
        rhdrs = e.headers.items()
    except Exception as e:
        handler.send_response(502)
        handler.send_header("Content-Type", "text/plain")
        handler.end_headers()
        handler.wfile.write(("proxy error: %s" % e).encode())
        return
    if raw is None:
        raw = b""
    handler.send_response(status)
    for k, v in rhdrs:
        if k.lower() in HOP:
            continue
        handler.send_header(k, v)
    # We stripped the inner Content-Length/Connection (HOP); re-add a correct framing so
    # HTTP/1.1 keep-alive clients (curl, browser fetch) know the body is complete and don't hang.
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()
    if raw:
        handler.wfile.write(raw)
