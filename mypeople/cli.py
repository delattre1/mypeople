"""mypeople CLI — thin wrapper over the existing daemons/supervisor.

  mypeople up [--server|--client|--both] [--backend claude|codex|grok] [--detach]
                                                        bring the stack up (default: both, foreground)
  mypeople            (no verb)                        alias for `up` (what `uvx mypeople` runs)
  mypeople down                                        stop daemons (board/roster kept on disk)
  mypeople status                                      health + agents + roster
  mypeople auth-check [--quiet]                        re-check this node's AI login (exit 0 = ready)
  mypeople verify                                      run the shipped acceptance harness
  mypeople logs [name]                                 tail INSTALL_DIR/logs/*.log
"""
import os, sys, time, json, signal, subprocess, urllib.request
from . import firstrun, __version__

CONFIG_PATH = firstrun.CONFIG_PATH


def load_cfg():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        for line in open(CONFIG_PATH):
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


def child_env(cfg):
    e = dict(os.environ)
    e.update(cfg)  # queue.env values available to daemons
    install = cfg.get("INSTALL_DIR", firstrun.install_dir())
    e["MYPEOPLE_CONFIG_PATH"] = CONFIG_PATH
    e["MYPEOPLE_HOME"] = install
    e["PATH"] = "%s/.local/bin:%s/bin:%s" % (
        os.path.expanduser("~"), install, e.get("PATH", "/usr/bin:/bin"))
    # Authentication stays in the selected CLI's node-local credential store.
    return e


def _get(url, secret=None, timeout=6):
    req = urllib.request.Request(url)
    if secret:
        req.add_header("X-Queue-Secret", secret)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except Exception:
        return 0, None


def wait_health(cfg, seconds=40):
    url = "http://127.0.0.1:%s/health" % cfg.get("HUD_PORT", "9900")
    deadline = time.time() + seconds
    while time.time() < deadline:
        code, body = _get(url)
        if code == 200 and isinstance(body, dict) and body.get("status") == "ok":
            return True
        time.sleep(1)
    return False


def urls(cfg):
    return {
        "board": "http://localhost:%s" % cfg.get("TODO_PORT", "9933"),
        "HUD":   "http://localhost:%s/dashboard" % cfg.get("HUD_PORT", "9900"),
        "term":  "http://localhost:%s" % cfg.get(
            "TTYD_BROWSER_PORT", cfg.get("TTYD_PORT", "7681")),
    }


def print_login_required(cfg):
    print("\n  " + "=" * 66)
    print("  MyPeople is UP but this node has no AI login yet.")
    print("  The board and the HUD below work; no agent can run until you log in.\n")
    for line in firstrun.login_howto():
        print("    %s" % line)
    print("\n  The Boss starts by itself within ~15s of a successful login —")
    print("  you do NOT need to restart the container.")
    print("  " + "=" * 66)


def print_urls(cfg):
    u = urls(cfg)
    print("\n  mypeople is up:")
    print("    board  → %s" % u["board"])
    print("    HUD    → %s" % u["HUD"])
    print("    term   → %s" % u["term"])
    print()


# ---------------------------------------------------------------- up
def cmd_up(args):
    role = "both"
    if "--server" in args:
        role = "server"
    if "--client" in args:
        role = "client"
    detach = "--detach" in args
    foreground = not detach
    backend = None
    if "--backend" in args:
        idx = args.index("--backend")
        if idx + 1 >= len(args):
            print("[mypeople] --backend needs claude, codex or grok", file=sys.stderr)
            return 2
        backend = args[idx + 1]

    install, host, backend = firstrun.ensure(backend)
    cfg = load_cfg()
    env = child_env(cfg)
    bindir = os.path.join(install, "bin")

    if role == "client":
        return _up_client(cfg, env, bindir, foreground)

    # server / both: the existing supervisor starts every daemon + boss-supervisor
    log = os.path.join(install, "logs", "supervise.out")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "ab") as lf:
        supervisor = subprocess.Popen(["bash", os.path.join(bindir, "supervise.sh")],
                                      env=env, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                                      start_new_session=True, cwd=install)
    print("[mypeople] daemons starting (role=%s) ..." % role)
    if not wait_health(cfg):
        print("[mypeople] WARNING: HUD health not ready after 40s; check `mypeople logs`",
              file=sys.stderr)
    if backend is None:
        # Login-required mode: the front doors are up and say so, but there is no backend to
        # spawn an agent against yet. boss-supervisor.sh re-checks and starts the Boss on its
        # own the moment a login lands, so the user never needs a down/up cycle.
        print_login_required(cfg)
    else:
        # Ensure the Boss deterministically. Existing nodes must resume their persisted session;
        # only a node without a Boss roster entry may create the initial session.
        mp = os.path.join(bindir, "mp")
        subprocess.run(["python3", mp, "ensure-boss", "%s/main:Boss" % host,
                        "--backend", backend],
                       env=env, cwd=install)
    print_urls(cfg)
    if foreground:
        if os.environ.get("MYPEOPLE_CONTAINER") == "1":
            try:
                return supervisor.wait()
            except KeyboardInterrupt:
                supervisor.terminate()
                return 130
        _follow(install, cfg)
    return 0


def _up_client(cfg, env, bindir, foreground):
    up = os.environ.get("UPSTREAM_QUEUE_URL") or cfg.get("UPSTREAM_QUEUE_URL")
    if not up:
        print("[mypeople] client mode needs UPSTREAM_QUEUE_URL=http://<server-host>:9900",
              file=sys.stderr)
        return 2
    upstream_secret = os.environ.get("UPSTREAM_QUEUE_SECRET") or cfg.get("UPSTREAM_QUEUE_SECRET")
    if not upstream_secret:
        print("[mypeople] client mode needs UPSTREAM_QUEUE_SECRET", file=sys.stderr)
        return 2
    # queue-client consumes QUEUE_URL/QUEUE_SECRET. Override both for this isolated client process.
    env["UPSTREAM_QUEUE_URL"] = up
    env["QUEUE_URL"] = up
    env["QUEUE_SECRET"] = upstream_secret
    print("[mypeople] client → %s" % up)
    p = subprocess.Popen(["python3", os.path.join(bindir, "queue-client.py")], env=env)
    if foreground:
        try:
            p.wait()
        except KeyboardInterrupt:
            p.terminate()
    return 0


def _follow(install, cfg):
    print("[mypeople] following — Ctrl-C to detach (daemons keep running; `mypeople down` to stop)\n")
    daemon_log = os.path.join(install, "logs", "daemon.log")
    try:
        open(daemon_log, "a").close()
        p = subprocess.Popen(["tail", "-n", "0", "-f", daemon_log])
        p.wait()
    except KeyboardInterrupt:
        try:
            p.terminate()
        except Exception:
            pass
        print("\n[mypeople] detached. `mypeople status` / `mypeople down`.")
    return 0


# ---------------------------------------------------------------- down
def cmd_down(args):
    cfg = load_cfg()
    install = cfg.get("INSTALL_DIR", firstrun.install_dir())
    pidfile = os.path.join(install, "run", "supervise.pid")
    if os.path.exists(pidfile):
        try:
            pid = int(open(pidfile).read().strip())
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        try:
            os.remove(pidfile)
        except OSError:
            pass
    bindir = os.path.join(install, "bin")
    for pat in ("supervise.sh", "queue-server.py", "todo-server.py", "queue-client.py",
                "board-exporter.py", "boss-supervisor.sh"):
        subprocess.run(["pkill", "-f", os.path.join(bindir, pat)], capture_output=True)
    # Writable + read-only ttyd. On Linux, supervise.sh detaches children with setsid so
    # killpg on the supervisor does not reach them. Without both pkill patterns, `down`/`up`
    # upgrades leave the RO graph-tile ttyd running with the old argv (card 789b5d04f9).
    # Empty string is a present key — cfg.get default only applies when MISSING.
    port = cfg.get("TTYD_PORT") or "7681"
    ro = cfg.get("TTYD_RO_PORT") or str(int(port) + 1)
    subprocess.run(["pkill", "-f", "ttyd -W -a -p %s" % port], capture_output=True)
    subprocess.run(["pkill", "-f", "ttyd -a -p %s" % ro], capture_output=True)
    print("[mypeople] stopped (board/roster kept on disk in %s)" % install)
    return 0


# ---------------------------------------------------------------- auth-check
def cmd_auth_check(args):
    """Re-resolve this node's login and republish the auth state file.

    Called on a loop by boss-supervisor.sh so a login performed while the stack is already
    running takes effect without a down/up cycle. Exits 0 when this node can run agents.
    """
    install = firstrun.install_dir()
    state = {}
    try:
        with open(firstrun.auth_state_path(install)) as f:
            state = json.load(f) or {}
    except Exception:
        state = {}
    # Honour an explicit backend choice; with none, ANY completed login unblocks the node --
    # the user must not be forced into claude just because it is the default name in the config.
    requested = (state.get("requested") or "").strip() or None
    ok, backend, msg = firstrun.resolve_auth(requested, chooser=None)
    firstrun.write_auth_state(install, ok, backend if ok else None, msg,
                              requested or "")
    if ok:
        # Persist the backend the login actually landed on, so every later spawn agrees with it.
        if (firstrun._read_env_val("DEFAULT_BACKEND") or "") != backend:
            firstrun.write_queue_env(install, backend)
        if "--quiet" not in args:
            print("[mypeople] auth: %s" % msg)
        return 0
    if "--quiet" not in args:
        print("[mypeople] %s" % msg, file=sys.stderr)
    return 1


# ---------------------------------------------------------------- status
def cmd_status(args):
    cfg = load_cfg()
    if not cfg:
        print("[mypeople] not configured yet — run `mypeople up`")
        return 1
    hud = "http://127.0.0.1:%s" % cfg.get("HUD_PORT", "9900")
    secret = cfg.get("QUEUE_SECRET", "")
    hc, hb = _get(hud + "/health")
    print("health : %s" % (hb.get("status") if isinstance(hb, dict) else "DOWN"))
    ac, agents = _get(hud + "/agents", secret)
    for a in (agents or []):
        print("  %-40s [%s] %s" % (a.get("agent_id"), a.get("state", "?"), a.get("status", "")))
    for k, v in urls(cfg).items():
        print("%-6s : %s" % (k, v))
    return 0 if hc == 200 else 1


# ---------------------------------------------------------------- verify
def cmd_verify(args):
    cfg = load_cfg()
    install = cfg.get("INSTALL_DIR", firstrun.install_dir())
    vs = os.path.join(install, "verify", "verify.sh")
    if not os.path.exists(vs):
        print("[mypeople] verify.sh missing; run `mypeople up` first", file=sys.stderr)
        return 1
    env = child_env(cfg)
    return subprocess.call(["bash", vs], env=env, cwd=install)


# ---------------------------------------------------------------- logs
def cmd_logs(args):
    cfg = load_cfg()
    install = cfg.get("INSTALL_DIR", firstrun.install_dir())
    logdir = os.path.join(install, "logs")
    name = args[0] if args else None
    import glob
    files = ([os.path.join(logdir, name + ".log")] if name
             else sorted(glob.glob(os.path.join(logdir, "*.log")) +
                         glob.glob(os.path.join(logdir, "*.out"))))
    files = [f for f in files if os.path.exists(f)]
    if not files:
        print("[mypeople] no logs in %s" % logdir)
        return 1
    return subprocess.call(["tail", "-n", "40", "-f"] + files)


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("-v", "--version"):
        print("mypeople %s" % __version__)
        return 0
    if not argv or argv[0] == "up":
        return cmd_up(argv[1:] if argv else [])
    verb, rest = argv[0], argv[1:]
    table = {"down": cmd_down, "status": cmd_status, "verify": cmd_verify, "logs": cmd_logs,
             "auth-check": cmd_auth_check}
    fn = table.get(verb)
    if not fn:
        print(__doc__)
        return 2
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
