#!/usr/bin/env python3
"""MyPlow queue-client (INNER plane): heartbeat + re-announce agents + task poll->tmux relay.
Owns the durable roster (run/roster.json) and re-announces live agents every heartbeat so a
server restart / false-prune self-heals within one cycle. Python stdlib only."""
import os, sys, json, time, subprocess, threading, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpcommon as C
import authcheck

CFG = C.CFG
INSTALL_DIR = CFG["INSTALL_DIR"]
HOST_ID = CFG["HOST_ID"]
QUEUE_URL = CFG["QUEUE_URL"]
SECRET = CFG.get("QUEUE_SECRET", "")
TTYD_PORT = CFG["TTYD_PORT"]
INTERVAL = float(CFG["HEARTBEAT_INTERVAL"])
ROSTER_PATH = os.path.join(INSTALL_DIR, "run", "roster.json")
STATUS_DIR = os.path.join(INSTALL_DIR, "status")
HDR = {"X-Queue-Secret": SECRET}


def attach_base():
    # Cross-host attach requires an explicitly browser-reachable LAN/DNS URL. Same-host pages
    # derive their host from window.location and do not need an advertised base.
    base = os.environ.get("TTYD_PUBLIC_URL", CFG.get("TTYD_PUBLIC_URL", ""))
    try:
        host = urllib.parse.urlparse(base).hostname if base else ""
    except Exception:
        host = ""
    if host in ("", "0.0.0.0", "127.0.0.1", "localhost", "::"):
        return ""
    return base.rstrip("/")


def window_alive(sess, tab):
    r = subprocess.run(["tmux", "list-windows", "-t", "mc-%s" % sess, "-F", "#{window_name}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    return tab in r.stdout.split()


def status_summary(aid):
    _, sess, tab = C.parse_agent_id(aid)
    p = os.path.join(STATUS_DIR, "mc-%s" % sess, "%s.json" % tab)
    d = C.read_json(p, None)
    return (d or {}).get("summary", "")


def auth_health():
    """Can this node's credential still answer? (card 8a6ebe4e48)

    A tmux window is proof a process exists, not proof it can reach the model. With a dead OAuth
    token every agent here kept announcing `alive`, the HUD painted WORKING, and none of them
    could answer a single ping. Cheap every heartbeat; the real probe is rate-limited inside
    authcheck. Never fails the heartbeat -- an error here means UNKNOWN, not a fleet blackout."""
    try:
        return authcheck.node_auth_health(INSTALL_DIR, backend=CFG.get("DEFAULT_BACKEND", "claude"))
    except Exception as exc:
        return {"state": authcheck.UNKNOWN, "detail": "auth health check failed: %s" % exc}


def live_agents():
    """Re-announce every live agent from the durable roster. Robust to a session/tab-less roster:
    derive session/tab from the agent_id itself when the roster fields are missing (§3)."""
    roster = C.read_json_tracked(ROSTER_PATH, {}) or {}
    health = auth_health()
    node_unauthenticated = health.get("state") == authcheck.DEAD
    out = []
    changed = False
    for aid, rr in roster.items():
        if rr.get("retired"):
            continue
        h, dsess, dtab = C.parse_agent_id(aid)
        sess = rr.get("session") or dsess
        tab = rr.get("tab") or dtab
        if not (sess and tab):
            continue
        if not window_alive(sess, tab):
            try:
                rr["recorder_stop"] = C.stop_recorder(
                    sess, tab, rr.get("recorder"), HOST_ID)
            except Exception as exc:
                rr["recorder_stop"] = {"error": str(exc), "requested_ts": time.time()}
            rr["retired"] = True
            rr.setdefault("retire_reason", "died-no-window")
            rr.setdefault("retired_ts", time.time())
            changed = True
            continue
        current_recorder = C.current_recorder(sess, tab, HOST_ID)
        if current_recorder and rr.get("recorder", {}).get("pid") != current_recorder["pid"]:
            prior_recorder = rr.get("recorder")
            if prior_recorder:
                C.stop_recorder(sess, tab, prior_recorder, HOST_ID, include_current=False)
            rr["recorder"] = current_recorder
            changed = True
        backend = rr.get("backend", "claude")
        # Only the backend whose credential we actually validated is downgraded; a codex agent on
        # a node with a dead claude token is still alive.
        starved = node_unauthenticated and backend == CFG.get("DEFAULT_BACKEND", "claude")
        out.append({
            "agent_id": aid, "host": HOST_ID, "session": sess, "tab": tab,
            "backend": backend, "state": "unauthenticated" if starved else "alive",
            "auth_detail": health.get("detail", "") if starved else "",
            "boss_id": rr.get("boss_id", ""), "is_master": rr.get("is_master", False),
            "summary": status_summary(aid), "spawn_cmd": rr.get("spawn_cmd", ""),
            "model": rr.get("model", ""), "tmux_target": "mc-%s:%s" % (sess, tab),
        })
    if changed:
        # The heartbeat is the writer that used to clobber `mp kill` and `mp spawn`.
        C.write_json_merged(ROSTER_PATH, roster)
    return out


def heartbeat_loop():
    while True:
        body = {
            "hostname": HOST_ID, "attach_base": attach_base(), "substrate_ready": True,
            "purpose": CFG.get("NODE_PURPOSE", "mypeople"),
            "node_type": CFG.get("NODE_TYPE", "system-agent"),
            "recording_url": CFG.get("NODE_RECORDING_URL", ""),
            "state": "ready", "agents": live_agents(),
        }
        C.http_json("POST", QUEUE_URL + "/heartbeat", body, HDR, timeout=8)
        time.sleep(INTERVAL)


def dispatch(task):
    typ = task.get("type")
    ta = task.get("target_agent", "")
    payload = task.get("payload", {}) or {}
    ok, result = False, ""
    try:
        if typ == "send":
            msg = payload.get("message", "")
            tgt = C.tmux_target(ta)
            route_token = C.enqueue_notification_route(ta, payload.get("reply_to", ""), msg)
            # Notifications to an agent that is restarting wait for it instead of vanishing.
            result = C.send_when_ready(tgt, msg, 20)
            ok = result == "sent"
            if not ok and route_token:
                C.cancel_notification_route(ta, route_token)
        elif typ == "peek":
            out = C.tmux_capture(C.tmux_target(ta))
            ok, result = True, out
        elif typ == "spawn":
            args = ["mp", "spawn", ta]
            if payload.get("backend"):
                args += ["--backend", payload["backend"]]
            if payload.get("boss"):
                args += ["--boss", payload["boss"]]
            if payload.get("cwd"):
                args += ["--cwd", payload["cwd"]]
            if payload.get("is_master"):
                args += ["--master"]
            # A Boss (--master) MUST carry --role boss or do_spawn fail-closes ("refusing a
            # bundle-less Boss", rc=2). The board's spawn-backend buttons submit {is_master:true}
            # with no role, which is why every dashboard Boss-spawn failed and only Revive worked
            # (card f6339b85a2). Default the boss role for any master spawn; an explicit payload
            # role still wins.
            role = payload.get("role") or ("boss" if payload.get("is_master") else "")
            if role:
                args += ["--role", role]
            if payload.get("model"):
                args += ["--model", payload["model"]]
            r = subprocess.run(args, capture_output=True, text=True, timeout=120)
            ok, result = (r.returncode == 0), (r.stdout + r.stderr)[-2000:]
        elif typ == "revive":
            r = subprocess.run(["mp", "revive", ta], capture_output=True, text=True, timeout=120)
            ok, result = (r.returncode == 0), (r.stdout + r.stderr)[-1000:]
        elif typ == "kill":
            args = ["mp", "kill", ta]
            if payload.get("reason"):
                args += ["--reason", payload["reason"]]
            r = subprocess.run(args, capture_output=True, text=True, timeout=60)
            ok, result = (r.returncode == 0), (r.stdout + r.stderr)[-1000:]
        elif typ == "answer":
            r = subprocess.run(["mp", "answer", ta, str(payload.get("n", 1))],
                               capture_output=True, text=True, timeout=30)
            ok, result = (r.returncode == 0), (r.stdout + r.stderr)[-500:]
        else:
            result = "unknown_type:%s" % typ
    except Exception as e:
        result = "error:%s" % e
    return ok, result


def poll_loop():
    while True:
        try:
            code, tasks = C.http_json("GET", QUEUE_URL + "/task/poll?hostname=%s" % HOST_ID,
                                      None, HDR, timeout=8)
            if code == 200 and isinstance(tasks, list):
                for t in tasks:
                    ok, result = dispatch(t)
                    C.http_json("POST", QUEUE_URL + "/task/result",
                                {"task_id": t["task_id"], "ok": ok, "result": result}, HDR, timeout=8)
        except Exception:
            pass
        time.sleep(1.5)


def main():
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    poll_loop()


if __name__ == "__main__":
    main()
