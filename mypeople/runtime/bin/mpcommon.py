#!/usr/bin/env python3
"""MyPlow shared helpers: config, auth/session, json io, tmux delivery, http proxy.
Python 3 stdlib only."""
import os, sys, json, hmac, hashlib, base64, html, time, socket, ssl, subprocess, shutil, urllib.request, urllib.parse
import errno
import re
import mimetypes
import shlex, signal
import copy
import fcntl
from http.server import BaseHTTPRequestHandler

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
        "DEFAULT_BACKEND", "DEFAULT_AGENT_BACKEND", "DEFAULT_CLAUDE_MODEL", "DEFAULT_CLAUDE_EFFORT", "DEFAULT_CODEX_MODEL", "DEFAULT_GROK_MODEL",
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
    cfg.setdefault("DEFAULT_ENG_MODEL", "claude-opus-5-5")
    cfg.setdefault("DEFAULT_BACKEND", "claude")
    cfg.setdefault("DEFAULT_CLAUDE_MODEL", cfg["DEFAULT_ENG_MODEL"])
    cfg.setdefault("DEFAULT_CLAUDE_EFFORT", "xhigh")
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
 <a href="/plugins" data-mp-path="/plugins"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 22v-5"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/></svg>Plugins</a>
 <a href="/access" data-mp-path="/access"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="12" r="4"/><path d="M12 12h9M18 12v3"/></svg>Access</a>
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
  Open a terminal on this machine and run: __MP_LOGIN_STEPS__
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
        "<code>%s</code>" % html.escape(str(line)) for line in (state.get("howto") or []))
    return _LOGIN_BANNER_TMPL.replace("__MP_LOGIN_STEPS__", steps)


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


_VARIABLE = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def render_template(template, values):
    """Fill a message's variables, in one pass, so words a sender wrote are never read as a variable. A name that is not a
    variable goes out as written."""
    return _VARIABLE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def saved_template(plugin):
    """The owner's edited message for a plugin, or "": then the plugin's own words go out, unchanged. Read per message."""
    try:
        with open(os.path.join(CFG["INSTALL_DIR"], "state", plugin, "template.txt")) as f:
            return f.read()
    except OSError:
        return ""


def plugin_text(plugin, template, values):
    """What a plugin hands an agent: its own words, or the owner's edit of them, with this message's values filled in."""
    return render_template(without_old_name(saved_template(plugin), template) or template, values)


# The variables a message cannot lose: the agent would get none of what was said, could not tell who sent it, or could not answer.
REQUIRED_VARIABLES = ("their_message", "sender", "user_id", "trust_level", "youtube_channel", "reply_command")
# Who sent it, filled in by the plugin for each message, so nobody can type them. The words around them are the owner's. They must stand
# before the person's words: if those came first, a sender could open with words that read like MyPlow's own and pose as the owner.
IDENTITY_VARIABLES = ("sender", "user_id", "trust_level", "youtube_channel")
# These only warn, in the page: the owner may mean to drop them.
WARNED_VARIABLES = ("safety_line", "owner_line", "trust_rules", "confirm_command")


def in_template(names, template):
    return [v for v in names if "{%s}" % v in template]


def checked_template(default, text):
    """A message as written, or ValueError. Every way of saving one comes through here, so none can lose a variable."""
    text = "".join(c if c == "\n" or c.isprintable() else " " for c in str(text or "")).strip()[:8000]
    if not text:
        raise ValueError("a message cannot be empty")
    lost = [v for v in in_template(REQUIRED_VARIABLES, default) if "{%s}" % v not in text]
    if lost:
        raise ValueError("not saved: without %s the agent cannot be told who this is, what they said, or how to answer"
                         % ", ".join("{%s}" % v for v in lost))
    if "{their_message}" in text:
        late = [v for v in in_template(IDENTITY_VARIABLES, default) if text.index("{%s}" % v) > text.index("{their_message}")]
        if late:
            raise ValueError("not saved: {their_message} has to come after %s, or what someone sends could arrive first and read like "
                             "MyPlow's own words" % ", ".join("{%s}" % v for v in late))
    return text


def backend_unready(backend):
    """Why an agent on this backend cannot run on this machine, or "". Started anyway, its window runs nothing and the page says Down
    with no reason (a Codex Boss chosen on a Mac whose one-liner installed Claude only, 11-05). Claude's login is the node's login
    gate's job; Codex's is checked here, as it is the one a person picks on its own."""
    name = {"claude": "Claude", "codex": "Codex", "grok": "Grok"}.get(backend, backend)
    if not shutil.which(backend):
        how = {"claude": "curl -fsSL https://claude.ai/install.sh | bash && claude auth login",
               "codex": "brew install --cask codex && codex login" if sys.platform == "darwin" else "npm install -g @openai/codex && codex login",
               "grok": "install the Grok CLI, then grok login"}.get(backend, "install it")
        return "%s is not installed on this machine. In a terminal, run: %s" % (name, how)
    if backend == "codex":
        try:
            if subprocess.run(["codex", "login", "status"], capture_output=True, timeout=15).returncode != 0:
                return "Codex is installed but not signed in. In a terminal, run: codex login"
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def without_old_name(saved, default):
    """Text saved on 5.22.167 says {trust_stamp}, its one name for the words that say who sent it, {who} for the sender on YouTube and
    {safety_line} for Plow Chat's line that says it is the owner. Read in today's words, so it delivers right and the page can show and
    check it. default: the same message's own words, which have those words before {their_message}."""
    words = default[:default.find("{their_message}")].rsplit("\n", 1)[-1].replace("{seen_line}", "").rstrip(' "')
    if "{owner_line}" in default and "{safety_line}" not in default:      # Plow Chat's, then; a message with both keeps both
        saved = saved.replace("{safety_line}", "{owner_line}")
    return saved.replace("{who}", "{sender}").replace("{trust_stamp}:", words.rstrip(":") + ":").replace("{trust_stamp}", words)


def _rule_text(plugin):
    try:
        with open(os.path.join(CFG["INSTALL_DIR"], "state", plugin or "-", "wrap.txt")) as f:
            return " ".join(f.read().split())
    except OSError:
        return ""


def owner_rule(plugin):
    """The owner's own text for what a plugin hands an agent, from the Plugins page, as a line to add to a message. Read per
    message, so an edit needs no restart. The command is why a sender typing the same words cannot pass as the owner."""
    text = _rule_text(plugin)
    return ("\n(Your owner's rule for this plugin: %s Your MyPlow added this line, not the sender; confirm it by running: "
            "mp plugins wrap %s)" % (text, plugin)) if text else ""


def cli_session(pane_pid, run):
    """What changes when the CLI in a pane's `while true; do cli; done` restarts: its pid. The pane id and the container's
    start time do not. run(shell_line) gives its stdout wherever the pane lives; /proc is for an image with no pgrep.
    "" when it cannot tell."""
    p = str(pane_pid or "").strip()
    return " ".join(str(run("pgrep -n -P %s || cat /proc/%s/task/%s/children" % (p, p, p)) or "").split()) if p.isdigit() else ""


def give_rule(plugin, session, paste):
    """The owner's text for a plugin that pastes strangers' words into its own locked agent. That agent reads all of such an
    input as data, so a line added to it would never count: the rule goes as an input of its own, once per CLI session and
    per change. paste(text) is True only if it went in, and only then does it count as told; False here means it did not go
    in, so the caller holds the stranger's message back and tries again. A session it cannot tell ("") never counts as told:
    the rule repeats rather than go missing."""
    path, text = os.path.join(CFG["INSTALL_DIR"], "state", plugin, "wrap.given"), _rule_text(plugin)
    was_session, was_text = read_json(path, None) or ["", ""]
    if (not text and not was_text) or (session and (was_session, was_text) == (session, text)):
        return True
    if text:
        note = "Your owner's rule for this plugin, from their app, for every message from now on: %s" % text
    else:
        note = "Your owner removed their rule for this plugin." if was_session == session else ""
    if note and not paste(note):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write(path, json.dumps([session, text]).encode("utf-8"))
    return True


def card_note(card, frm):
    """What mp send --card adds for the agent it reaches, so the answer lands on the same thread."""
    return "\n(On card %s. To answer on it: mp send --card %s %s \"...\")" % (card, card, frm)


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
        return
    seen = " ".join((text or "").split())

    def mark(routes):
        now, kept = time.time(), []
        for r in routes:
            if not isinstance(r, dict) or now - r.get("ts", 0) > ROUTE_TTL:
                continue
            if r.get("head") and r["head"] in seen:
                r["opened"] = True
            kept.append(r)
        return None, kept

    _locked_notification_routes(target_agent, mark)


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
    while time.time() < deadline and any(_process_alive(pid) for pid in pids):
        time.sleep(0.05)


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


def input_end(screen):
    """What the input box shows above the screen's last rule (back to the rule before it, or the top
    of the screen), whitespace removed; None with no rule on screen. Its end is the end of the input
    even when the box is taller than the window, where its `\u276f` line has scrolled away (11-08: the
    Boss's window took a 90x25 client's size)."""
    lines = [l for l in (_ANSI.sub("", raw).rstrip() for raw in screen.splitlines()) if l.strip()]
    rules = [i for i, l in enumerate(lines) if l.lstrip().startswith("\u2500" * 8)]
    if not rules:
        return None
    return re.sub(r"\s", "", "".join(lines[rules[-2] + 1 if len(rules) > 1 else 0:rules[-1]]))


# Claude Code ends a paste that has had no bytes for 2 s (its input reader's IN_PASTE timeout) and
# takes the rest as typed keys, and a pty hands a paste over about 1 KB per read. So an agent busy
# enough to stall between two reads of one long paste got it in pieces, and sometimes lost the head
# (11-07: 83 of the Boss's 271 long messages). A piece this size arrives in one read, and one of at
# most 800 characters and 2 lines goes into the box as text, not a "[Pasted text]" chip.
PASTE_BYTES, PASTE_CHARS, PASTE_LINES = 900, 800, 2
PASTE_WAIT = 30   # seconds a long message waits, in all, for its pieces to show in the box
# What a caller gives one mp send: more than its longest delivery (MP_SEND_READY_WAIT 10 s for the
# box, PASTE_WAIT + 5 for another sender's turn, PASTE_WAIT for the pieces, then the Enter). A shorter
# one killed mp halfway through a long message, left it unsent in the box and pasted it again on the
# retry, and its sender was told it had failed (11-08, plow-chat at 30 s with the Boss stalled).
SEND_TIMEOUT = 90


def paste_pieces(message):
    """`message` in pieces of at most PASTE_BYTES UTF-8 bytes, PASTE_CHARS characters and
    PASTE_LINES line breaks, cut after a line break or a space when the piece has one."""
    pieces, rest = [], message
    while rest:
        used = breaks = 0
        for n, ch in enumerate(rest):
            used, breaks = used + len(ch.encode("utf-8")), breaks + (ch == "\n")
            if used > PASTE_BYTES or n >= PASTE_CHARS or breaks > PASTE_LINES:
                break
        else:
            return pieces + [rest]
        cut = rest.rfind("\n", 0, n) + 1 or rest.rfind(" ", 0, n) + 1 or n
        pieces.append(rest[:cut])
        rest = rest[cut:]
    return pieces


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
    if tmux_run("list-panes", "-t", target, env=env).returncode != 0:
        return "no_pane"
    if _target_backend(target) != "claude":
        return "ready"
    screen = tmux_run("capture-pane", "-e", "-p", "-t", target, env=env, text=True).stdout or ""
    return "ready" if claude_composer_ready(screen) else "not_ready"


def send_when_ready(target, message, wait):
    """tmux_send_message, after waiting up to `wait` seconds for a Claude target to reach its
    input box. Returns "sent", "not_ready", "no_pane", or for a retired agent a sentence saying
    so, so a caller that can retry later knows to, and one that cannot at least says why instead
    of printing "sent"."""
    deadline = time.time() + wait
    while pane_input_state(target) == "not_ready" and time.time() < deadline:
        time.sleep(1)
    if tmux_send_message(target, message):
        return "sent"
    state = pane_input_state(target)
    return "not_ready" if state == "not_ready" else (retired_note(target) or "no_pane")


def retired_note(target):
    """What a send to mc-<sess>:<tab> says when the roster has that agent retired, else "".

    A bare "no_pane" reads as a hiccup: on 2026-10-23 an A2A GO to an agent killed 4 h earlier
    got one, and its sender waited 1.5 h for a result (card 65c54015d5). Local mp send and the
    queue-client (which A2A deliveries go through) both return this to the sender."""
    sess, _, tab = target[3:].partition(":") if target.startswith("mc-") else ("", "", "")
    aid = "%s/%s:%s" % (CFG["HOST_ID"], sess, tab)
    rr = (read_json(os.path.join(CFG["INSTALL_DIR"], "run", "roster.json"), {}) or {}).get(aid) or {}
    if not rr.get("retired"):
        return ""
    mins = int((time.time() - rr.get("retired_ts", time.time())) // 60)
    return ("not sent: %s was retired %s ago (%s), so no one got this message; "
            "`mp revive %s` on its MyPlow brings it back"
            % (aid, "%d min" % mins if mins < 120 else "%d h" % (mins // 60),
               rr.get("retire_reason") or "mp kill", aid))


# tmux serves on one thread with about 128 connections queued behind it. While it is stalled, a client
# past that queue is refused and says "no server running", rc 1, in 0.03 s, with the server alive
# (card a4607576c1). So a stall reads as no pane, no window and no session at once, and acting on that
# is what must never happen. Short enough that three tries still fit plow-chat's 30 s around mp send.
TMUX_STALL_WAIT = 8.0
TMUX_REFUSED = ("no server running", "error connecting")


def tmux_refused(r):
    """Did this tmux result fail at the connection, before any command ran?"""
    err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else str(r.stderr or "")
    return r.returncode != 0 and any(m in err for m in TMUX_REFUSED)


def tmux_server_gone(env=None):
    """True when no server can be behind the default socket, False when it may only be stalled.

    A server that takes the connection is there. A refusal on Linux means nothing listens: a full queue makes a client wait there
    instead. macOS refuses a full queue too, so there the server counts as there only if a tmux process holds this very socket. Any
    daemonized tmux used to count, and a builder's private `tmux -L` server held a crashed default one's recovery back (11-05).
    """
    if not hasattr(os, "getuid"):
        return True  # no uid, no tmux-<uid> directory, no ps (a Windows port on psmux): it cannot tell a stalled server from a gone one, so a refused command is final
    env = os.environ if env is None else env
    path = os.path.join(env.get("TMUX_TMPDIR") or "/tmp", "tmux-%d" % os.getuid(), "default")
    if not os.path.exists(path):
        return True
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(path)
        return False
    except OSError as e:
        if e.errno not in (errno.ECONNREFUSED, errno.ENOENT, errno.ENOTSOCK):
            return False    # a timeout, Linux's full queue, no permission: there is a server, maybe stalled
    finally:
        s.close()
    if sys.platform != "darwin":
        return True
    try:
        held = subprocess.run(["lsof", "-U", "-a", "-c", "tmux", "-Fn"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return not {path, os.path.realpath(path)} & {line[1:] for line in held.splitlines() if line.startswith("n")}


def tmux_run(*args, env=None, wait=None, **kw):
    """Run a tmux command, riding out a stalled server. Returns the last result."""
    if env is None:
        env = dict(os.environ)
        env.pop("TMUX", None)
    deadline = time.time() + (TMUX_STALL_WAIT if wait is None else wait)
    while True:
        r = subprocess.run(["tmux"] + list(args), env=env, capture_output=True, **kw)
        if not tmux_refused(r) or time.time() >= deadline or tmux_server_gone(env):
            return r
        time.sleep(0.25)


def tmux_windows(wait=None):
    """{"mc-main:eng-1", ...} from one tmux call, or None when the server cannot be read right now.

    None is not "no windows": deciding on it would retire and respawn live agents. Forking tmux per
    agent here and in mp reconcile, retired agents included, was 59% of the commands tmux logged.
    """
    env = dict(os.environ)
    env.pop("TMUX", None)
    r = tmux_run("list-windows", "-a", "-F", "#{session_name}:#{window_name}", env=env, wait=wait, text=True)
    if r.returncode == 0:
        return set(r.stdout.splitlines())
    return set() if tmux_server_gone(env) else None


def pane_key(target, key):
    """Press a key ("Enter", or a literal like "2") for the program in a pane. Returns True on success.

    send-keys goes to a pane's MODE first: with the window chooser open on an agent, a delivered
    message's Enter picked the highlighted row, jumped whoever was looking, and left the message
    unsubmitted in the composer; copy-mode ate it the same way. A paste always reaches the program.
    """
    env = dict(os.environ)
    env.pop("TMUX", None)

    def tmux(*args, **kw):
        return tmux_run(*args, env=env, **kw)

    if tmux("display-message", "-p", "-t", target, "#{pane_in_mode}", text=True).stdout.strip() != "1":
        return tmux("send-keys", "-t", target, key).returncode == 0
    buf = "mp-" + base64.urlsafe_b64encode(os.urandom(9)).decode("ascii").rstrip("=")
    raw = b"\r" if key == "Enter" else key.encode("utf-8")
    return (tmux("load-buffer", "-b", buf, "-", input=raw).returncode == 0 and
            tmux("paste-buffer", "-r", "-d", "-b", buf, "-t", target).returncode == 0)


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

    To a Claude agent a long message goes as paste_pieces, each pasted only once the one before
    shows in the input box, so no paste spans two pty reads a stall can sit between. Deliveries
    to one pane take turns: another sender's Enter between two pieces submitted half a message
    (11-07, a [swarm chief] envelope reached the Boss as two).
    """
    if message is None or not str(message).strip():
        return False
    lock_path = os.path.join(CFG["INSTALL_DIR"], "run", "send-locks", re.sub(r"[^\w.-]", "_", target) + ".lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+") as lock:
        give_up = time.time() + PASTE_WAIT + 5   # another sender's longest turn; a stuck one must not stop every other
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > give_up:
                    break
                time.sleep(0.05)
        return _send_message(target, str(message))


def _send_message(target, message):
    env = dict(os.environ)
    env.pop("TMUX", None)  # never target the caller's pane
    buf = "mp-" + base64.urlsafe_b64encode(os.urandom(9)).decode("ascii").rstrip("=")

    def tmux(*args, **kw):
        return tmux_run(*args, env=env, **kw)

    def box():
        return input_end(tmux("capture-pane", "-p", "-t", target, text=True).stdout or "")

    pieces = paste_pieces(message) if _target_backend(target) == "claude" else [message]
    pasted, deadline = 0, time.time() + PASTE_WAIT
    for attempt in range(3):
        while pasted < len(pieces):
            state = pane_input_state(target) if not pasted else "ready"
            if state == "not_ready":
                # Refuse rather than paste into a session that is starting or behind a dialog: the
                # sender learns it failed and can retry, instead of a "sent" that never arrived.
                return False
            before = box() if len(pieces) > 1 and state == "ready" else None
            # -p = bracketed paste markers, -d = drop the buffer once pasted.
            if state == "no_pane" or tmux("load-buffer", "-b", buf, "-",
                                          input=pieces[pasted].encode("utf-8")).returncode:
                break
            if tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", target).returncode != 0:
                tmux("delete-buffer", "-b", buf)
                break
            pasted += 1  # a retry from here is for what is left: pasting a piece again would double it
            # Landed = the box ends with this piece, so the agent has read it and the pty is empty
            # again. A box that merely changed is not proof: the Boss's re-wraps on every resize of
            # its window, and pieces sent on that piled up in the pty and were split again (11-07).
            tail = re.sub(r"\s", "", pieces[pasted - 1])[-24:]
            while before is not None and time.time() < deadline:
                now = box()
                if now is not None and now != before and now.endswith(tail):
                    break
                time.sleep(0.05)
        if pasted < len(pieces):
            time.sleep(0.3)
            continue
        time.sleep(0.15)
        if not pane_key(target, "Enter"):
            time.sleep(0.3)
            continue
        # A composer that holds the paste as a chip needs a second Enter. Guarded by the
        # marker so we never submit an empty line into a pane that already accepted it.
        if "\n" in message:
            time.sleep(0.4)
            pane = tmux("capture-pane", "-p", "-t", target, "-S", "-30", text=True)
            if "[Pasted text" in (pane.stdout or ""):
                pane_key(target, "Enter")
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

# ---------- websocket client (the chat bridges: Plow's event socket, Discord's gateway) ----------
class Closed(OSError):
    """A close frame, with its code: Plow's 4401 = a refused ticket, Discord's 4004 = a dead token."""

    def __init__(self, code, reason):
        OSError.__init__(self, "websocket closed %d %s" % (code, reason))
        self.code = code


class WebSocket:
    """Just enough RFC 6455 on the stdlib: text both ways, uncompressed (no extension is offered),
    fragments joined, a ping answered, a close frame raised as Closed. `heard` is when the server
    last sent anything, so a caller can tell a dead socket from a quiet one."""

    def __init__(self, url, timeout=15):
        u = urllib.parse.urlsplit(url)
        sock = socket.create_connection((u.hostname, u.port or (80 if u.scheme == "ws" else 443)), timeout)
        if u.scheme != "ws":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        self.sock, self.buf, self.parts, self.heard = sock, b"", b"", time.time()
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(("GET %s?%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                      % (u.path or "/", u.query, u.netloc, key)).encode())
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if not head.startswith(b"HTTP/1.1 101") or accept not in head:
            raise OSError("websocket refused: %r" % head.splitlines()[0][:100])

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise OSError("websocket closed by the server")
        self.buf += chunk
        self.heard = time.time()

    def recv(self, timeout):
        """The next text message, or None when none came within `timeout`. Raises once closed."""
        self.sock.settimeout(timeout)
        while True:
            b = self.buf
            n, at = (b[1] & 0x7F, 2) if len(b) >= 2 else (0, 2)
            if n > 125:
                at = 4 if n == 126 else 10
                n = int.from_bytes(b[2:at], "big") if len(b) >= at else 0
            if len(b) < 2 or len(b) < at + n:      # a frame not yet whole: read more, keep what came
                try:
                    self._fill()
                except socket.timeout:
                    return None
                continue
            op, fin, data, self.buf = b[0] & 0x0F, b[0] & 0x80, b[at:at + n], b[at + n:]
            if op == 0x9:
                self.send(data, 0xA)
            elif op == 0x8:
                raise Closed(int.from_bytes(data[:2], "big") if len(data) >= 2 else 1005,
                             data[2:].decode(errors="replace"))
            elif op in (0x0, 0x1, 0x2):
                self.parts += data
                if fin:
                    text, self.parts = self.parts.decode(), b""
                    return text

    def send(self, data, op=0x1):
        """One frame, masked like every client frame: a text unless op says otherwise (ping 0x9, pong 0xA)."""
        data = data.encode() if isinstance(data, str) else data
        n = len(data)
        size = bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + n.to_bytes(2, "big") if n < 65536 \
            else bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | op]) + size + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

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


# ---------- HTTP server side (the board and the queue share this plumbing) ----------
class JSONHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, ctype="application/json", raw=None, extra=None):
        if raw is None:
            raw = json.dumps(obj).encode("utf-8") if obj is not None else b""
        elif isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        if extra:
            for k, v in extra.items():
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

    def _page_extra(self):
        return {
            "Set-Cookie": "mp_session=%s; HttpOnly; Path=/; SameSite=Lax" % mint_session(),
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache", "Expires": "0",
        }
