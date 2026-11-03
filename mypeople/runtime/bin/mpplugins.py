"""Plugins: what is here, what state each one is in, and the switch (mp plugins, the board's Plugins page).

A plugin is a folder in plugins/ (ships with the release) or plugins.d/ (installed here; a release deletes any
folder in plugins/ it does not ship). Its plugin.conf:
  desc=one line for the list
  run=INTERPRETER SCRIPT      supervise.sh runs `INTERPRETER <folder>/SCRIPT serve`
  needs=env:KEY | file:PATH   optional: what it must have to run (KEY set in queue.env, PATH under the install)
  switch=yes                  optional: `SCRIPT off` and `SCRIPT on` are its own switch, and it keeps running while off
  message=yes                 optional: `SCRIPT message` prints what it wraps a message in, for the board's Plugins page

state/<name>/off exists = off: supervise.sh won't start it. Setup stays in each plugin's header, so nothing here
writes queue.env or shows a value from it, only whether a key is set.
"""
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time

import mpcommon as C

INSTALL = C.CFG["INSTALL_DIR"]
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
SCRIPT = re.compile(r"^[A-Za-z0-9_.-]+$")


class PluginError(Exception):
    pass


def _conf(path):
    conf = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep:
                conf[k] = v
    return conf


def _plugins():
    found = {}
    for sub in ("plugins", "plugins.d"):
        root = os.path.join(INSTALL, sub)
        for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            path = os.path.join(root, name, "plugin.conf")
            if name.startswith(".") or not os.path.isfile(path):
                continue
            conf = _conf(path)
            if " " in conf.get("run", ""):
                found.setdefault(name, {"conf": conf, "dir": os.path.join(root, name), "installed": sub == "plugins.d"})
    return found


def _get(name):
    p = _plugins().get(name)
    if not p:
        raise PluginError("no plugin named %s" % name)
    return p


def _set_up(conf):
    needs = conf.get("needs", "")
    if needs.startswith("file:"):
        return os.path.isfile(os.path.join(INSTALL, needs[5:]))
    if needs.startswith("env:"):
        return bool(os.environ.get(needs[4:]) or C.read_config_file().get(needs[4:]))
    return not needs


def _off_file(name):
    return os.path.join(INSTALL, "state", name, "off")


def _mark_off(name):
    os.makedirs(os.path.dirname(_off_file(name)), exist_ok=True)
    open(_off_file(name), "a").close()


def _script(p):
    return os.path.join(p["dir"], p["conf"]["run"].rsplit(" ", 1)[1])


def listing():
    """One row per plugin. Holds names and states only: a plugin's own `status` can print its config."""
    ps = subprocess.run(["ps", "-eo", "command="], capture_output=True, text=True).stdout.splitlines()
    rows = []
    for name, p in _plugins().items():
        serve = _script(p) + " serve"
        # A labelled second bridge (`serve whatsapp`) is started from elsewhere, so it is shown and never switched.
        also = sorted({m.group(1) for m in (re.search(re.escape(serve) + r" ([\w-]+)$", line) for line in ps) if m})
        if not _set_up(p["conf"]):
            state = "not set up"
        elif os.path.exists(_off_file(name)):
            state = "off"
        else:
            state = "running" if any(line.endswith(serve) for line in ps) else "down"
        rows.append({"name": name, "state": state, "desc": p["conf"].get("desc", ""),
                     "installed": p["installed"], "also": also})
    return rows


def _run(p, verb, timeout=60):
    interp = shlex.split(p["conf"]["run"].rsplit(" ", 1)[0])
    # supervise.sh's daemons get these from queue.env; a terminal's shell doesn't have them, and without HOST_ID
    # a plugin falls back to the Mac's hostname and addresses its note to a Boss that doesn't exist.
    env = dict(os.environ, HOST_ID=C.CFG["HOST_ID"], INSTALL_DIR=INSTALL)
    return subprocess.run(interp + [_script(p), verb], capture_output=True, text=True, timeout=timeout, env=env)


def _own_switch(p, verb):
    r = _run(p, verb)
    if r.returncode:
        raise PluginError("%s %s failed: %s" % (_script(p), verb, r.stderr.strip()[-300:]))


def messages():
    """{plugin: the text it wraps a message in}, printed by the plugin's own code so this can't drift from what the
    agent receives. It starts a process per plugin, so the page asks once per open, not on its 5 s poll."""
    out = {}
    for name, p in _plugins().items():
        if p["conf"].get("message") == "yes":
            try:
                r = _run(p, "message", 20)
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode == 0 and r.stdout.strip():
                out[name] = r.stdout.strip()
    return out


def _stop(p):
    # Same anchored pattern as supervise.sh's ensure: a one-shot `reply` or a labelled bridge is not this daemon.
    pattern = _script(p) + " serve$"
    subprocess.run(["pkill", "-f", pattern], capture_output=True)
    for _ in range(50):
        if subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode:
            return
        time.sleep(0.1)
    subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True)


def set_on(name, on):
    p = _get(name)
    set_up = _set_up(p["conf"])
    if on and not set_up:
        raise PluginError("%s is not set up: the top of %s says how" % (name, _script(p)))
    if os.path.exists(_off_file(name)) != on:
        return  # already there: no second note to the Boss, nothing to stop
    own = p["conf"].get("switch") == "yes" and set_up
    if on:
        if own:
            _own_switch(p, "on")
        if os.path.exists(_off_file(name)):
            os.unlink(_off_file(name))
    else:
        _mark_off(name)  # first, so supervise can't restart what is being stopped
        if own:
            _own_switch(p, "off")
        else:
            _stop(p)


def install(src):
    """Copy a plugin into plugins.d, off. Nothing runs until `on`: a plugin runs with the owner's full access."""
    plugins_d = os.path.join(INSTALL, "plugins.d")
    os.makedirs(plugins_d, exist_ok=True)
    name = re.sub(r"\.git$", "", os.path.basename(src.rstrip("/")))
    if not NAME.match(name):
        raise PluginError("bad plugin name: %s" % name)
    if name in _plugins() or os.path.exists(os.path.join(INSTALL, "plugins", name)) \
            or os.path.exists(os.path.join(plugins_d, name)):
        raise PluginError("%s already exists" % name)
    tmp = tempfile.mkdtemp(prefix=".new-", dir=plugins_d)  # hidden: supervise.sh's glob and _plugins skip it
    try:
        new = os.path.join(tmp, name)
        if os.path.isdir(src):
            shutil.copytree(src, new, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        else:
            r = subprocess.run(["git", "clone", "--depth", "1", "--", src, new],
                               capture_output=True, text=True, timeout=300)
            if r.returncode:
                raise PluginError("git clone failed: %s" % r.stderr.strip()[-300:])
            shutil.rmtree(os.path.join(new, ".git"), ignore_errors=True)
        path = os.path.join(new, "plugin.conf")
        if not os.path.isfile(path):
            raise PluginError("no plugin.conf in %s" % src)
        conf = _conf(path)
        script = conf.get("run", "").rsplit(" ", 1)[-1]
        if " " not in conf.get("run", "") or not SCRIPT.match(script) or not os.path.isfile(os.path.join(new, script)):
            raise PluginError("plugin.conf needs run=INTERPRETER SCRIPT, with SCRIPT in the plugin's folder")
        _mark_off(name)  # before it appears, so supervise never sees it switched on
        os.rename(new, os.path.join(plugins_d, name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return name, conf["run"]


def remove(name):
    """Installed plugins only; state/<name> stays, it may hold a login."""
    p = _get(name)
    if not p["installed"]:
        raise PluginError("%s ships with MyPlow: use mp plugins off %s" % (name, name))
    _mark_off(name)
    _stop(p)
    shutil.rmtree(p["dir"])
