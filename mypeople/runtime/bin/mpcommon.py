#!/usr/bin/env python3
"""MyPlow shared helpers: config, auth/session, json io, tmux delivery, http proxy.
Python 3 stdlib only."""
import os, sys, json, hmac, hashlib, base64, time, socket, subprocess, threading, urllib.request, urllib.parse
import re
import mimetypes
import shlex, signal
import copy
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

def read_config_file():
    """queue.env as it is on disk now, without the env override load_env() applies.

    A long-lived process (the Boss) snapshotted its env at launch, so for a knob meant to move
    without a restart -- the default models -- the file is the current answer."""
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
    return cfg


def load_env():
    cfg = read_config_file()
    # Live env overrides the file, including fleet/client-only keys not present in old configs.
    known = set(cfg) | {
        "INSTALL_DIR", "HOST_ID", "HUD_PORT", "TODO_PORT", "TTYD_PORT",
        "TTYD_BROWSER_PORT", "TTYD_RO_PORT", "BIND_ADDR",
        "QUEUE_URL", "QUEUE_SECRET", "TTYD_PUBLIC_URL", "DEFAULT_ENG_MODEL",
        "DEFAULT_BACKEND", "DEFAULT_AGENT_BACKEND", "DEFAULT_CLAUDE_MODEL", "DEFAULT_CODEX_MODEL", "DEFAULT_GROK_MODEL",
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


def live_drift():
    """Installed runtime files that no longer match what materialize stamped under VERSION
    (run/manifest.json): a hand copy or patch that skipped `make live`. [] when none, or no manifest."""
    install = CFG["INSTALL_DIR"]
    try:
        with open(os.path.join(install, "run", "manifest.json")) as f:
            files = json.load(f)["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    changed = []
    for rel, want in files.items():
        try:
            with open(os.path.join(install, rel), "rb") as f:
                got = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            got = None
        if got != want:
            changed.append(rel)
    return sorted(changed)

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
#mp-version-badge{position:fixed;right:10px;bottom:9px;z-index:2147483000;
 font:400 10.5px/1 'DM Mono',ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.04em;
 color:rgba(240,240,232,.3);pointer-events:none;user-select:none}
@media print{#mp-version-badge{display:none}}
</style>
<div id="mp-version-badge" title="MyPlow version serving this page">__MP_VERSION__</div>
"""

# Every first-class surface, reachable from every other one. Injected at this seam for the same
# reason as the badge: a page added later is linked by construction, and no page can drift into
# being a dead end you can only reach by typing a URL. There are exactly two surfaces -- Board
# (the priorities) and Graph -- and the bar sits at the TOP of every page. Individual terminals
# are not a surface: the Graph is how you reach a terminal. The HUD page (/dashboard) still
# serves by URL but is off the bar: the CEO never used it.
_NAV = """
<style>
/* Brand tokens, read off the page itself. todos/dashboard and the graph name them differently
   (--muted-dark vs --muted, --text-dark vs --ink), so each one falls back to the other and then
   to a literal -- the nav must look native on a page that defines neither. */
/* A segmented control, the height of the board's add bar beside it (44px), in product-UI radii
   (10px, not marketing pills). */
#mp-nav{position:fixed;right:22px;top:18px;z-index:2147483000;display:flex;gap:2px;padding:3px;
 --mp-volt:var(--volt,#D5EF8A);
 --mp-ink:var(--text-dark,var(--ink,#F0F0E8));
 --mp-muted:var(--muted-dark,var(--muted,rgba(240,240,232,.45)));
 --mp-line:var(--dark-border,rgba(255,255,255,.09));
 border-radius:12px;background:rgba(26,26,24,.82);border:1px solid var(--mp-line);
 -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);box-shadow:0 8px 30px rgba(0,0,0,.28);
 font-family:'DM Sans',system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}
#mp-nav a{display:flex;align-items:center;gap:7px;height:36px;padding:0 13px 0 11px;border-radius:9px;
 text-decoration:none;font-size:13px;font-weight:500;line-height:1;color:var(--mp-muted);
 transition:color .15s,background .15s}
#mp-nav a svg{flex:0 0 auto;opacity:.75;transition:opacity .15s,color .15s}
#mp-nav a:hover{color:var(--mp-ink);background:rgba(255,255,255,.05)}
#mp-nav a:focus-visible{outline:2px solid var(--mp-volt);outline-offset:2px}
#mp-nav a[aria-current="page"]{color:var(--mp-ink);background:rgba(255,255,255,.09);
 box-shadow:inset 0 0 0 1px rgba(255,255,255,.05)}
#mp-nav a[aria-current="page"] svg{color:var(--mp-volt);opacity:1}
@media(max-width:700px){#mp-nav{right:12px;top:12px}#mp-nav a{height:32px;padding:0 11px 0 9px}}
@media print{#mp-nav{display:none}}
</style>
<nav id="mp-nav" aria-label="MyPlow surfaces">
 <a href="/" data-mp-path="/"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/></svg>Board</a>
 <a href="/terminal-graph" data-mp-path="/terminal-graph"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"/><path d="M12 12V8"/></svg>Graph</a>
</nav>
<script>
(function(){
  var here=location.pathname.replace(/\\/+$/,"")||"/";
  if(here==="/todos"){here="/";}
  document.querySelectorAll("#mp-nav a[data-mp-path]").forEach(function(a){
    if(a.getAttribute("data-mp-path")===here){a.setAttribute("aria-current","page");}
  });
  // The login banner is sticky at the very top; measured rather than hardcoded because its
  // height depends on how many login steps this node prints. Found by attribute, not by id, so
  // this script never carries the banner's id into a page that has no banner.
  var nav=document.getElementById("mp-nav");
  function place(){
    var b=document.querySelector("[data-mp-banner]");
    nav.style.top=(b?b.getBoundingClientRect().height+14:18)+"px";
  }
  place();
  window.addEventListener("resize",place);
})();
</script>
"""

# Shown on every page while this node has no AI login. It is deliberately loud and at the top of
# the document: the failure it describes ("nothing happens when I ask for anything") is otherwise
# indistinguishable from the product being broken.
_LOGIN_BANNER_TMPL = """
<style>
#mp-login-banner{position:sticky;top:0;z-index:2147483001;
 font:400 13.5px/1.55 'DM Sans',system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased;
 color:#F0F0E8;background:#231f14;border-bottom:1px solid rgba(254,188,46,.3);padding:12px 20px}
#mp-login-banner b{font-weight:600;color:#ffd98a}
#mp-login-banner code{display:inline-block;margin:3px 6px 0 0;padding:3px 8px;
 font:400 12px/1.5 'DM Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
 color:#F0F0E8;background:rgba(0,0,0,.3);border:1px solid rgba(254,188,46,.22);border-radius:6px;
 user-select:all}
</style>
<div id="mp-login-banner" data-mp-banner>
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


_FONTS = '<link rel="stylesheet" href="/fonts/fonts.css">'


def render_page(html):
    """Substitute the page placeholders every UI page shares, and make the running version
    visible on it. Injecting here (rather than in each .html) is what makes 'every page'
    true by construction — including pages added later."""
    # The brand faces the pages name (Instrument Serif, DM Sans, DM Mono), served by the board
    # server from bin/fonts. Without this every page fell back to Georgia and the system fonts.
    if _FONTS not in html and "</head>" in html:
        html = html.replace("</head>", _FONTS + "</head>", 1)
    banner = _login_banner()
    if banner and "id=\"mp-login-banner\"" not in html:
        if "<body" in html:
            idx = html.index("<body")
            end = html.index(">", idx) + 1
            html = html[:end] + banner + html[end:]
        else:
            html = banner + html
    chrome = ""
    if "id=\"mp-nav\"" not in html:
        chrome += _NAV
    if "id=\"mp-version-badge\"" not in html:
        chrome += _VERSION_BADGE
    if chrome:
        if "</body>" in html:
            html = html.replace("</body>", chrome + "</body>", 1)
        else:
            html += chrome
    # Placeholders are substituted LAST, after every injection: the nav and the banner carry
    # placeholders of their own, and substituting first would ship them to the browser literally.
    html = html.replace("__TTYD_PORT__", str(CFG["TTYD_BROWSER_PORT"]))
    html = html.replace("__HOST_ID__", CFG["HOST_ID"])
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

_JSON_SNAPSHOTS = {}


def read_json_tracked(path, default=None):
    """read_json that remembers what it read, so write_json_merged can tell which keys THIS
    process changed. Half of the roster transaction -- see write_json_merged."""
    data = read_json(path, default)
    _JSON_SNAPSHOTS[path] = copy.deepcopy(data)
    return data


def write_json_merged(path, obj):
    """Write a dict under an exclusive lock, merging onto whatever is on disk NOW.

    run/roster.json was read-modify-write with no mutual exclusion (card e949527956). Each
    write was atomic (atomic_write does fsync + os.replace, so no torn file), but twelve
    writers -- eleven save_roster call sites in bin/mp plus queue-client's heartbeat every
    10s -- meant last-writer-wins: an `mp kill` marking retired=True was erased by a
    heartbeat that had read the file 15ms earlier, and reconcile then correctly revived what
    looked like a crashed agent. eng-554 came back twice that way; eng-555's spawn entry was
    lost the same way and the fleet had no record of a running agent.

    Locking the write alone would fix nothing -- the stale read already happened. So the
    lock spans re-read + merge + write, and only the keys this process actually touched
    since its own read are applied. Keys another writer added meanwhile survive; keys this
    process deleted are deleted. Same flock pattern as _locked_notification_routes.

    A write CONSUMES the tracked read. A deletion is a key that was in this process's own
    read_json_tracked() and is missing from what it writes back -- one read, one write. Keeping
    the merged file as the next snapshot (as this used to) meant a caller that wrote a partial
    dict twice in one process deleted every key it had not mentioned: the Discord plugin's
    relaunch loop dropped 14 agents and their roles from roster.json that way (card
    f864c568e6). Without a fresh tracked read, a write only adds and updates.
    """
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = read_json(path, {}) or {}
        snapshot = _JSON_SNAPSHOTS.get(path)
        if not (isinstance(obj, dict) and isinstance(current, dict)):
            write_json(path, obj)
            _JSON_SNAPSHOTS.pop(path, None)
            return
        merged = dict(current)
        if isinstance(snapshot, dict):
            for k, v in obj.items():
                if k not in snapshot or snapshot[k] != v:
                    merged[k] = v
            for k in snapshot:
                if k not in obj:
                    merged.pop(k, None)
        else:
            # No tracked read since this process last wrote: apply everything, delete nothing.
            merged.update(obj)
        write_json(path, merged)
        _JSON_SNAPSHOTS.pop(path, None)


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


ROUTE_TTL = 24 * 3600  # a reply nobody claimed within a day was never coming


def _route_head(message):
    return " ".join((message or "").split())[:160]


def enqueue_notification_route(target_agent, reply_to, message=""):
    """Queue reply_to to hear how target_agent's turn on THIS message ends. Returns a token."""
    if not target_agent or not reply_to or target_agent == reply_to:
        return ""
    token = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")

    def append(routes):
        routes.append({"token": token, "reply_to": reply_to, "ts": time.time(),
                       "head": _route_head(message)})
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


def open_notification_routes(target_agent, text):
    """Mark the routes whose message reached this turn (it is in TEXT). Drops expired ones."""
    if not target_agent:
        return 0
    seen = " ".join((text or "").split())

    def mark(routes):
        now, kept, opened = time.time(), [], 0
        for r in routes:
            if not isinstance(r, dict) or now - r.get("ts", 0) > ROUTE_TTL:
                continue
            if r.get("head") and r["head"] in seen and not r.get("opened"):
                r["opened"] = True
                opened += 1
            kept.append(r)
        return opened, kept

    return _locked_notification_routes(target_agent, mark)


def claim_notification_routes(target_agent):
    """Who asked to hear how this turn ended: the senders whose message reached it.

    This used to pop the OLDEST route on every Stop, whatever the turn was about, so a turn
    nobody asked for (the Boss talking to the owner) reached whoever messaged it weeks ago
    (card 8f490e73e5). A route queued before pairing (no "head") is still honoured oldest-first,
    one per turn, until it expires. [] when nobody asked."""
    if not target_agent:
        return []

    def claim(routes):
        now = time.time()
        live = [r for r in routes if isinstance(r, dict) and r.get("reply_to")
                and now - r.get("ts", 0) <= ROUTE_TTL]
        mine = [r for r in live if r.get("opened")] or [r for r in live if "head" not in r][:1]
        return list(dict.fromkeys(r["reply_to"] for r in mine)), [r for r in live if r not in mine]

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

# ---------- tmux message delivery (bracketed paste via buffer, with retry) ----------
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def claude_composer_ready(screen):
    """True when a Claude Code screen shows its input box: a `❯` line directly under a
    horizontal rule, with the box's closing rule somewhere below it. Nothing else is a safe
    place to paste. A session still starting has no box
    yet, and text pasted then can vanish without trace; a dialog has none either, and the Enter
    that follows a paste would pick one of its options (the exit dialog's Enter is "review &
    send" for queued feedback drafts; an unanswered question's Enter picks its highlighted
    option). Menus draw `❯` too; a menu's last option can sit under a separator rule, but no
    rule closes it below, only "Enter to select".
    """
    lines = [l for l in (_ANSI.sub("", raw).rstrip() for raw in screen.splitlines()) if l.strip()][-25:]
    rule = [l.lstrip().startswith("\u2500" * 8) for l in lines]
    return any(
        lines[i].lstrip().startswith("\u276f") and rule[i - 1] and any(rule[i + 1:])
        for i in range(1, len(lines)))


def _target_backend(target):
    """The backend recorded for the agent behind mc-<sess>:<tab>, or None for any other pane."""
    if not (target.startswith("mc-") and ":" in target):
        return None
    sess, tab = target[3:].split(":", 1)
    st = read_json(os.path.join(CFG["INSTALL_DIR"], "status", "mc-%s" % sess, "%s.json" % tab), None)
    return (st or {}).get("backend") or None


def pane_input_state(target):
    """"no_pane", "not_ready" (a Claude agent with no input box on screen: still starting, or a
    dialog is up), or "ready". A pane that is not a Claude agent (Codex, Grok, a test receiver)
    reads "ready": there is no composer shape to hold it to."""
    env = dict(os.environ)
    env.pop("TMUX", None)
    if subprocess.run(["tmux", "list-panes", "-t", target], env=env, capture_output=True).returncode != 0:
        return "no_pane"
    if _target_backend(target) != "claude":
        return "ready"
    screen = subprocess.run(["tmux", "capture-pane", "-e", "-p", "-t", target],
                            env=env, capture_output=True, text=True).stdout or ""
    return "ready" if claude_composer_ready(screen) else "not_ready"


def send_when_ready(target, message, wait):
    """tmux_send_message, after waiting up to `wait` seconds for a Claude target to reach its
    input box. Returns "sent", "not_ready" or "no_pane", so a caller that can retry later knows
    to, and one that cannot at least says why instead of printing "sent"."""
    deadline = time.time() + wait
    while pane_input_state(target) == "not_ready" and time.time() < deadline:
        time.sleep(1)
    if tmux_send_message(target, message):
        return "sent"
    state = pane_input_state(target)
    return "not_ready" if state == "not_ready" else "no_pane"


def tmux_send_message(target, message):
    """Deliver a message into a tmux pane's composer and submit it.
    target = mc-<sess>:<tab>. Returns True on success.

    Delivery is `load-buffer` + `paste-buffer -p`, never `send-keys -l`. send-keys was wrong
    in three ways at once (card 5676f76673):

      * It carries no bracketed-paste framing, so a composer reads every newline as Enter.
        One multi-paragraph message was submitted as N fragments; the ones landing while the
        agent was mid-turn were discarded, and it saw only the TAIL. Measured: one send ->
        three submissions. The TUIs' own burst heuristic hid this most of the time, which is
        why it surfaced as rare unexplained "lost during init" messages rather than a
        reproducible break -- correctness was a race.
      * It passes the message as a command ARGUMENT, and tmux refuses past a limit with
        "command too long". Bisected: 16000 chars delivered, 18000 chars dropped WHOLE.
      * Its result was never checked, so both failures returned True and printed "sent".

    load-buffer takes the message on stdin (no argument limit) and `paste-buffer -p` wraps it
    in ESC[200~ / ESC[201~ so the composer takes it as one atomic paste. Every tmux call is
    now checked, and the target is validated as a PANE -- `has-session` only checked the
    session name, so a wrong window reported success while every keystroke was discarded.
    """
    if message is None or not str(message).strip():
        return False
    message = str(message)
    env = dict(os.environ)
    env.pop("TMUX", None)  # never target the caller's pane
    buf = "mp-" + base64.urlsafe_b64encode(os.urandom(9)).decode("ascii").rstrip("=")

    def tmux(*args, **kw):
        return subprocess.run(["tmux"] + list(args), env=env, capture_output=True, **kw)

    for attempt in range(3):
        state = pane_input_state(target)
        if state == "no_pane":
            time.sleep(0.3)
            continue
        if state == "not_ready":
            # Refuse rather than paste into a session that is starting or behind a dialog: the
            # sender learns it failed and can retry, instead of a "sent" that never arrived.
            return False
        if tmux("load-buffer", "-b", buf, "-", input=message.encode("utf-8")).returncode != 0:
            time.sleep(0.3)
            continue
        # -p = bracketed paste markers, -d = drop the buffer once pasted.
        if tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", target).returncode != 0:
            tmux("delete-buffer", "-b", buf)
            time.sleep(0.3)
            continue
        time.sleep(0.15)
        if tmux("send-keys", "-t", target, "Enter").returncode != 0:
            time.sleep(0.3)
            continue
        # A composer that holds the paste as a chip needs a second Enter. Guarded by the
        # marker so we never submit an empty line into a pane that already accepted it.
        if "\n" in message:
            time.sleep(0.4)
            pane = tmux("capture-pane", "-p", "-t", target, "-S", "-30", text=True)
            if "[Pasted text" in (pane.stdout or ""):
                tmux("send-keys", "-t", target, "Enter")
        return True
    return False

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


def http_upload(url, fields, file_path, headers=None, timeout=60):
    """POST multipart/form-data with one file part. Returns (status, parsed_json_or_raw).

    This is how a local artifact (screenshot, clip) becomes a board-served proof. Without it
    the only way to reference a local file is a file:// URL, which no browser will load from
    an http page.
    """
    boundary = "----mp" + base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")
    with open(file_path, "rb") as f:
        payload = f.read()
    ctype = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    parts = []
    for k, v in (fields or {}).items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                      % (boundary, k, v)).encode("utf-8"))
    parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                  'Content-Type: %s\r\n\r\n' % (boundary, os.path.basename(file_path), ctype)
                  ).encode("utf-8"))
    parts.append(payload)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    data = b"".join(parts)
    hdrs = {"Content-Type": "multipart/form-data; boundary=%s" % boundary}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
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
