"""First-run entrypoint — REPLACES seed hydration with configuration only (never code-gen).

ensure() is idempotent: safe on every `up` / container restart. It materializes a writable
INSTALL_DIR from the packaged runtime, resolves the selected backend's auth, writes the single
config file (~/.config/mypeople/queue.env, fresh QUEUE_SECRET per install), wires Claude/Codex
lifecycle hooks, and installs the functional tmux.conf. Starting daemons + spawning the Boss is
the CLI's job (see cli.up)."""
import os, sys, json, shutil, secrets, socket, stat, subprocess, shlex

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


def materialize(install):
    """Copy the packaged runtime into a WRITABLE INSTALL_DIR. Idempotent: never overwrite
    existing daemon code differently, and NEVER clobber live state (board/roster/logs)."""
    rt = runtime_dir()
    os.makedirs(install, exist_ok=True)
    # code trees: refresh from the package so upgrades take effect (state dirs excluded below).
    # "roles" carries the versioned role store (personalities + Skills + profiles). mp resolves
    # every --role spawn against INSTALL_DIR/roles and fails closed if it is absent, so omitting
    # it here would ship a runtime whose Boss cannot be born.
    for sub in ("bin", "plugins", "plans", "verify", "config", "roles"):
        src = os.path.join(rt, sub)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(install, sub), dirs_exist_ok=True,
                            copy_function=_replace_file)
    # Boss doctrine file
    bc = os.path.join(rt, "boss-CLAUDE.md")
    if os.path.exists(bc):
        _replace_file(bc, os.path.join(install, "boss-CLAUDE.md"))
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
def _claude_authenticated():
    if shutil.which("claude"):
        try:
            r = subprocess.run(["claude", "auth", "status"], capture_output=True,
                               text=True, timeout=15)
            logged_in = False
            try:
                logged_in = bool(json.loads(r.stdout).get("loggedIn"))
            except Exception:
                normalized = "".join(ch for ch in (r.stdout + r.stderr).lower() if ch.isalnum())
                logged_in = "loggedintrue" in normalized or "loginmethod" in normalized
            if r.returncode == 0 and logged_in:
                return True
        except Exception:
            pass
    return False


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


def resolve_auth(preferred=None):
    """Require this node's own completed login for the selected agent backend."""
    preferred = (preferred or "").strip().lower()
    if preferred and preferred not in VALID_BACKENDS:
        return False, preferred, "Unknown backend %r; choose claude, codex or grok." % preferred
    checks = {"claude": _claude_authenticated, "codex": _codex_authenticated,
              "grok": _grok_authenticated}
    order = [preferred] if preferred else list(VALID_BACKENDS)
    for backend in order:
        if checks[backend]():
            return True, backend, "this node's %s login is active" % backend
    requested = preferred or "claude, codex or grok"
    return (False, preferred or "none",
            "This node is not authenticated for %s. Run `claude auth login`, `codex login` or "
            "`grok login` inside THIS node, then re-run MyPeople. Never copy or mount credentials "
            "from another node." % requested)


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
    lines = ["# mypeople runtime config — generated on first run (secrets here; never commit)"]
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
    """Remove every prior MyPeople hook, including retired events, then install current ones."""
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
    """Install MyPeople's tmux settings as an include while preserving the user's config."""
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
            f.write("\n# MyPeople runtime settings\n%s\n" % directive)
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
def ensure(preferred_backend=None):
    """Idempotent first-run configuration. Returns (install, host, selected backend)."""
    install = install_dir()
    materialize(install)
    configured = _read_env_val("DEFAULT_BACKEND")
    requested = (preferred_backend or os.environ.get("MYPEOPLE_BACKEND") or
                 os.environ.get("DEFAULT_BACKEND") or configured)
    ok, backend, msg = resolve_auth(requested)
    if not ok:
        _echo("\n[mypeople] " + msg + "\n")
        sys.exit(2)
    _echo("[mypeople] auth: %s" % msg)
    fresh = write_queue_env(install, backend)
    write_claude_config(install)
    write_codex_config(install)
    install_tmux_conf(install)
    host = os.environ.get("HOST_ID") or _read_env_val("HOST_ID") or socket.gethostname().split(".")[0]
    _echo("[mypeople] install dir: %s  (config: %s%s)" %
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
