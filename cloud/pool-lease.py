#!/usr/bin/env python3
"""This MyPlow's Claude login, leased from its owner's login pool (the Auth Bank's /checkout).

    pool-lease.py checkout   print a login on stdout (exit 0), lease kept in state/claude-login/lease.json;
                             exit 3: the login server has no pool (use /token), 4: every login is in use,
                             5: not the owner's agent, 6: anything else
    pool-lease.py bad        the leased login does not work: tell the pool, print the replacement (exit 0)
    pool-lease.py loop       hold the lease: heartbeat every minute with the usage this VM's Claude saw,
                             and move to another login when this one hits a usage limit (or is disabled)

Claude reads its login once, at start, so moving to another login is an in-place restart: the new
login goes into tmux's environment, then every Claude agent is restarted on its SAME session
(`mp kill` + `mp revive`, which resumes it), the Boss first. The old login goes back to the pool only
once the Boss is back; if it does not come back, this VM stays on the old login and gives the new
one back. The VM, its data and the chat bridge stay up throughout.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("MYPEOPLE_HOME", "/var/lib/mypeople"))
LEASE = HOME / "state" / "claude-login" / "lease.json"
LOG = HOME / "logs" / "pool-lease.log"
MP = HOME / "bin" / "mp"
ROSTER = HOME / "run" / "roster.json"
PROJECTS = Path.home() / ".claude" / "projects"
EVERY = int(os.environ.get("MYPLOW_POOL_EVERY", "60"))
RETRY_AFTER = int(os.environ.get("MYPLOW_POOL_RETRY_AFTER", "600"))   # after a failed move, before the next
# What a usage limit looks like in Claude Code's own session log (an API error turn).
LIMIT = re.compile(r"usage limit|limit reached|rate_limit_error|API Error: 429", re.I)
USAGE = {"input_tokens": "input", "output_tokens": "output",
         "cache_read_input_tokens": "cache_read", "cache_creation_input_tokens": "cache_write"}


def bank():
    """The owner's login server: this install's own setting, as in claude-login.sh. No default."""
    if os.environ.get("MYPLOW_CLAUDE_BANK"):
        return os.environ["MYPLOW_CLAUDE_BANK"]
    try:
        return (HOME / "state" / "claude-bank-url").read_text().strip()
    except OSError:
        return ""


def log(msg, beacon=False):
    line = time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + msg
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if beacon and bank():
        try:
            urllib.request.urlopen(urllib.request.Request(bank() + "/beacon", method="POST",
                                   data=("%s pool: %s" % (os.uname().nodename, msg)).encode()), timeout=5)
        except Exception:
            pass


def assertion():
    req = urllib.request.Request(os.environ.get("PLOW_API_BASE", "https://api.plow.co") + "/v1/auth/index-identity",
                                 headers={"Authorization": "Bearer " + os.environ.get("PLOW_AGENT_TOKEN", "proxied")})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["assertion"]


def call(path, body):
    """POST to the login server with a fresh Plow assertion: (status, json)."""
    body = dict(body, holder=os.uname().nodename)
    try:
        a = assertion()
    except Exception:
        return 0, {}
    req = urllib.request.Request(bank() + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Plow-Index-Assertion": a})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}
    except Exception:
        return 0, {}


def read_lease():
    try:
        return json.loads(LEASE.read_text())
    except (OSError, ValueError):
        return {}


def write_lease(d):
    LEASE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEASE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)                       # ids and account name only: the login itself is never on disk
    os.replace(tmp, LEASE)


def seen_since(started, projects=PROJECTS):
    """(usage, limit_hit) from this VM's Claude session logs since `started` (epoch): tokens summed once
    per message id (a streamed message is logged more than once), and whether any turn hit a limit."""
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started))
    usage, seen, limit = {v: 0 for v in USAGE.values()}, set(), False
    for f in Path(projects).glob("*/*.jsonl"):
        try:
            if f.stat().st_mtime < started:
                continue
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant" or str(d.get("timestamp", ""))[:19] < since:
                continue
            m = d.get("message") or {}
            if d.get("isApiErrorMessage") and LIMIT.search(json.dumps(m.get("content", ""))):
                limit = True
            mid = m.get("id") or d.get("uuid")
            if mid in seen:
                continue
            seen.add(mid)
            for k, v in USAGE.items():
                usage[v] += int((m.get("usage") or {}).get(k) or 0)
    return usage, limit


def checkout():
    code, out = call("/checkout", {})
    if code == 200 and out.get("token"):
        write_lease({"lease_id": out["lease_id"], "account": out["account"], "started": time.time()})
        log("checked out %s" % out["account"], beacon=True)
        sys.stdout.write(out["token"])
        return 0
    return {404: 3, 405: 3, 409: 4, 401: 5}.get(code, 6)


def bad():
    """The leased login does not work (expired, or revoked by hand): disable it in the pool, take another."""
    lease = read_lease()
    code, out = call("/heartbeat", {"lease_id": lease.get("lease_id"), "bad_login": True})
    rot = out.get("rotate") if code == 200 else None
    if not rot:
        return 4
    call("/return", {"lease_id": lease.get("lease_id")})
    write_lease({"lease_id": rot["lease_id"], "account": rot["account"], "started": time.time()})
    log("login %s did not work; moved to %s" % (lease.get("account"), rot["account"]), beacon=True)
    sys.stdout.write(rot["token"])
    return 0


def claude_agents():
    """Live Claude agents, the Boss first (a Boss that resumes is the proof the new login works)."""
    try:
        roster = json.loads(ROSTER.read_text())
    except (OSError, ValueError):
        return []
    live = [(aid, r) for aid, r in roster.items()
            if not r.get("retired") and (r.get("backend") or "claude") == "claude"]
    return [aid for aid, r in sorted(live, key=lambda x: not x[1].get("is_master"))]


def set_login(token):
    """Put the login in tmux's global environment, which every revived agent inherits. Through a
    600 file, never argv (a process list would show it)."""
    fd, path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w") as f:
            f.write('set-environment -g CLAUDE_CODE_OAUTH_TOKEN "%s"\n' % token)
        return subprocess.run(["tmux", "source-file", path], capture_output=True).returncode == 0
    finally:
        os.remove(path)


def claude_pids():
    return set(subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "claude"], capture_output=True,
                              text=True).stdout.split())


def boss_on(token, session_id, before, wait=180):
    """True once a NEW claude process (not one of `before`) runs on `token`, resumed on `session_id`.
    A process from before the restart never counts: it would pass when old and new are one login."""
    want = hashlib.sha256(token.encode()).hexdigest()
    end = time.time() + wait
    while time.time() < end:
        for pid in claude_pids() - before:
            try:
                cmd = Path("/proc/%s/cmdline" % pid).read_bytes().split(b"\0")
                env = dict(l.split(b"=", 1) for l in Path("/proc/%s/environ" % pid).read_bytes().split(b"\0") if b"=" in l)
            except OSError:
                continue
            if session_id.encode() in cmd and hashlib.sha256(env.get(b"CLAUDE_CODE_OAUTH_TOKEN", b"")).hexdigest() == want:
                return True
        time.sleep(3)
    return False


def restart_all(agents, token):
    """Restart each agent on its same session. The login rides in the mp calls' own environment too:
    killing the last window ends the tmux server, and the server mp then starts takes ITS environment
    from the caller, not from the old server's global one (that lost the new login on the rig)."""
    env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=token)
    for aid in agents:
        subprocess.run([str(MP), "kill", aid, "--reason", "moving to another Claude login"], capture_output=True, env=env)
        try:
            subprocess.run([str(MP), "revive", aid], capture_output=True, timeout=180, env=env)
        except subprocess.TimeoutExpired:
            log("revive of %s timed out" % aid)


def rotate(lease, rot, old_token):
    roster = json.loads(ROSTER.read_text()) if ROSTER.exists() else {}
    agents = claude_agents()
    boss = next((a for a in agents if roster.get(a, {}).get("is_master")), None)
    sid = roster.get(boss, {}).get("session_id", "") if boss else ""
    write_lease(dict(lease, pending=rot["lease_id"], pending_account=rot["account"]))
    log("moving %d agent(s) from %s to %s" % (len(agents), lease["account"], rot["account"]), beacon=True)
    before = claude_pids()
    if set_login(rot["token"]):
        restart_all(agents, rot["token"])
        if not boss or (sid and boss_on(rot["token"], sid, before)):
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = rot["token"]
            call("/return", {"lease_id": lease["lease_id"]})
            write_lease({"lease_id": rot["lease_id"], "account": rot["account"], "started": time.time()})
            log("on %s now; %s given back" % (rot["account"], lease["account"]), beacon=True)
            return rot["token"]
    # The Boss did not come back on the new login: stay on the old one, give the new one back.
    set_login(old_token)
    restart_all(agents, old_token)
    call("/return", {"lease_id": rot["lease_id"]})
    write_lease(dict(lease, pending=None, retry_at=time.time() + RETRY_AFTER))
    log("move to %s FAILED; staying on %s" % (rot["account"], lease["account"]), beacon=True)
    return old_token


def loop():
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    while True:
        time.sleep(EVERY)
        lease = read_lease()
        if not lease.get("lease_id"):
            continue
        usage, limit = seen_since(lease.get("started", time.time()))
        code, out = call("/heartbeat", {"lease_id": lease["lease_id"], "usage": usage, "limit_hit": limit})
        if code == 410:
            log("lease gone: %s may be lent to someone else now" % lease.get("account"), beacon=True)
        elif out.get("rotate") and time.time() < lease.get("retry_at", 0):
            call("/return", {"lease_id": out["rotate"]["lease_id"]})   # backing off: don't sit on a reservation
        elif out.get("rotate"):
            token = rotate(lease, out["rotate"], token)
        elif out.get("no_capacity"):
            log("%s must move but no other login is free" % lease.get("account"), beacon=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if not bank():
        sys.exit(3)
    if cmd == "checkout":
        sys.exit(checkout())
    if cmd == "bad":
        sys.exit(bad())
    if cmd == "loop":
        loop()
    sys.exit(__doc__)
