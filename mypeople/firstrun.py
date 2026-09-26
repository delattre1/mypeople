"""First-run entrypoint — REPLACES seed hydration with configuration only (never code-gen).

ensure() is idempotent: safe on every `up` / container restart. It materializes a writable
INSTALL_DIR from the packaged runtime, resolves the selected backend's auth, writes the single
config file (~/.config/mypeople/queue.env, fresh QUEUE_SECRET per install), wires Claude/Codex
lifecycle hooks, and installs the functional tmux.conf. Starting daemons + spawning the Boss is
the CLI's job (see cli.up)."""
import os, sys, json, shutil, secrets, socket, stat, subprocess, shlex, time

VALID_BACKENDS = ("claude", "codex", "grok")
LIFECYCLE_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")

def _config_path():
    explicit = os.environ.get("MYPEOPLE_CONFIG_PATH")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    home = os.environ.get("MYPEOPLE_HOME")
    if home:
        return os.path.join(os.path.abspath(os.path.expanduser(home)), "config", "queue.env")
    return os.path.expanduser("~/.config/mypeople/queue.env")


CONFIG_PATH = _config_path()
CONFIG_DIR = os.path.dirname(CONFIG_PATH)


def runtime_dir():
    """Absolute path to the packaged runtime tree (bin/, plugins/, verify/, ...)."""
    try:
        from importlib.resources import files
        return str(files("mypeople") / "runtime")
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime")


def install_dir():
    return os.path.abspath(os.path.expanduser(
        os.environ.get("MYPEOPLE_HOME") or "~/.local/share/mypeople"))


def _echo(msg):
    sys.stderr.write(msg + "\n")


# ---------------------------------------------------------------- step 1: materialize
def _replace_file(src, dst):
    """copytree's default copy2 writes THROUGH the destination, which fails on an install that
    mprole has already published to: the role store is chmod 444 so a runtime agent cannot rewrite
    its own personality. Replace the file and put that mode back, so upgrading an install that has
    ever spawned a role doesn't die half-way with EACCES."""
    mode = None
    if os.path.lexists(dst):
        try:
            mode = stat.S_IMODE(os.lstat(dst).st_mode)
        except OSError:
            mode = None
        try:
            os.unlink(dst)
        except OSError:
            pass
    shutil.copy2(src, dst)
    if mode is not None and not mode & stat.S_IWUSR:
        try:
            os.chmod(dst, mode)
        except OSError:
            pass


def _write_file(dst, text):
    """Same replace-don't-write-through discipline as _replace_file, for content we generate
    rather than copy."""
    try:
        os.unlink(dst)
    except OSError:
        pass
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text)


def _read_registry_roles(path):
    """The roles map of an existing install's registry, or {} if there is none to keep."""
    try:
        with open(path, encoding="utf-8") as f:
            roles = json.load(f).get("roles")
        return roles if isinstance(roles, dict) else {}
    except (OSError, ValueError):
        return {}


def _version_tuple(text):
    """(5, 13, 1) from "5.13.1". None when it is not a plain numeric version."""
    parts = (text or "").strip().split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def installed_version(install):
    try:
        with open(os.path.join(install, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return ""


def refuse_downgrade(install):
    """True when INSTALL_DIR already holds a NEWER release than this package carries.

    Materialize overwrites the install with whatever the caller ships, and the desktop app
    carries its own copy of the runtime. So an old .app -- one left in a build folder, or simply
    reopened by macOS after a reboot -- silently rolled a 5.13.1 install back to 5.8.0 twice on
    the CEO's Mac, taking every feature since with it and breaking message delivery. Nothing here
    can tell an intended downgrade from an accident, so the older side always yields.

    Set MYPEOPLE_ALLOW_DOWNGRADE=1 to force it (a deliberate rollback still has a way through).
    """
    if os.environ.get("MYPEOPLE_ALLOW_DOWNGRADE") == "1":
        return False
    from . import __version__
    here, there = _version_tuple(__version__), _version_tuple(installed_version(install))
    return bool(here and there and there > here)


def materialize(install):
    """Copy the packaged runtime into a WRITABLE INSTALL_DIR. Idempotent: never overwrite
    existing daemon code differently, and NEVER clobber live state (board/roster/logs)."""
    from . import __version__
    if refuse_downgrade(install):
        _echo("[myplow] this build is %s and %s already runs %s — leaving it alone.\n"
              "[myplow] open the current app, or set MYPEOPLE_ALLOW_DOWNGRADE=1 to force it."
              % (__version__, install, installed_version(install)))
        return
    rt = runtime_dir()
    os.makedirs(install, exist_ok=True)
    # code trees: refresh from the package so upgrades take effect (state dirs excluded below).
    # "roles" carries the versioned role store (personalities + Skills + profiles). mp resolves
    # every --role spawn against INSTALL_DIR/roles and fails closed if it is absent, so omitting
    # it here would ship a runtime whose Boss cannot be born.
    # roles/registry.json is the spawn allowlist, and an install can hold roles the product does
    # not ship -- anything the creator authored here. The copy below would replace that file with
    # the shipped one, silently unregistering every authored role (they stay on disk but become
    # unspawnable). So keep the entries this release does not name; shipped names still move
    # forward to the versions this release carries.
    kept_roles = _read_registry_roles(os.path.join(install, "roles", "registry.json"))
    for sub in ("bin", "plugins", "plans", "verify", "config", "roles"):
        src = os.path.join(rt, sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(install, sub), dirs_exist_ok=True,
                            copy_function=_replace_file)
    if kept_roles:
        reg_path = os.path.join(install, "roles", "registry.json")
        with open(reg_path, encoding="utf-8") as f:
            reg = json.load(f)
        mode = stat.S_IMODE(os.stat(reg_path).st_mode)
        merged = dict(kept_roles)
        merged.update(reg.get("roles") or {})
        # a kept entry whose profile this release deleted would fail closed at spawn; drop it.
        reg["roles"] = {r: ref for r, ref in merged.items()
                        if os.path.isfile(os.path.join(install, "roles", *ref.split("/")))}
        _write_file(reg_path, json.dumps(reg, indent=1) + "\n")
        os.chmod(reg_path, mode)   # the store is published read-only; keep it that way
    # Files a previous version shipped and this one does not. copytree(dirs_exist_ok) only ever
    # adds, so a retired page would survive every upgrade forever as a file the product no longer
    # serves. Removing it here is what makes "removed" true on an upgraded install, not just a
    # fresh one.
    for gone in ("bin/wall.html", "boss-CLAUDE.md"):
        try:
            os.remove(os.path.join(install, *gone.split("/")))
        except OSError:
            pass
    # Boss doctrine file
    bc = os.path.join(rt, "mp-boss-doctrine.md")
    if os.path.exists(bc):
        _replace_file(bc, os.path.join(install, "mp-boss-doctrine.md"))
    # Stamp the version this runtime was materialized from. The daemons run the copies above
    # under a bare interpreter with no route back to the package, so this file is how they (and
    # the UI) know which version is serving. Rewritten here so an upgrade cannot leave a stale
    # number on the pages; a hand-placed VERSION is replaced the same way the code trees are.
    from . import __version__
    _write_file(os.path.join(install, "VERSION"), __version__ + "\n")
    # writable state skeletons — create empty, never overwrite existing board/roster/logs
    for sub in ("todos", "run", "status", "logs"):
        os.makedirs(os.path.join(install, sub), exist_ok=True)
    # make scripts executable (package-data can lose the bit on some backends)
    bindir = os.path.join(install, "bin")
    for f in os.listdir(bindir):
        p = os.path.join(bindir, f)
        if f.endswith((".py", ".sh")) or f in ("mp", "board-restore"):
            try:
                os.chmod(p, 0o755)
            except OSError:
                pass
    for f in ("emit-event.sh",):
        p = os.path.join(install, "plugins", "tmux-boss-hooks", f)
        if os.path.exists(p):
            os.chmod(p, 0o755)
    vs = os.path.join(install, "verify", "verify.sh")
    if os.path.exists(vs):
        os.chmod(vs, 0o755)


# ---------------------------------------------------------------- step 2: auth
def _authcheck():
    """Load the runtime's authcheck module (shared with the daemons, which cannot import us)."""
    import importlib.machinery, importlib.util
    path = os.path.join(runtime_dir(), "bin", "authcheck.py")
    loader = importlib.machinery.SourceFileLoader("mypeople_authcheck", path)
    spec = importlib.util.spec_from_loader("mypeople_authcheck", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _claude_authenticated():
    """A parseable credential file is NOT a login (card 8a6ebe4e48).

    `claude auth status` returns `loggedIn: true` for a credential whose access AND refresh tokens
    both expired -- proven against a hand-built dead credential, where the very next `claude -p`
    answered `Failed to authenticate. API Error: 401`. Trusting that flag is what made MyPlow
    announce `this node's claude login is active`, start the Boss, and paint the HUD ALIVE while
    the agent sat at "Not logged in". So the flag is now only the FIRST hurdle: the credential
    still has to survive authcheck (expiry stamps, then a real one-word probe).
    """
    if not shutil.which("claude"):
        return False
    try:
        r = subprocess.run(["claude", "auth", "status"], capture_output=True,
                           text=True, timeout=15)
        logged_in = False
        try:
            logged_in = bool(json.loads(r.stdout).get("loggedIn"))
        except Exception:
            normalized = "".join(ch for ch in (r.stdout + r.stderr).lower() if ch.isalnum())
            logged_in = "loggedintrue" in normalized or "loginmethod" in normalized
        if r.returncode != 0 or not logged_in:
            return False
    except Exception:
        return False
    try:
        state, detail = _authcheck().claude_verdict(timeout=90)
    except Exception:
        # authcheck itself is broken/absent -- fall back to the old (weaker) answer rather than
        # locking out a node that really is logged in.
        return True
    if state == "dead":
        _echo("[myplow] claude credential rejected: %s" % detail)
        return False
    return True


def _codex_authenticated():
    if shutil.which("codex"):
        try:
            r = subprocess.run(["codex", "login", "status"], capture_output=True,
                               text=True, timeout=15)
            return r.returncode == 0
        except Exception:
            pass
    return False


def _grok_authenticated():
    """grok has no `login status` verb, and `grok models` EXITS 0 EVEN WHEN LOGGED OUT --
    it just prints "You are not authenticated." and the anonymous model list. So the
    returncode idiom used for codex would pass an unauthenticated node; the affirmative
    stdout marker is the only honest signal. Fail closed on anything unrecognized."""
    if shutil.which("grok"):
        try:
            r = subprocess.run(["grok", "models"], capture_output=True,
                               text=True, timeout=25)
            out = (r.stdout + r.stderr).lower()
            if r.returncode == 0 and "you are logged in" in out:
                return True
        except Exception:
            pass
    return False


def authenticated_backends():
    """Every backend this node has its own completed login for, in VALID_BACKENDS order."""
    checks = {"claude": _claude_authenticated, "codex": _codex_authenticated,
              "grok": _grok_authenticated}
    return [b for b in VALID_BACKENDS if checks[b]()]


def resolve_auth(preferred=None, chooser=None):
    """Require this node's own completed login for the selected agent backend.

    With no explicit preference this used to return the first authenticated backend in
    VALID_BACKENDS order, so a machine that merely had Claude installed got Claude and was never
    asked -- reported as "since I have claude installed it used that for mypeople instead of
    chatgpt automatically" (card 5211716904, items 8+9). List order is an implementation detail,
    not a decision: when several logins are live, `chooser` decides, and if there is nobody to ask
    the pick is at least stated out loud along with how to override it.
    """
    preferred = (preferred or "").strip().lower()
    if preferred and preferred not in VALID_BACKENDS:
        return False, preferred, "Unknown backend %r; choose claude, codex or grok." % preferred
    checks = {"claude": _claude_authenticated, "codex": _codex_authenticated,
              "grok": _grok_authenticated}
    if preferred:
        if checks[preferred]():
            return True, preferred, "this node's %s login is active" % preferred
        available = []
    else:
        available = authenticated_backends()
    if available:
        if len(available) == 1:
            return True, available[0], "this node's %s login is active" % available[0]
        picked = (chooser(available) or "").strip().lower() if chooser else ""
        if picked in available:
            return True, picked, "using %s (chosen; %s also authenticated)" % (
                picked, ", ".join(b for b in available if b != picked))
        return True, available[0], (
            "%s are all authenticated; defaulting to %s. Re-run with `--backend <name>` to pick "
            "another." % (", ".join(available), available[0]))
    requested = preferred or "claude, codex or grok"
    return (False, preferred or "none",
            "This node is not authenticated for %s. Run `claude auth login`, `codex login` or "
            "`grok login` inside THIS node, then re-run MyPlow. Never copy or mount credentials "
            "from another node." % requested)


def auth_state_path(install=None):
    return os.path.join(install or install_dir(), "status", "auth.json")


def login_howto():
    """The exact commands that fix an unauthenticated node, phrased for where it is running.

    Written into the auth state file so the CLI, the board and the HUD all quote the same
    instruction instead of three copies drifting apart.
    """
    if os.environ.get("MYPEOPLE_DESKTOP") == "1":
        return ["open the terminal tab above and run:  claude auth login",
                "(`codex login` / `grok login` work too)"]
    if os.environ.get("MYPEOPLE_CONTAINER") == "1":
        return ["docker compose exec -it mypeople claude auth login",
                "or open the terminal tab above and run:  claude auth login",
                "(`codex login` / `grok login` work too)"]
    return ["claude auth login",
            "(`codex login` / `grok login` work too)",
            "then re-run `mypeople up`"]


def write_auth_state(install, authenticated, backend=None, message="", requested=""):
    """Publish this node's login state where the runtime can read it.

    The daemons run out of INSTALL_DIR under a bare interpreter with no route back to this
    package (same constraint that makes INSTALL_DIR/VERSION exist), so a file is how the pages
    and the boss supervisor learn whether this node can actually talk to an AI backend.
    """
    path = auth_state_path(install)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_json(path, {
            "authenticated": bool(authenticated),
            "backend": backend or "",
            "requested": requested or "",
            "message": message or "",
            "howto": [] if authenticated else login_howto(),
            "checked_at": int(time.time()),
        })
    except Exception:
        pass  # a status file must never be the reason the stack fails to come up
    return path


def _prompt_backend(available):
    """Ask which authenticated backend to run, when there is a human on the other end.

    A non-interactive install (docker build, CI, any piped stdin) must never block on a prompt, so
    this answers nothing and lets resolve_auth fall back to a stated default.
    """
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return ""
    except Exception:
        return ""
    _echo("\n[myplow] More than one AI backend is authenticated on this node:")
    for i, b in enumerate(available, 1):
        _echo("  %d) %s" % (i, b))
    try:
        raw = input("[myplow] Which should MyPlow use? [1-%d, default %s]: "
                    % (len(available), available[0])).strip()
    except (EOFError, KeyboardInterrupt):
        return ""
    if not raw:
        return available[0]
    if raw.isdigit() and 1 <= int(raw) <= len(available):
        return available[int(raw) - 1]
    return raw.lower()


# ---------------------------------------------------------------- step 3: queue.env
def _write_env_file(path, lines):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_queue_env(install, backend):
    """Write the single config file if absent. Idempotent — reuse keeps the same
    QUEUE_SECRET/HOST_ID across restarts so board/sessions persist."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            lines = f.read().splitlines()
        existing = {}
        for line in lines:
            body = line.strip()
            body = body[7:] if body.startswith("export ") else body
            if "=" not in body:
                continue
            key, value = body.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            existing[key.strip()] = value
        replacements = {
            "DEFAULT_BACKEND": backend,
            "DEFAULT_CLAUDE_MODEL": os.environ.get(
                "DEFAULT_CLAUDE_MODEL", existing.get(
                    "DEFAULT_CLAUDE_MODEL", existing.get("DEFAULT_ENG_MODEL", "claude-opus-4-8"))),
            "DEFAULT_CODEX_MODEL": os.environ.get(
                "DEFAULT_CODEX_MODEL", existing.get("DEFAULT_CODEX_MODEL", "")),
        }
        seen = set()
        out = []
        for line in lines:
            stripped = line.strip()
            body = stripped[7:] if stripped.startswith("export ") else stripped
            key = body.split("=", 1)[0] if "=" in body else ""
            if key in replacements:
                out.append('export %s="%s"' % (key, replacements[key]))
                seen.add(key)
            else:
                out.append(line)
        for key, value in replacements.items():
            if key not in seen:
                out.append('export %s="%s"' % (key, value))
        _write_env_file(CONFIG_PATH, out)
        return False
    host = os.environ.get("HOST_ID") or socket.gethostname().split(".")[0]
    kv = {
        "INSTALL_DIR": install,
        "HOST_ID": host,
        "QUEUE_SECRET": secrets.token_hex(24),          # FRESH per install; never baked
        "HUD_PORT": os.environ.get("HUD_PORT", "9900"),
        "TODO_PORT": os.environ.get("TODO_PORT", "9933"),
        "TTYD_PORT": os.environ.get("TTYD_PORT", "7681"),
        "TTYD_BROWSER_PORT": os.environ.get(
            "TTYD_BROWSER_PORT", os.environ.get("TTYD_PORT", "7681")),
        # read-only ttyd behind the Terminal Graph's tiles (stock ttyd, no -W)
        "TTYD_RO_PORT": os.environ.get(
            "TTYD_RO_PORT", str(int(os.environ.get("TTYD_PORT", "7681")) + 1)),
        "BIND_ADDR": os.environ.get("BIND_ADDR", "0.0.0.0"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DEFAULT_BACKEND": backend,
        "DEFAULT_ENG_MODEL": os.environ.get("DEFAULT_ENG_MODEL", "claude-opus-4-8"),
        "DEFAULT_CLAUDE_MODEL": os.environ.get(
            "DEFAULT_CLAUDE_MODEL", os.environ.get("DEFAULT_ENG_MODEL", "claude-opus-4-8")),
        "DEFAULT_CODEX_MODEL": os.environ.get("DEFAULT_CODEX_MODEL", ""),
        "DEFAULT_GROK_MODEL": os.environ.get("DEFAULT_GROK_MODEL", ""),
        "QUEUE_DEAD_AFTER": "45",
        "HEARTBEAT_INTERVAL": "10",
    }
    kv["QUEUE_URL"] = os.environ.get("QUEUE_URL", "http://127.0.0.1:%s" % kv["HUD_PORT"])
    lines = ["# MyPlow runtime config — generated on first run (secrets here; never commit)"]
    lines += ['export %s="%s"' % (k, v) for k, v in kv.items()]
    _write_env_file(CONFIG_PATH, lines)
    return True


# ---------------------------------------------------------------- step 4: backend hooks
def _mypeople_hook_group(group):
    for handler in group.get("hooks", []) if isinstance(group, dict) else []:
        command = handler.get("command", "") if isinstance(handler, dict) else ""
        if "/plugins/tmux-boss-hooks/emit-event.sh" in command:
            return True
    return False


def _replace_mypeople_hooks(hooks, hook):
    """Remove every prior MyPlow hook, including retired events, then install current ones."""
    if not isinstance(hooks, dict):
        hooks = {}
    for event in list(hooks):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            continue
        kept = [group for group in groups if not _mypeople_hook_group(group)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    for event in LIFECYCLE_EVENTS:
        command = "%s %s" % (shlex.quote(hook), event)
        hooks.setdefault(event, []).append({"hooks": [
            {"type": "command", "command": command}
        ]})
    return hooks


def write_claude_config(install):
    """Claude-Code onboarding + folder-trust + the Boss lifecycle hook, pointed at THIS
    install's plugin path. Replaces seed Step 3."""
    # ~/.claude.json — onboarding + trust for install dir and the agent cwds
    cj = os.path.expanduser("~/.claude.json")
    d = {}
    if os.path.exists(cj):
        try:
            d = json.load(open(cj)) or {}
        except Exception:
            d = {}
    if not isinstance(d, dict):
        d = {}
    d["hasCompletedOnboarding"] = True
    proj = d.setdefault("projects", {})
    for path in {os.path.expanduser("~"), install, os.path.join(install, "run"),
                 os.path.join(install, "run", "boss"), os.path.join(install, "run", "eng"),
                 os.path.join(install, "bin")}:
        rec = proj.get(path, {})
        rec["hasTrustDialogAccepted"] = True
        proj[path] = rec
    _atomic_json(cj, d)

    # ~/.claude/settings.json — shared lifecycle hooks + dangerous-mode precondition
    cs_dir = os.path.expanduser("~/.claude")
    os.makedirs(cs_dir, exist_ok=True)
    hook = os.path.join(install, "plugins", "tmux-boss-hooks", "emit-event.sh")
    settings_path = os.path.join(cs_dir, "settings.json")
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path) as f:
                settings = json.load(f) or {}
        except Exception:
            settings = {}
    if not isinstance(settings, dict):
        settings = {}
    settings["hooks"] = _replace_mypeople_hooks(settings.get("hooks", {}), hook)
    settings["skipDangerousModePermissionPrompt"] = True
    _atomic_json(settings_path, settings)


def write_codex_config(install):
    """Install the same lifecycle contract for Codex without touching unrelated hooks."""
    codex_dir = os.path.expanduser("~/.codex")
    os.makedirs(codex_dir, exist_ok=True)
    hooks_path = os.path.join(codex_dir, "hooks.json")
    config = {}
    if os.path.exists(hooks_path):
        try:
            with open(hooks_path) as f:
                config = json.load(f) or {}
        except Exception:
            config = {}
    if not isinstance(config, dict):
        config = {}
    hook = os.path.join(install, "plugins", "tmux-boss-hooks", "emit-event.sh")
    config["hooks"] = _replace_mypeople_hooks(config.get("hooks", {}), hook)
    _atomic_json(hooks_path, config)


def _atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- step 5: tmux.conf
def install_tmux_conf(install):
    """Install MyPlow's tmux settings as an include while preserving the user's config."""
    dst = os.path.expanduser("~/.tmux.conf")
    src = os.path.join(install, "config", "tmux.conf")
    include = os.path.join(CONFIG_DIR, "tmux.runtime.conf")
    if not os.path.exists(src):
        return
    os.makedirs(CONFIG_DIR, exist_ok=True)
    shutil.copy2(src, include)
    directive = "source-file %s" % include
    existing = ""
    if os.path.exists(dst):
        try:
            existing = open(dst).read()
        except Exception:
            existing = ""
    if directive not in existing:
        with open(dst, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n# MyPlow runtime settings\n%s\n" % directive)
    tpm = os.path.expanduser("~/.tmux/plugins/tpm")
    if not os.path.isdir(tpm) and shutil.which("git"):
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/tmux-plugins/tpm", tpm],
                       capture_output=True, timeout=60)
    installer = os.path.join(tpm, "bin", "install_plugins")
    if os.path.exists(installer):
        subprocess.run([installer], capture_output=True, timeout=120)
    if shutil.which("tmux"):
        subprocess.run(["tmux", "source-file", dst], capture_output=True)


# ---------------------------------------------------------------- orchestration
def ensure(preferred_backend=None, allow_unauthenticated=None):
    """Idempotent first-run configuration. Returns (install, host, selected backend).

    `backend` comes back None when this node has no login yet AND exiting is not an option --
    see the login-required branch below.
    """
    install = install_dir()
    materialize(install)
    configured = _read_env_val("DEFAULT_BACKEND")
    requested = (preferred_backend or os.environ.get("MYPEOPLE_BACKEND") or
                 os.environ.get("DEFAULT_BACKEND") or configured)
    if allow_unauthenticated is None:
        # Only PID 1 under a restart policy, or the desktop app. An installer or an interactive
        # `mypeople up` still refuses cleanly and starts nothing -- that behaviour is the
        # reference, not the bug. The desktop app joins the container here because sys.exit(2)
        # in a double-clicked .app is a window that never opens and says nothing: it has no
        # terminal to print to, and the login it is asking for is reachable only from the
        # terminal tab inside the window it just refused to show.
        allow_unauthenticated = (os.environ.get("MYPEOPLE_CONTAINER") == "1" or
                                 os.environ.get("MYPEOPLE_DESKTOP") == "1")
    # Backend config seeding happens BEFORE the auth gate on purpose (card 293fc81898). It writes
    # only local files and needs no login, but it is what pre-accepts Claude's first-run modals
    # (onboarding, folder trust, and the Bypass Permissions warning whose default button is "No,
    # exit"). Below the `sys.exit(2)` it never ran on a fresh install -- so the very first agent
    # spawned after the login landed sat on a modal, ate its prompt and died instead of running
    # with --dangerously-skip-permissions.
    write_claude_config(install)
    write_codex_config(install)
    ok, backend, msg = resolve_auth(requested, chooser=_prompt_backend)
    if not ok:
        _echo("\n[myplow] " + msg + "\n")
        if not allow_unauthenticated:
            sys.exit(2)
        # In a container this process IS the container. Exiting here hands `restart:
        # unless-stopped` a process whose only possible outcome is the same exit code, so the
        # user gets an endless restart loop and dead ports instead of a login prompt (card
        # f0e7101c94). Come up degraded instead: the board and the HUD serve, they say what is
        # missing, and the boss supervisor starts the Boss by itself once the login lands.
        _echo("[myplow] LOGIN REQUIRED — starting board + HUD only; no agent can run yet.")
        backend = None
    else:
        _echo("[myplow] auth: %s" % msg)
    fresh = write_queue_env(install, backend or requested or "claude")
    install_tmux_conf(install)
    write_auth_state(install, ok, backend, msg, requested or "")
    host = os.environ.get("HOST_ID") or _read_env_val("HOST_ID") or socket.gethostname().split(".")[0]
    _echo("[myplow] install dir: %s  (config: %s%s)" %
          (install, CONFIG_PATH, ", fresh" if fresh else ", reused"))
    return install, host, backend


def _read_env_val(key):
    try:
        for line in open(CONFIG_PATH):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith(key + "="):
                v = line.split("=", 1)[1].strip()
                return v[1:-1] if v[:1] in "\"'" else v
    except Exception:
        return None
    return None
