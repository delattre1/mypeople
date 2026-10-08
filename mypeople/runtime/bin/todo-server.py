#!/usr/bin/env python3
"""MyPlow todo-server (:9933): board API + board->Boss ping.
Serves todos.html at / and /todos, and reverse-proxies the HUD routes so the
cross-nav works from either front door. Python 3 stdlib only.
MODULE-LEVEL imports of every stdlib a handler uses (§5.3b UnboundLocalError guard)."""
import os, sys, json, time, uuid, threading, shutil, subprocess, glob, re
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpcommon as C
import boardstore as BS

CFG = C.CFG
INSTALL_DIR = CFG["INSTALL_DIR"]
HOST_ID = CFG["HOST_ID"]
HUD_PORT = int(CFG["HUD_PORT"])
TODO_PORT = int(CFG["TODO_PORT"])
SECRET = CFG.get("QUEUE_SECRET", "")
BOSS_AGENT = "%s/main:Boss" % HOST_ID
TODOS_DIR = os.path.join(INSTALL_DIR, "todos")
BOARD_PATH = os.path.join(TODOS_DIR, "board.v2.json")
PROOFS_DIR = os.path.join(TODOS_DIR, "proofs")
INBOX_LOG = os.path.join(TODOS_DIR, "boss-inbox.log")
HTML_DIR = os.path.dirname(os.path.abspath(__file__))
TODOS_HTML = os.path.join(HTML_DIR, "todos.html")
TERMINAL_GRAPH_HTML = os.path.join(HTML_DIR, "terminal-graph.html")
FONT_TYPES = {".woff2": "font/woff2", ".css": "text/css; charset=utf-8"}
STATUS_DIR = os.path.join(INSTALL_DIR, "status")

VALID_STATES = {"working", "review", "done", "blocked", "cancelled", "recurring"}
# A card in a terminal state has no work left, so it must have no living owner.
TERMINAL_STATES = {"done", "cancelled"}
FULL_AGENT_ID = re.compile(r"^[^/\s]+/[^/:\s]+:[^/:\s]+$")
# Engines offerable for a Boss spawn (card 0cc0bde980). Mirrors mp's VALID_BACKENDS; `mp` remains
# the authority and rejects anything it does not support, so this is only a UX guard.
BOSS_BACKENDS = ("claude", "codex", "grok")
LOCK = threading.RLock()
START = time.time()


# ---------------- watchdog deferred-job facility ----------------
def _wcfg(k, d):
    v = os.environ.get(k)
    if v is None:
        v = CFG.get(k)
    return v if (v is not None and v != "") else d

WATCHDOG_AGENT = _wcfg("WATCHDOG_AGENT", "%s/watchdog:Watchdog" % HOST_ID)
WATCHDOG_NUDGE_DELAY_MIN = float(_wcfg("WATCHDOG_NUDGE_DELAY_MIN", 3))
WATCHDOG_TASKCREATE_DELAY_MIN = float(_wcfg("WATCHDOG_TASKCREATE_DELAY_MIN", 10))
WATCHDOG_MAX_LOAD = float(_wcfg("WATCHDOG_MAX_LOAD", 0))   # 0 = disabled
WATCHDOG_POLL_SEC = float(_wcfg("WATCHDOG_POLL_SEC", 5))
# A failed mp_send is retried with linear backoff (60s, 120s, ... 600s => ~55min horizon).
# Past that the job is dropped with an `abandoned` log line: a nudge older than ~1h is noise,
# but the loss is recorded rather than silent.
WATCHDOG_RETRY_BACKOFF_SEC = float(_wcfg("WATCHDOG_RETRY_BACKOFF_SEC", 60))
WATCHDOG_MAX_ATTEMPTS = int(float(_wcfg("WATCHDOG_MAX_ATTEMPTS", 10)))
JOBS_PATH = os.path.join(TODOS_DIR, "watchdog-jobs.json")
JOBS_LOG = os.path.join(TODOS_DIR, "watchdog-jobs.log")
WD_PAUSE = os.path.join(TODOS_DIR, "watchdog.PAUSE")


def wd_new_store():
    return {"version": 1, "jobs": []}


def wd_log(event, job, extra=""):
    try:
        line = "%s %s card=%s kind=%s %s\n" % (time.strftime("%FT%TZ", time.gmtime()),
                                                event, job.get("card"), job.get("kind"), extra)
        with open(JOBS_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def wd_load_store():
    try:
        with open(JOBS_PATH, "r", encoding="utf-8") as fh:
            st = json.load(fh)
        if not isinstance(st, dict) or not isinstance(st.get("jobs"), list):
            return wd_new_store()
        good = []
        for j in st["jobs"]:
            if (isinstance(j, dict) and j.get("card")
                    and isinstance(j.get("fire_at"), (int, float))
                    and j.get("kind") in ("unanswered", "taskcreate")):
                good.append(j)
        st["jobs"] = good
        return st
    except FileNotFoundError:
        return wd_new_store()
    except Exception:
        return wd_new_store()


def wd_save_store(store):
    os.makedirs(TODOS_DIR, exist_ok=True)
    C.atomic_write(JOBS_PATH, json.dumps(store, ensure_ascii=False).encode())


def _wd_is_ceo_or_watchdog(by):
    return by == "CEO" or by == WATCHDOG_AGENT


def wd_schedule_unanswered(tid, comment_id, by):
    """Caller holds LOCK. CEO/Watchdog comment => schedule, superseding any pending job on this card."""
    if not _wd_is_ceo_or_watchdog(by):
        return
    store = wd_load_store()
    store["jobs"] = [j for j in store["jobs"] if not (j["card"] == tid and j["kind"] == "unanswered")]
    job = {"id": uuid.uuid4().hex[:8], "card": tid, "comment_id": comment_id, "by": by,
           "fire_at": now() + WATCHDOG_NUDGE_DELAY_MIN * 60, "kind": "unanswered", "init_state": None}
    store["jobs"].append(job)
    wd_save_store(store)
    wd_log("scheduled", job, "by=%s" % by)


def wd_schedule_taskcreate(tid, init_state):
    """Caller holds LOCK. Any new task => schedule an owner-hasn't-spoken gate."""
    store = wd_load_store()
    store["jobs"] = [j for j in store["jobs"] if not (j["card"] == tid and j["kind"] == "taskcreate")]
    job = {"id": uuid.uuid4().hex[:8], "card": tid, "comment_id": None, "by": "CEO",
           "fire_at": now() + WATCHDOG_TASKCREATE_DELAY_MIN * 60, "kind": "taskcreate", "init_state": init_state}
    store["jobs"].append(job)
    wd_save_store(store)
    wd_log("scheduled", job, "taskcreate")


def wd_gate_holds(task, job):
    if task is None:
        return False
    comments = task.get("comments") or []
    if job["kind"] == "unanswered":
        if not comments:
            return False
        return _wd_is_ceo_or_watchdog(comments[-1].get("by"))
    # taskcreate: fire when the card's OWNER still hasn't said the first word. Covers both
    # "nobody picked it up" (no assignee) and "assigned but silent" -- the latter is the real
    # failure and a comments==0 gate never catches it, because Boss triages in seconds.
    # Terminal cards are exempt: a done/cancelled card needs no nudge.
    if task.get("state") in TERMINAL_STATES:
        return False
    assignee = task.get("assignee") or ""
    if not assignee:
        return True
    return not any(c.get("by") == assignee for c in comments)


def wd_incident_text(job, task):
    if job["kind"] == "unanswered":
        quoted = ""
        for c in reversed(task.get("comments") or []):
            if c.get("id") == job.get("comment_id"):
                quoted = c.get("body", ""); break
        if not quoted and task.get("comments"):
            quoted = task["comments"][-1].get("body", "")
        return "[watchdog incident] card=%s unanswered by=%s: %s" % (job["card"], job["by"], quoted)
    # Two distinct failures reach here. The "new task unowned" wording is verbatim on purpose:
    # the Watchdog persona keys its nudge off that exact phrase.
    owner = task.get("assignee") or ""
    if owner:
        return ("[watchdog incident] card=%s owner assigned but silent: %s has owned this card and has "
                "not posted a first message. Tell them to REPLY ON THIS CARD now: %s"
                % (job["card"], owner, (task.get("text", "") or "")))
    return "[watchdog incident] card=%s new task unowned: %s" % (job["card"], (task.get("text", "") or ""))


def wd_resolve_due(store, board, now_ts):
    """Remove ALL due jobs (fired or cancelled) BEFORE dispatch => fire-once + idempotent under concurrent scans."""
    fire, remaining = [], []
    for j in store["jobs"]:
        if j["fire_at"] > now_ts:
            remaining.append(j)
            continue
        task = (board.get("tasks") or {}).get(j["card"])
        if wd_gate_holds(task, j):
            fire.append(j)
        else:
            wd_log("cancelled", j, "gate-failed")
    store["jobs"] = remaining
    return fire


def watchdog_worker():
    sys.stderr.write("watchdog-worker started (delay=%smin poll=%ss agent=%s)\n"
                     % (WATCHDOG_NUDGE_DELAY_MIN, WATCHDOG_POLL_SEC, WATCHDOG_AGENT))
    while True:
        try:
            time.sleep(WATCHDOG_POLL_SEC)
            if os.path.exists(WD_PAUSE):
                continue
            if WATCHDOG_MAX_LOAD:
                try:
                    if os.getloadavg()[0] > WATCHDOG_MAX_LOAD:
                        continue
                except Exception:
                    pass
            # cheap pre-check WITHOUT the board or the lock: is anything actually due?
            store = wd_load_store()
            if not any(j["fire_at"] <= now() for j in store["jobs"]):
                continue
            board = load_board()   # heavy board read done OUTSIDE the LOCK, and only when due
            with LOCK:             # hold the LOCK only for the tiny store mutation (never the board read)
                store = wd_load_store()
                fire = wd_resolve_due(store, board, now())
                wd_save_store(store)
            requeue = []
            for j in fire:
                task = (board.get("tasks") or {}).get(j["card"])
                if task is None:
                    continue
                rc = mp_send(WATCHDOG_AGENT, wd_incident_text(j, task))
                attempts = j.get("attempts", 0) + 1
                j["attempts"] = attempts
                if rc == 0:
                    wd_log("fired", j, "mp_send_rc=0 attempts=%s" % attempts)
                    continue
                # Jobs are popped from the store BEFORE dispatch (fire-once), so a failed send would
                # die right here, silently. Re-arm it with linear backoff instead; the gate is
                # re-checked on the next due pass, so a nudge that became stale cancels itself.
                if attempts >= WATCHDOG_MAX_ATTEMPTS:
                    wd_log("abandoned", j, "mp_send_rc=%s attempts=%s" % (rc, attempts))
                    continue
                delay = WATCHDOG_RETRY_BACKOFF_SEC * attempts
                j["fire_at"] = now() + delay
                requeue.append(j)
                wd_log("retry", j, "mp_send_rc=%s attempts=%s next_in=%ss" % (rc, attempts, int(delay)))
            if requeue:
                with LOCK:
                    store = wd_load_store()
                    store["jobs"].extend(requeue)
                    wd_save_store(store)
        except Exception as e:
            sys.stderr.write("watchdog_worker error: %r\n" % e)


# ---------------- board store ----------------
BOARD_BACKEND = BS.select_backend(BOARD_PATH, C.CFG.get("BOARD_BACKEND"))


def _sqlite_path():
    return BS.db_path_for(BOARD_PATH)


def default_board():
    return {"version": 2, "order": [], "pinSeq": 0, "tasks": {}}


def board_exists():
    """Is there already a board in the ACTIVE backend?

    Must be asked of the backend, never by statting BOARD_PATH: under sqlite the JSON file never
    exists, so a plain os.path.exists() call reads every boot as a fresh install and seeds an empty
    board OVER the live one. The shrink guard hid this for busy boards (>5 cards) and let it through
    for small ones -- i.e. it deleted the board of every NEW user and nobody else's.
    """
    if BOARD_BACKEND == "sqlite":
        return BS.is_initialized(_sqlite_path())
    return os.path.exists(BOARD_PATH)


def seed_board_if_missing():
    """Boot step: give a genuinely fresh install an empty board, and never touch an existing one.
    Split out of main() so a test can run the real boot decision without starting a server."""
    if board_exists():
        return False
    save_board(default_board())
    return True


def load_board():
    if BOARD_BACKEND == "sqlite":
        return BS.load_board(_sqlite_path())
    b = C.read_json(BOARD_PATH, None)
    if not b or not isinstance(b, dict):
        return default_board()
    b.setdefault("order", [])
    b.setdefault("pinSeq", 0)
    b.setdefault("tasks", {})
    return b


def save_board(board):
    """Atomic save + rolling timestamped backup + catastrophic-shrink guard (§3).

    SQLite backend: atomic single-transaction row-per-card write (only changed cards), same
    catastrophic-shrink refusal; durable backups come from the decoupled board-exporter.
    """
    if BOARD_BACKEND == "sqlite":
        os.makedirs(TODOS_DIR, exist_ok=True)
        return BS.save_board(_sqlite_path(), board)
    os.makedirs(TODOS_DIR, exist_ok=True)
    ondisk = C.read_json(BOARD_PATH, None)
    new_n = len(board.get("tasks", {}))
    if ondisk and isinstance(ondisk, dict):
        old_n = len(ondisk.get("tasks", {}))
        if old_n > 5 and new_n < 0.5 * old_n:
            # refuse catastrophic shrink: write SUSPECT, keep the live board intact
            sp = "%s.SUSPECT.%d" % (BOARD_PATH, int(time.time()))
            C.atomic_write(sp, json.dumps(board, ensure_ascii=False).encode())
            sys.stderr.write("SUSPECT shrink refused: %d->%d, wrote %s\n" % (old_n, new_n, sp))
            return False
        # roll a backup of the current on-disk board before replacing
        try:
            bak = "%s.bak.%d" % (BOARD_PATH, int(time.time() * 1000))
            C.atomic_write(bak, json.dumps(ondisk, ensure_ascii=False).encode())
            baks = sorted(glob.glob("%s.bak.*" % BOARD_PATH))
            for old in baks[:-20]:
                try:
                    os.remove(old)
                except Exception:
                    pass
        except Exception:
            pass
    C.atomic_write(BOARD_PATH, json.dumps(board, ensure_ascii=False).encode())
    return True



def delete_comment(board, tid, cid, actor):
    """Remove ONE comment by id. Only its own author or the Boss may do it.

    No bulk, no wildcard: a delete is irreversible on every card of the board, so the scope is a
    single id the caller already knows, and an actor who is neither the author nor the Boss is
    refused. Returns (error, removed_comment)."""
    t = board["tasks"].get(tid)
    if not t:
        return "no_task", None
    for i, c in enumerate(t.get("comments") or []):
        if c.get("id") == cid:
            if not actor or actor not in (c.get("by"), BOSS_AGENT):
                return "not_author", None
            return None, t["comments"].pop(i)
    return "no_comment", None

def now():
    return time.time()


def _tmux_window_sizes():
    """Every window's size in ONE tmux call, keyed by the `mc-<sess>:<tab>` target.

    The graph polls this every 2s per open page, and it used to ask tmux once PER AGENT.
    That cost grows with the fleet while the answer is a single list: measured on a live
    27-agent install, the per-agent loop took 149ms and this one call takes 5ms.

    Failure stays soft, exactly as the per-agent version did: an empty map makes every node
    fall back to the standard 160x48 below, because a missing window size must never be the
    reason the fleet does not render.
    """
    try:
        out = subprocess.run(["tmux", "list-windows", "-a", "-F",
                              "#{session_name}:#{window_name} #{window_width} #{window_height}"],
                             capture_output=True, text=True, timeout=4,
                             env={**os.environ, "TMUX": ""})
        if out.returncode != 0:
            return {}
    except Exception:
        return {}
    sizes = {}
    for line in out.stdout.splitlines():
        parts = line.rsplit(" ", 2)
        if len(parts) != 3:
            continue
        try:
            sizes[parts[0]] = (int(parts[1]), int(parts[2]))
        except ValueError:
            continue
    return sizes


# ---------------- board->Boss ping ----------------
def mp_path():
    return shutil.which("mp") or os.path.join(INSTALL_DIR, "bin", "mp")


def mp_send(agent, message):
    mp = mp_path()
    try:
        r = subprocess.run([mp, "send", agent, message], capture_output=True, text=True, timeout=25)
        rc = r.returncode
    except Exception as e:
        rc = -1
        r = None
    try:
        with open(INBOX_LOG, "a") as f:
            f.write("MP_SEND -> %s rc=%s :: %s\n" % (agent, rc, message[:160]))
    except Exception:
        pass
    return rc


def ping_boss(message):
    threading.Thread(target=mp_send, args=(BOSS_AGENT, message), daemon=True).start()


# ---------------- card ownership ----------------
def migrate_legacy_owner_fields(board):
    """Idempotently describe existing owners without fabricating their original time."""
    changed = False
    for task in board.get("tasks", {}).values():
        history = task.get("ownerHistory")
        if not isinstance(history, list):
            history = []
            task["ownerHistory"] = history
            changed = True
        owner = task.get("assignee", "")
        if owner and not history:
            history.append({"action": "migrated_existing_owner", "agent_id": owner,
                            "previous": "", "by": "system", "ts": now()})
            changed = True
        if not isinstance(task.get("ownerNeedsReplacement"), bool):
            task["ownerNeedsReplacement"] = False
            changed = True
    return changed


def record_owner_event(task, action, agent_id, previous="", by=BOSS_AGENT):
    task.setdefault("ownerHistory", []).append({"action": action, "agent_id": agent_id,
        "previous": previous, "by": by, "ts": now()})


def queue_kill_owner(owner, reason):
    """Actually kill a card owner via the queue (does not depend on Boss being alive).

    Close/replace used to ONLY ping the Boss. If Boss was down, mid-crash, or a new session that
    missed the lifecycle message, owners became permanent ghosts (card 476fd89607 -- six eng still
    alive on closed cards). Submitting type=kill through queue-server is the same path /todo/boss
    and queue-client already use.
    """
    if not owner or not FULL_AGENT_ID.match(owner):
        return False
    try:
        code, r = C.http_json(
            "POST", CFG["QUEUE_URL"] + "/task/submit",
            {"type": "kill", "target_agent": owner,
             "payload": {"reason": reason}},
            {"X-Queue-Secret": SECRET}, timeout=10)
        return code == 200 and isinstance(r, dict) and bool(r.get("task_id"))
    except Exception:
        return False


def request_owner_lifecycle(task, action, owner=""):
    if task.get("test"):
        return
    tid = task["id"]
    if action == "close":
        # Primary: kill via queue so ghosts cannot outlive a dead/busy Boss.
        # Secondary: ping Boss for the audit trail / any extra bookkeeping.
        if owner:
            queue_kill_owner(owner, "card-%s-closed" % tid)
        ping_boss('[todo lifecycle] Card %s was CLOSED by the CEO. Kill its owner %s now; '
                  'preserve the assignee/history. Do not replace it unless the CEO reopens the card.' %
                  (tid, owner or "unassigned"))
    elif action == "reopen":
        ping_boss('[todo lifecycle] Card %s was REOPENED by the CEO. CREATE one FRESH owner engineer '
                  'with --owner-task %s; do not reuse %s.' % (tid, tid, owner or "unassigned"))
    elif action == "replace":
        if owner:
            queue_kill_owner(owner, "card-%s-owner-replaced" % tid)
        ping_boss('[todo lifecycle] Card %s owner was REPLACED. Kill prior owner %s; preserve history.' %
                  (tid, owner or "unassigned"))


def apply_owner_state_transition(task, previous, state, actor):
    was_terminal, is_terminal = previous in TERMINAL_STATES, state in TERMINAL_STATES
    owner = task.get("assignee", "")
    if not was_terminal and is_terminal:
        record_owner_event(task, "closed", owner, previous=owner, by=actor)
        task["ownerNeedsReplacement"] = False
        request_owner_lifecycle(task, "close", owner)
    elif was_terminal and not is_terminal:
        record_owner_event(task, "reopen_requested", "", previous=owner, by=actor)
        task["ownerNeedsReplacement"] = True
        request_owner_lifecycle(task, "reopen", owner)


def title_of(task):
    # Verbatim on purpose (CEO, card 5676f76673). This used to cut at 60 and the comment
    # body below at 120 -- silently, with no ellipsis -- so a Boss ping carried a reason it
    # never fully received: a 1932-char comment reached it as 120 chars, 93.8% dropped.
    # A notification that omits the ask is worse than a long one. Do not re-cap these.
    return task.get("text", "") or ""


def emit_task_event(board, task, reason, by=None):
    """Ping Boss for a non-test add / work-state change. Exempt {test} tasks."""
    if task.get("test"):
        return
    tid = task["id"]
    task["pingsToBoss"] = task.get("pingsToBoss", 0) + 1
    ping_boss('[todo] task %s "%s": %s' % (tid, title_of(task), reason))


def owner_relay(card_id, by, bodytext):
    """The owner's copy of a comment: their words, whole and unchanged, plus where it was said.

    Same shape the Boss relays by hand, so an agent reads exactly what it read before.
    """
    return '%s on card %s, verbatim: "%s"' % (by, card_id, bodytext)


def boss_chat_relay(card_id, by, bodytext):
    """The Boss chat card is a conversation, not a task: say so, and say where the answer goes."""
    return ('[boss chat] %s: %s\n(reply on the Boss chat card: mp comment %s "your answer")'
            % (by, bodytext, card_id))


def ensure_boss_chat(board):
    """Caller holds LOCK. Give the board its one Boss chat card, pinned to the top by the UI.

    A flag, not a pin: pinRank is append-only, so a pin would land last among the pinned cards.
    Created silently -- no task event, no watchdog job -- since nobody is meant to own it.
    Returns True when the board changed."""
    t = board["tasks"].get(board.get("bossChat") or "")
    if t and t.get("bossChat"):
        return False
    tid = uuid.uuid4().hex[:10]
    board["tasks"][tid] = {"id": tid, "text": "Boss", "state": "working", "bossChat": True,
                           "assignee": "", "ownerHistory": [], "ownerNeedsReplacement": False,
                           "pinned": False, "pinRank": None, "doneCondition": "", "workToDone": "",
                           "done": False, "verified": False, "unread": 0, "pingsToBoss": 0,
                           "comments": [], "proofs": [], "test": False,
                           "created": now(), "updated": now(), "lastAction": now()}
    board["order"].insert(0, tid)
    board["bossChat"] = tid
    return True


def deliver_comment(card_id, title, owner, by, bodytext, boss_chat=False):
    """Owner first, then the Boss. Runs off the request thread: mp_send blocks up to 25s."""
    if boss_chat:
        return ping_boss(boss_chat_relay(card_id, by, bodytext))
    delivered = False
    if owner:
        delivered = mp_send(owner, owner_relay(card_id, by, bodytext)) == 0
    # ping_boss stays the one seam for Boss notifications; it is what the verbatim-relay tests
    # pin, and what any future change to Boss delivery has to go through.
    ping_boss('[todo] comment on %s "%s" by %s%s: %s'
              % (card_id, title, by,
                 " (already delivered to %s)" % owner if delivered else "", bodytext))


def emit_comment_event(board, task, by, bodytext):
    if task.get("test"):
        return
    if by == BOSS_AGENT:
        return                                  # the Boss's own comment pings nobody
    task["pingsToBoss"] = task.get("pingsToBoss", 0) + 1
    # Straight to the card's owner. Routing through the Boss meant every word waited for a whole
    # agent turn before it moved: measured on the CEO's own card, a median of 18s between his
    # comment and the owner acting on it, worst case 439s. The board already knows the owner, so
    # that hop buys nothing. The Boss is still told -- and told the owner already has it, so it
    # does not relay the same words a second time.
    owner = (task.get("assignee") or "").strip()
    if owner in (by, BOSS_AGENT):
        owner = ""
    threading.Thread(target=deliver_comment,
                     args=(task["id"], title_of(task), owner, by, bodytext,
                           bool(task.get("bossChat"))),
                     daemon=True).start()


# ---------------- proof kind classification ----------------
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
VID_EXT = (".mp4", ".webm", ".mov", ".m4v")


def classify_kind(url=None, filename=None, content_type=None, given=None):
    name = (filename or url or "").lower().split("?")[0]
    ct = (content_type or "").lower()
    if ct.startswith("image/") or name.endswith(IMG_EXT):
        return "image"
    if ct.startswith("video/") or name.endswith(VID_EXT):
        return "video"
    if url and (url.startswith("http://") or url.startswith("https://") or url.startswith("/")):
        return "link" if (given in (None, "", "text", "link")) else given
    if given in ("image", "video", "link"):
        return given
    return "text"


def unservable_proof_url(url):
    """Explain why a proof URL could never render on the board, or return '' if it can.

    The board is served over http, so a file:// URL or a bare local path is dead on arrival:
    the browser refuses to load it and the card shows a broken image with no explanation.
    Such a proof must be uploaded (multipart) so it is served from /todo/proof-file/.
    """
    u = (url or "").strip()
    if not u:
        return "empty url"
    if u.startswith("file://"):
        return ("file:// URLs cannot be loaded by the board (page is served over http). "
                "Upload the file instead: mp proof <task_id> <path> [note]")
    if u.startswith("/todo/") or u.startswith("http://") or u.startswith("https://"):
        return ""
    if u.startswith("/") or u.startswith("~") or u.startswith("./") or u.startswith("../"):
        return ("local filesystem paths are not reachable from the board. "
                "Upload the file instead: mp proof <task_id> <path> [note]")
    return ""


def parse_multipart(handler, ctype):
    """Minimal multipart/form-data parser: returns (fields:dict, file:(filename,ctype,bytes)|None)."""
    m = re.search(r'boundary=("?)([^";]+)\1', ctype)
    if not m:
        return {}, None
    boundary = ("--" + m.group(2)).encode()
    n = int(handler.headers.get("Content-Length", 0) or 0)
    body = handler.rfile.read(n)
    fields = {}
    fileobj = None
    for part in body.split(boundary):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        # Strip exactly the one CRLF that delimits the part, never trailing payload bytes —
        # rstrip() would silently corrupt any file whose last bytes are 0x0d/0x0a.
        if data.endswith(b"\r\n"):
            data = data[:-2]
        htxt = head.decode("latin-1", "ignore")
        dm = re.search(r'name="([^"]+)"', htxt)
        fm = re.search(r'filename="([^"]*)"', htxt)
        cm = re.search(r'Content-Type:\s*([^\r\n]+)', htxt, re.I)
        nm = dm.group(1) if dm else "field"
        if fm and fm.group(1):
            fileobj = (fm.group(1), (cm.group(1).strip() if cm else ""), data)
        else:
            fields[nm] = data.decode("utf-8", "ignore")
    return fields, fileobj


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
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

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _identity(self):
        return C.caller_identity(self.headers)

    def _page_extra(self):
        return {
            "Set-Cookie": "mp_session=%s; HttpOnly; Path=/; SameSite=Lax" % C.mint_session(),
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache", "Expires": "0",
        }

    def _serve_page(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                html = C.render_page(f.read())
        except Exception:
            html = C.render_page("<h1>MyPlow</h1>")
        self._send(200, raw=html, ctype="text/html; charset=utf-8", extra=self._page_extra())

    def do_HEAD(self):
        p = self.path.split("?", 1)[0]
        if p in ("/", "/todos", "/terminal-graph"):
            self.send_response(200)
            for k, v in self._page_extra().items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/dashboard" or p.startswith("/dashboard/") or p in ("/agents", "/clients", "/roster"):
            return C.proxy_request(self, "127.0.0.1", HUD_PORT)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- GET ----
    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/health":
            build = "0"
            try:
                build = str(int(os.path.getmtime(TODOS_HTML)))
            except Exception:
                pass
            return self._send(200, {"status": "ok", "build": build, "uptime": int(now() - START)})
        if p == "/favicon.ico":
            return self._send(204, raw=b"")
        # Brand fonts (bin/fonts, SIL OFL). Public like the pages that link them; basename only.
        if p.startswith("/fonts/"):
            name = os.path.basename(p)
            fp = os.path.join(HTML_DIR, "fonts", name)
            ctype = FONT_TYPES.get(os.path.splitext(name)[1])
            if not ctype or not os.path.isfile(fp):
                return self._send(404, {"error": "no_file"})
            with open(fp, "rb") as f:
                return self._send(200, raw=f.read(), ctype=ctype,
                                  extra={"Cache-Control": "public, max-age=86400"})
        if p in ("/", "/todos"):
            return self._serve_page(TODOS_HTML)
        if p == "/terminal-graph":
            return self._serve_page(TERMINAL_GRAPH_HTML if os.path.exists(TERMINAL_GRAPH_HTML)
                                    else TODOS_HTML)
        # HUD routes -> proxy to queue-server (symmetric front doors)
        if p == "/dashboard" or p.startswith("/dashboard/") or p in ("/agents", "/clients", "/roster"):
            return C.proxy_request(self, "127.0.0.1", HUD_PORT)
        # proof file serving (both new flat + legacy nested routes)
        if p.startswith("/todo/proof-file/"):
            ok, _ = self._identity()
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            name = os.path.basename(p.split("/todo/proof-file/", 1)[1])
            fp = os.path.join(PROOFS_DIR, name)
            return self._serve_file(fp)
        if p.startswith("/todo/proof/"):
            ok, _ = self._identity()
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            rest = p.split("/todo/proof/", 1)[1].split("/")
            if len(rest) >= 2:
                tid = os.path.basename(rest[0])
                fn = os.path.basename(rest[1])
                fp = os.path.join(PROOFS_DIR, tid, fn)
                return self._serve_file(fp)
        # gated json
        ok, ident = self._identity()
        if p == "/todo/board":
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            with LOCK:
                return self._send(200, self._board_view())
        if p == "/todo/terminal-graph":
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, self._terminal_graph())
        if p.startswith("/todo/attach"):
            if not ok:
                return self._send(401, {"error": "unauthorized"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            agent = q.get("agent", [""])[0]
            code, agents = C.http_json("GET", CFG["QUEUE_URL"] + "/agents", None, {"X-Queue-Secret": SECRET})
            base = ""
            target = C.tmux_target(agent)
            for a in (agents or []):
                if a.get("agent_id") == agent:
                    base = a.get("attach_base", "")
                    target = a.get("tmux_target", target)
            return self._send(200, {"ok": True, "target": target, "base": base,
                                    "port": int(CFG["TTYD_BROWSER_PORT"])})
        return self._send(404, {"error": "not_found"})

    def _serve_file(self, fp):
        if not os.path.isfile(fp):
            return self._send(404, {"error": "no_file"})
        import mimetypes
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        with open(fp, "rb") as f:
            data = f.read()
        return self._send(200, raw=data, ctype=ctype)

    # ---- POST ----
    def do_POST(self):
        p = self.path.split("?", 1)[0]
        if p == "/dashboard" or p in ("/agents", "/clients", "/roster", "/revive") or p.startswith("/task/"):
            return C.proxy_request(self, "127.0.0.1", HUD_PORT)
        ok, ident = self._identity()
        if not ok:
            return self._send(401, {"error": "unauthorized"})

        ctype = self.headers.get("Content-Type", "")
        if p == "/todo/proof" and ctype.startswith("multipart/form-data"):
            return self._proof_multipart()
        body = self._json_body()

        if p == "/todo/update":
            return self._update(body, ident)
        if p == "/todo/comment":
            return self._comment(body, ident)
        if p == "/todo/comment/delete":
            return self._comment_delete(body, ident)
        if p == "/todo/owner":
            return self._owner(body, ident)
        if p == "/todo/proof":
            return self._proof_json(body)
        if p == "/todo/status":
            return self._status_op(body, ident)
        if p == "/todo/boss":
            return self._boss_op(body, ident)
        return self._send(404, {"error": "not_found"})

    # ---- Boss lifecycle control (card 0cc0bde980) ----
    def _boss_alive(self):
        """Is the singleton Boss up? Heartbeat-driven, so it can lag reality by one interval; it
        is a UX guard, not the invariant. `mp spawn`'s own window_exists check is the real backstop
        and queue-client dispatches serially, so a stale yes/no here cannot produce two Bosses."""
        code, agents = C.http_json("GET", CFG["QUEUE_URL"] + "/agents", None,
                                   {"X-Queue-Secret": SECRET}, timeout=6)
        for a in (agents or []):
            if a.get("is_master") and a.get("state") == "alive":
                return True, a.get("agent_id", "")
        return False, ""

    def _boss_op(self, body, ident):
        """Kill / spawn / revive the Boss from the board UI.

        Adds NO new execution mechanism: every action rides the queue-server task path that
        queue-client's dispatch() already implements against `mp` (type=kill with --reason,
        type=spawn with --master --backend, type=revive). This endpoint only chooses the
        arguments and enforces the exactly-one-Boss guard.

        spawn vs revive: spawn starts a NEW Boss on a chosen engine; revive restores the SAME
        Boss, resuming its persisted session (its memory). Both require the Boss to be down.
        """
        action = (body.get("action") or "").strip().lower()
        alive, live_id = self._boss_alive()

        if action == "kill":
            if not alive:
                return self._send(409, {"ok": False, "error": "boss_not_alive"})
            # A non-accidental reason is what makes the kill stick: mp records it as durable intent
            # in roster.json and ensure-boss refuses to respawn against it.
            payload = {"reason": "ceo-kill:%d" % int(time.time())}
            target = live_id or BOSS_AGENT
        elif action == "spawn":
            backend = (body.get("backend") or "").strip().lower()
            if backend not in BOSS_BACKENDS:
                return self._send(400, {"ok": False, "error": "bad_backend",
                                        "detail": "choose %s" % ", ".join(BOSS_BACKENDS)})
            if alive:
                return self._send(409, {"ok": False, "error": "boss_already_alive",
                                        "agent_id": live_id,
                                        "detail": "kill the running Boss before spawning another"})
            # role:boss is required or do_spawn refuses a bundle-less Boss; queue-client also
            # defaults it for any master spawn, but state it explicitly here (card f6339b85a2).
            payload = {"backend": backend, "is_master": True, "role": "boss"}
            target = BOSS_AGENT
        elif action == "revive":
            if alive:
                return self._send(409, {"ok": False, "error": "boss_already_alive",
                                        "agent_id": live_id,
                                        "detail": "the Boss is already up; nothing to revive"})
            payload = {}
            target = BOSS_AGENT
        else:
            return self._send(400, {"ok": False, "error": "bad_action",
                                    "detail": "choose kill, spawn or revive"})

        code, r = C.http_json("POST", CFG["QUEUE_URL"] + "/task/submit",
                              {"type": action, "target_agent": target, "payload": payload},
                              {"X-Queue-Secret": SECRET}, timeout=10)
        if code != 200 or not isinstance(r, dict) or not r.get("task_id"):
            return self._send(502, {"ok": False, "error": "submit_failed", "detail": str(r)[:200]})
        return self._send(200, {"ok": True, "action": action, "agent_id": target,
                                "task_id": r["task_id"], "by": ident})

    # ---- board view (server-authoritative ordering: pinned first by pinRank, then order) ----
    def _terminal_graph(self):
        """Live fleet topology. Polling this metadata never replaces terminal iframe clients."""
        acode, agents = C.http_json("GET", CFG["QUEUE_URL"] + "/agents", None,
                                    {"X-Queue-Secret": SECRET})
        rcode, roster = C.http_json("GET", CFG["QUEUE_URL"] + "/roster", None,
                                    {"X-Queue-Secret": SECRET})
        if acode != 200 or not isinstance(agents, list): agents = []
        if rcode != 200 or not isinstance(roster, list): roster = []
        roster_by_id = {r.get("agent_id", ""): r for r in roster}
        live_ids = {a.get("agent_id", "") for a in agents if a.get("state", "alive") == "alive"}
        sizes = _tmux_window_sizes()
        nodes = []
        for a in agents:
            aid = a.get("agent_id", ""); rr = roster_by_id.get(aid, {})
            if not aid or aid not in live_ids or rr.get("retired") is True: continue
            target = a.get("tmux_target") or C.tmux_target(aid)
            cols, rows = sizes.get(target, (0, 0))
            nodes.append({"agent_id": aid, "boss_id": rr.get("boss_id") or a.get("boss_id", ""),
                          "is_master": bool(rr.get("is_master") or a.get("is_master")),
                          "state": a.get("status", "ready"), "summary": a.get("summary", ""),
                          "target": target, "host": rr.get("host") or a.get("host", ""),
                          "cols": cols or 160, "rows": rows or 48})
        nodes.sort(key=lambda n: (not n["is_master"], n["agent_id"]))
        node_ids = {n["agent_id"] for n in nodes}
        edges = [{"from": n["boss_id"], "to": n["agent_id"]} for n in nodes
                 if n["boss_id"] in node_ids]
        # Ports come from config, never constants: the tiles are iframes onto this install's own
        # ttyd, and a hardcoded port silently renders every tile dead wherever ttyd was moved.
        return {"nodes": nodes, "edges": edges,
                "readonly_port": int(CFG["TTYD_RO_PORT"]),
                "interactive_port": int(CFG["TTYD_BROWSER_PORT"]), "ts": now()}

    def _board_view(self):
        board = load_board()
        return board

    # ---- update op ----
    def _update(self, body, ident):
        op = body.get("op")
        actor = body.get("by") or ("CEO" if ident == "browser" else "")
        with LOCK:
            board = load_board()
            if op == "add":
                tid = uuid.uuid4().hex[:10]
                # assignee is NOT settable here: /todo/owner is the only door, and it is the one
                # that checks the agent is alive, unretired, this Boss's, and born for this card.
                task = {"id": tid, "text": body.get("text", ""), "state": "working",
                        "assignee": "", "ownerHistory": [], "ownerNeedsReplacement": False,
                        "pinned": False, "pinRank": None,
                        "doneCondition": "", "workToDone": "", "done": False, "verified": False,
                        "unread": 0, "pingsToBoss": 0, "comments": [], "proofs": [],
                        "test": bool(body.get("test")), "created": now(), "updated": now(),
                        "lastAction": now()}
                board["tasks"][tid] = task
                board["order"].insert(0, tid)
                save_board(board)
                emit_task_event(board, task, "new task added")
                save_board(board)
                # Every new card, regardless of who created it. Boss relays most of the CEO's
                # cards via the API (by=.../Boss), so an actor=="CEO" guard here would leave those
                # silently uncovered.
                wd_schedule_taskcreate(tid, task["state"])
                return self._send(200, {"ok": True, "id": tid})
            if op == "del":
                tid = body.get("id")
                if tid in board["tasks"]:
                    for proof in board["tasks"][tid].get("proofs", []):
                        url = proof.get("url", "")
                        if url.startswith("/todo/proof-file/"):
                            fp = os.path.join(PROOFS_DIR, os.path.basename(url))
                            try:
                                os.remove(fp)
                            except OSError:
                                pass
                    del board["tasks"][tid]
                    board["order"] = [x for x in board["order"] if x != tid]
                    save_board(board)
                    return self._send(200, {"ok": True})
                return self._send(200, {"ok": False, "error": "no_task"})
            if op == "set":
                tid = body.get("id")
                t = board["tasks"].get(tid)
                if not t:
                    return self._send(200, {"ok": False, "error": "no_task"})
                if "assignee" in body:
                    return self._send(400, {"ok": False, "error": "assignee_controlled"})
                # reject removed features (subtasks/deps/hardgate) silently — never persist
                for banned in ("parent", "dependsOn", "hardGate", "brainstorm"):
                    body.pop(banned, None)
                prev_state = t.get("state")
                if "state" in body:
                    st = body["state"]
                    if st not in VALID_STATES:
                        return self._send(400, {"ok": False, "error": "bad_state"})
                    # Closing a card is the CEO's call alone: it kills the owner and declares the
                    # work over, so an agent must not be able to retire its own card.
                    if st in TERMINAL_STATES and actor != "CEO":
                        return self._send(403, {"ok": False, "error": "ceo_only_terminal"})
                    t["state"] = st
                    if st == "done":
                        t["done"] = True
                for f in ("text", "doneCondition", "workToDone"):
                    if f in body:
                        t[f] = body[f]
                if body.get("done") is True or body.get("workToDone") is True:
                    # the same close, by another name -- gate it identically
                    if actor != "CEO":
                        return self._send(403, {"ok": False, "error": "ceo_only_terminal"})
                    t["state"] = "done"
                    t["done"] = True
                if "verified" in body:
                    t["verified"] = bool(body["verified"])
                t["updated"] = now()
                t["lastAction"] = now()
                if t.get("state") != prev_state:
                    apply_owner_state_transition(t, prev_state, t.get("state"), actor)
                save_board(board)
                if t.get("state") != prev_state:
                    emit_task_event(board, t, "state -> %s" % t.get("state"))
                    save_board(board)
                return self._send(200, {"ok": True})
            if op == "pin":
                tid = body.get("id")
                t = board["tasks"].get(tid)
                if not t:
                    return self._send(200, {"ok": False, "error": "no_task"})
                if t.get("pinned"):
                    return self._send(200, {"ok": True})
                board["pinSeq"] += 1
                t["pinned"] = True
                t["pinRank"] = board["pinSeq"]
                save_board(board)
                return self._send(200, {"ok": True})
            if op == "unpin":
                tid = body.get("id")
                t = board["tasks"].get(tid)
                if not t:
                    return self._send(200, {"ok": False, "error": "no_task"})
                t["pinned"] = False
                t["pinRank"] = None
                save_board(board)
                return self._send(200, {"ok": True})
            return self._send(400, {"ok": False, "error": "bad_op"})

    # ---- owner ----
    def _owner(self, body, ident):
        """Boss-only. Every clause below is a way a card ends up with the wrong owner, so the
        eligibility check is deliberately exhaustive: the agent must be alive, not retired, report
        to THIS Boss, have been born for ownership, and have been born for THIS card."""
        if ident != "machine" or body.get("by") != BOSS_AGENT:
            return self._send(403, {"ok": False, "error": "boss_only"})
        action, tid, agent_id = body.get("action"), body.get("task_id"), body.get("agent_id", "")
        if action not in ("assign", "replace", "reopen") or not FULL_AGENT_ID.match(agent_id):
            return self._send(400, {"ok": False, "error": "bad_owner_request"})
        code, roster = C.http_json("GET", CFG["QUEUE_URL"] + "/roster", None,
                                   {"X-Queue-Secret": SECRET})
        if code != 200 or not isinstance(roster, list):
            return self._send(503, {"ok": False, "error": "roster_unavailable"})
        row = next((r for r in roster if r.get("agent_id") == agent_id), None)
        if (not row or row.get("state") != "alive" or row.get("retired") is True or
                row.get("boss_id") != BOSS_AGENT or row.get("lifecycle") != "owner" or
                row.get("owner_task_id") != tid):
            return self._send(400, {"ok": False, "error": "ineligible_owner"})
        with LOCK:
            board = load_board(); task = board.get("tasks", {}).get(tid)
            if not task:
                return self._send(200, {"ok": False, "error": "no_task"})
            for other in board.get("tasks", {}).values():
                if (other.get("id") != tid and other.get("assignee") == agent_id and
                        other.get("state") not in TERMINAL_STATES):
                    return self._send(409, {"ok": False, "error": "owner_busy"})
            current = task.get("assignee", "")
            if action == "assign":
                if current == agent_id:
                    return self._send(200, {"ok": True, "assignee": current,
                                            "previous": current, "action": action})
                if current:
                    return self._send(409, {"ok": False, "error": "owner_exists"})
            elif action == "replace":
                if not current:
                    return self._send(409, {"ok": False, "error": "no_owner_to_replace"})
            elif action == "reopen":
                if not task.get("ownerNeedsReplacement"):
                    return self._send(409, {"ok": False, "error": "card_not_awaiting_reopen_owner"})
                if current == agent_id:
                    return self._send(409, {"ok": False, "error": "fresh_owner_required"})
            previous = current
            task["assignee"] = agent_id; task["ownerNeedsReplacement"] = False
            record_owner_event(task, action, agent_id, previous=previous)
            task["updated"] = now(); task["lastAction"] = now(); save_board(board)
            if action == "replace" and previous:
                request_owner_lifecycle(task, "replace", previous)
            return self._send(200, {"ok": True, "assignee": agent_id,
                                    "previous": previous, "action": action})

    # ---- comment ----
    def _comment(self, body, ident):
        with LOCK:
            board = load_board()
            tid = body.get("task_id")
            t = board["tasks"].get(tid)
            if not t:
                return self._send(200, {"ok": False, "error": "no_task"})
            by = body.get("by", "CEO")
            c = {"id": uuid.uuid4().hex[:8], "by": by, "kind": "comment",
                 "body": body.get("body", ""), "ts": now()}
            t["comments"].append(c)
            if by != "CEO":
                t["unread"] = t.get("unread", 0) + 1
            t["updated"] = now()
            t["lastAction"] = now()
            t["idleFired"] = False
            save_board(board)
            emit_comment_event(board, t, by, c["body"])
            save_board(board)
            wd_schedule_unanswered(tid, c["id"], by)
            return self._send(200, {"ok": True, "id": c["id"]})

    # ---- comment delete (one id, author or Boss only) ----
    def _comment_delete(self, body, ident):
        actor = body.get("by") or ("CEO" if ident == "browser" else "")
        tid, cid = body.get("task_id"), body.get("comment_id")
        if not tid or not cid:
            return self._send(400, {"ok": False, "error": "task_id_and_comment_id_required"})
        with LOCK:
            board = load_board()
            err, c = delete_comment(board, tid, cid, actor)
            if err:
                code = 403 if err == "not_author" else 200
                return self._send(code, {"ok": False, "error": err})
            board["tasks"][tid]["updated"] = now()
            save_board(board)
            try:
                with open(os.path.join(TODOS_DIR, "comment-deletes.jsonl"), "a") as f:
                    f.write(json.dumps({"comment_id": cid, "card": tid, "author": c.get("by"),
                                        "actor": actor, "ts": now()}) + "\n")
            except OSError:
                pass
            return self._send(200, {"ok": True, "deleted": cid})

    # ---- proof (json) ----
    def _proof_json(self, body):
        with LOCK:
            board = load_board()
            tid = body.get("task_id")
            t = board["tasks"].get(tid)
            if not t:
                return self._send(200, {"ok": False, "error": "no_task"})
            url = body.get("url", "")
            bad = unservable_proof_url(url)
            if bad:
                return self._send(200, {"ok": False, "error": "unservable_url", "detail": bad})
            kind = classify_kind(url=url, given=body.get("kind"))
            proof = {"kind": kind, "url": url, "body": body.get("body", ""), "ts": now()}
            t["proofs"].append(proof)
            t["updated"] = now()
            save_board(board)
            return self._send(200, {"ok": True, "proof": proof})

    # ---- proof (multipart upload) ----
    def _proof_multipart(self):
        fields, fileobj = parse_multipart(self, self.headers.get("Content-Type", ""))
        tid = fields.get("task_id", "")
        with LOCK:
            board = load_board()
            t = board["tasks"].get(tid)
            if not t:
                return self._send(200, {"ok": False, "error": "no_task"})
            if fileobj:
                fn, fct, data = fileobj
                os.makedirs(PROOFS_DIR, exist_ok=True)
                safe = "%s-%s" % (uuid.uuid4().hex[:8], os.path.basename(fn))
                with open(os.path.join(PROOFS_DIR, safe), "wb") as f:
                    f.write(data)
                kind = classify_kind(filename=fn, content_type=fct, given=fields.get("kind"))
                url = "/todo/proof-file/%s" % safe
                proof = {"kind": kind, "url": url, "body": fields.get("body", ""), "ts": now()}
            else:
                url = fields.get("url", "")
                proof = {"kind": classify_kind(url=url, given=fields.get("kind")),
                         "url": url, "body": fields.get("body", ""), "ts": now()}
            t["proofs"].append(proof)
            save_board(board)
            return self._send(200, {"ok": True, "proof": proof})

    # ---- status op ----
    def _status_op(self, body, ident):
        actor = body.get("by") or ("CEO" if ident == "browser" else "")
        with LOCK:
            board = load_board()
            tid = body.get("task_id")
            t = board["tasks"].get(tid)
            if not t:
                return self._send(200, {"ok": False, "error": "no_task"})
            st = body.get("state")
            if st and st not in VALID_STATES:
                return self._send(400, {"ok": False, "error": "bad_state"})
            if st in TERMINAL_STATES and actor != "CEO":
                return self._send(403, {"ok": False, "error": "ceo_only_terminal"})
            prev = t.get("state")
            if st:
                t["state"] = st
                t["done"] = (st == "done")
            if "verified" in body:
                t["verified"] = bool(body["verified"])
            t["updated"] = now()
            t["lastAction"] = now()
            t["idleFired"] = False
            if st and st != prev:
                apply_owner_state_transition(t, prev, st, actor)
            save_board(board)
            if st and st != prev:
                emit_task_event(board, t, "state -> %s" % st)
                save_board(board)
            return self._send(200, {"ok": True})


def main():
    os.makedirs(TODOS_DIR, exist_ok=True)
    seed_board_if_missing()
    with LOCK:
        board = load_board()
        if migrate_legacy_owner_fields(board) | ensure_boss_chat(board):
            save_board(board)
    threading.Thread(target=watchdog_worker, daemon=True, name="watchdog-worker").start()
    srv = ThreadingHTTPServer((CFG["BIND_ADDR"], TODO_PORT), Handler)
    srv.daemon_threads = True
    sys.stderr.write("todo-server on %s:%d\n" % (CFG["BIND_ADDR"], TODO_PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
