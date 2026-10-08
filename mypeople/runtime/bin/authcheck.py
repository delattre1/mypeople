#!/usr/bin/env python3
"""Is this node's AI login actually able to answer? (card 8a6ebe4e48)

`claude auth status` answers "is there a credential file I can parse", NOT "can this node reach
the model". Proven on a purpose-built dead credential (access AND refresh token expired in 2023,
both values garbage):

    $ claude auth status
    { "loggedIn": true, "authMethod": "claude.ai", "email": null, ... }   # exit 0
    $ claude -p 'reply with PONG'
    Failed to authenticate. API Error: 401 OAuth access token is invalid.

So the gate in firstrun.resolve_auth() reported `this node's claude login is active`, MyPlow
brought the Boss up, the HUD painted ALIVE/WORKING, the board delivered pings -- and the agent
sat at "Not logged in · Please run /login". The product asserted a state that did not exist, and
every surface downstream repeated the lie. That is worse than failing.

This module is the honest answer, in two layers:

  1. STRUCTURAL (free, no network) -- read the credential's own expiry stamps. A refresh token
     that has already expired means the session cannot be refreshed, which is exactly the
     reported failure, and it is knowable without talking to anyone.
  2. PROBE (authoritative) -- actually ask the backend for one word. Catches what timestamps
     cannot: a credential revoked server-side while its window still looks open.

Fail-closed is applied to DEAD only. A probe that times out or hits a broken network returns
UNKNOWN and does NOT downgrade a structurally-open credential -- an offline node must not be told
its login is expired, because that would be the same crime in the other direction.

stdlib only, and NO `import mypeople`: the runtime daemons are detached from the package, so this
file has to be importable both as a sibling of mpcommon.py and by firstrun.py via importlib.
"""
import json
import os
import shutil
import subprocess
import time

LIVE = "live"          # proven able to answer (or credential window open and unprobed)
DEAD = "dead"          # proven unable to answer -- node is NOT authenticated
UNKNOWN = "unknown"    # could not tell; never counted as proof either way

CLAUDE_CREDENTIALS = os.path.join("~", ".claude", ".credentials.json")

# Markers that mean "the backend refused us", as opposed to any other failure. Deliberately
# specific: the probe prompt is a fixed single word, so none of these can come from the answer.
AUTH_FAILURE_MARKERS = (
    "failed to authenticate",
    "oauth access token is invalid",
    "oauth token has expired",
    "oauth session expired",
    "could not be refreshed",
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication_error",
    "invalid_grant",
)

PROBE_PROMPT = "reply with the single word PONG"


def _now_ms():
    return time.time() * 1000.0


def _first_line(text):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _stamp_ms(value):
    """Credential stamps are epoch MILLIseconds; ignore anything that isn't a usable number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def claude_credential_state(path=None, now_ms=None):
    """Structural verdict from the credential file alone -- no network, no subprocess.

    Returns DEAD only when the file itself proves the session cannot be refreshed. Anything we
    cannot read or recognise is UNKNOWN (the credential may live in a keychain or in the env),
    never a silent pass.
    """
    path = os.path.expanduser(path or CLAUDE_CREDENTIALS)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        return UNKNOWN, "cannot read %s (%s)" % (path, exc.__class__.__name__)
    if not isinstance(data, dict):
        return UNKNOWN, "credential file is not an object"
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth:
        return UNKNOWN, "credential file has no claudeAiOauth block"

    now = _now_ms() if now_ms is None else float(now_ms)
    refresh_exp = _stamp_ms(oauth.get("refreshTokenExpiresAt"))
    access_exp = _stamp_ms(oauth.get("expiresAt"))

    if refresh_exp is not None and refresh_exp <= now:
        return DEAD, ("OAuth session expired and cannot be refreshed (refresh token expired %s)"
                      % _ago(now - refresh_exp))
    if access_exp is not None and access_exp <= now and refresh_exp is None:
        # Access token is stale and nothing on disk promises the refresh still works. Not proof
        # of death -- the CLI may hold a refresh path we cannot see -- so hand it to the probe.
        return UNKNOWN, "access token expired and no refresh window recorded"
    return LIVE, "credential window open"


def _ago(delta_ms):
    secs = max(0.0, delta_ms) / 1000.0
    if secs < 3600:
        return "%d min ago" % (secs // 60)
    if secs < 86400:
        return "%d h ago" % (secs // 3600)
    return "%d days ago" % (secs // 86400)


def claude_probe(timeout=90, runner=None):
    """Ask the model for one word. The only signal that cannot be faked by a file on disk."""
    if shutil.which("claude") is None:
        return DEAD, "claude CLI is not on PATH"
    run = runner or subprocess.run
    # Not an agent turn: without AGENT_ID the lifecycle hooks stay silent, so a probe run from an
    # agent's shell (the test suite) never writes that agent's status or pings the Boss "PONG".
    env = {k: v for k, v in os.environ.items() if k != "AGENT_ID"}
    try:
        r = run(["claude", "-p", PROBE_PROMPT], capture_output=True, text=True, timeout=timeout,
                env=env)
    except subprocess.TimeoutExpired:
        return UNKNOWN, "auth probe timed out after %ss" % timeout
    except OSError as exc:
        return UNKNOWN, "auth probe could not run (%s)" % exc
    out = "%s\n%s" % (getattr(r, "stdout", "") or "", getattr(r, "stderr", "") or "")
    low = out.lower()
    if any(m in low for m in AUTH_FAILURE_MARKERS):
        return DEAD, _first_line(out) or "backend refused the credential"
    if getattr(r, "returncode", 1) != 0:
        # A non-auth failure (network, rate limit, crash). Not proof the login is dead.
        return UNKNOWN, "auth probe exited %s: %s" % (r.returncode, _first_line(out))
    return LIVE, "backend answered the probe"


def claude_verdict(probe=True, timeout=90, path=None, now_ms=None, runner=None):
    """Full verdict: structural first (free), then the probe if it is still worth asking.

    A structurally-open credential that fails to probe for a NON-auth reason stays LIVE: we do not
    convert "the network is down" into "your login expired".
    """
    state, detail = claude_credential_state(path=path, now_ms=now_ms)
    if state == DEAD or not probe:
        return state, detail
    pstate, pdetail = claude_probe(timeout=timeout, runner=runner)
    if pstate == DEAD:
        return DEAD, pdetail
    if pstate == LIVE:
        return LIVE, pdetail
    if state == LIVE:
        return LIVE, "%s (probe inconclusive: %s)" % (detail, pdetail)
    return UNKNOWN, pdetail


# ------------------------------------------------------------------ daemon-side cached health
HEALTH_FILE = os.path.join("run", "auth-health.json")


def node_auth_health(install_dir, backend="claude", probe_ttl=900, timeout=90, now=None,
                     verdict=None):
    """Auth health for a long-running daemon, cheap enough to call every heartbeat.

    The structural check runs EVERY call (it is a single file read and it catches the exact
    reported failure). The probe costs a real model round-trip, so it runs at most once per
    `probe_ttl` seconds and its result is cached in <install>/run/auth-health.json.

    Only `claude` is validated here. codex/grok keep the checks they already had -- widening the
    probe to backends whose failure strings are unproven would just move the guessing.
    """
    now = time.time() if now is None else now
    path = os.path.join(install_dir, HEALTH_FILE)
    cached = {}
    try:
        with open(path) as f:
            cached = json.load(f) or {}
    except (OSError, ValueError):
        cached = {}
    if not isinstance(cached, dict):
        cached = {}

    if backend != "claude":
        return {"backend": backend, "state": UNKNOWN, "detail": "not validated for %s" % backend,
                "ts": now, "probed_ts": 0}

    call = verdict or claude_verdict
    fresh_probe = (cached.get("backend") == backend
                   and now - float(cached.get("probed_ts") or 0) < probe_ttl)
    state, detail = call(probe=not fresh_probe, timeout=timeout)

    if state == LIVE and fresh_probe and cached.get("state") == DEAD:
        # Structural check says the window is open but the last real probe was refused (revoked
        # token). Keep the refusal until a probe earns the green back.
        state, detail = DEAD, cached.get("detail") or "backend refused the credential"

    out = {"backend": backend, "state": state, "detail": detail, "ts": now,
           "probed_ts": (float(cached.get("probed_ts") or 0) if fresh_probe else now)}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return out


if __name__ == "__main__":
    st, dt = claude_verdict()
    print(json.dumps({"state": st, "detail": dt}, indent=2))
    raise SystemExit(0 if st == LIVE else 1)
