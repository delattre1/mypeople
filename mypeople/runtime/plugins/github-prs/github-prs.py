#!/usr/bin/env python3
"""GitHub PR watcher: reviews and comments on the fleet's open PRs reach the agent that owns them.

Without this, an engineer opens a PR, requests a review, and never hears the verdict: nothing in
the fleet reads GitHub. Every poll this finds the open PRs authored by the fleet's GitHub login,
and delivers each NEW comment, review or inline review comment with `mp send` to the agent whose
board card links that PR -- so the engineer who opened it reacts to its own review. A PR no card
links, or whose owner is gone, goes to the Boss.

Turn it on in ~/.config/mypeople/queue.env, then restart the daemons:

    export GITHUB_PRS=1
    export GITHUB_PRS_AUTHOR="your-github-login"   # optional: default is whoever `gh` is logged in as
    export GITHUB_PRS_INTERVAL=60                  # optional: seconds between polls

Needs the `gh` CLI logged in. The first time a PR is seen its existing comments are recorded
without being sent, so turning this on never replays a PR's history at anyone.

    github-prs.py serve        poll forever (what supervise.sh runs)
    github-prs.py once         one poll, then exit
    github-prs.py status       login, open PRs, and where each one would be delivered
    github-prs.py catchup      deliver every review still unanswered (use after downtime)
    github-prs.py catchup-dry  list them without sending anything
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

INSTALL = Path(os.environ.get("INSTALL_DIR") or os.environ.get("MYPEOPLE_HOME")
               or Path.home() / ".local/share/mypeople")
STATE = Path(os.environ.get("GITHUB_PRS_STATE_DIR") or INSTALL / "state" / "github-prs") / "state.json"
MP_BIN = os.environ.get("MP_BIN") or str(INSTALL / "bin" / "mp")
INTERVAL = int(os.environ.get("GITHUB_PRS_INTERVAL") or 60)
FULL_SWEEP_SECS = 30 * 60   # updatedAt does not move for every event (a body-less approve), so recheck all
SEARCH_LIMIT = 100          # gh search's ceiling; hitting it is logged, never silent
SNIPPET = 400
GH_TIMEOUT = 60
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# A comment that is only a slash command ("/srosro-review") is a trigger an agent posted, not a
# review; echoing it back to that agent is pure noise.
SLASH_ONLY = re.compile(r"^\s*/[\w-]+\s*$")


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S ") + "[github-prs] " + msg, flush=True)


# ---------------------------------------------------------------- config the daemons already share
def cfg(key, default=""):
    """Env first, then queue.env -- the same precedence as the rest of the runtime."""
    if os.environ.get(key):
        return os.environ[key]
    path = os.environ.get("MYPEOPLE_CONFIG_PATH") or str(Path.home() / ".config/mypeople/queue.env")
    try:
        for line in open(path):
            line = line.strip()
            line = line[7:] if line.startswith("export ") else line
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return default


# ---------------------------------------------------------------- GitHub
def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError("gh %s: %s" % (" ".join(args[:3]), (r.stderr or r.stdout).strip()[:300]))
    return json.loads(r.stdout or "null")


def login():
    return os.environ.get("GITHUB_PRS_AUTHOR") or cfg("GITHUB_PRS_AUTHOR") or gh("api", "user")["login"]


def open_prs(author):
    """[(repo, number, updatedAt, url)] for every open PR `author` wrote, in any repo gh can read."""
    prs = gh("search", "prs", "--author", author, "--state", "open", "--limit", str(SEARCH_LIMIT),
             "--json", "repository,number,updatedAt,url")
    if len(prs) >= SEARCH_LIMIT:
        log("WARNING: %d PRs is gh search's ceiling; some PRs may be invisible" % len(prs))
    return [(p["repository"]["nameWithOwner"], p["number"], p.get("updatedAt") or "", p["url"])
            for p in prs]


def pr_events(repo, n):
    """Comments, inline review comments and reviews on one PR, each with a stable key."""
    out = []
    for kind, path in (("comment", "repos/%s/issues/%d/comments" % (repo, n)),
                       ("review_comment", "repos/%s/pulls/%d/comments" % (repo, n)),
                       ("review", "repos/%s/pulls/%d/reviews" % (repo, n))):
        try:
            items = gh("api", "--paginate", path + "?per_page=100") or []
        except RuntimeError as e:
            log("skip %s %s#%d: %s" % (kind, repo, n, e))
            continue
        for it in items:
            out.append({"key": "%s:%s" % (kind, it["id"]), "kind": kind,
                        "user": (it.get("user") or {}).get("login", "?"),
                        "body": it.get("body") or "", "state": it.get("state") or "",
                        "url": it.get("html_url") or ""})
    return out


# ---------------------------------------------------------------- pure logic (tested)
def is_noise(event):
    body = HTML_COMMENT.sub("", event["body"])
    return bool(SLASH_ONLY.match(body)) and event["kind"] == "comment"


def should_fetch(key, updated_at, prev, full_sweep):
    return full_sweep or prev is None or not updated_at or updated_at != prev


def fresh_events(pr_key, events, state):
    """New events for this PR, recording everything as seen. A PR's first sighting is silent."""
    seen = set(state.setdefault("seen", []))
    known = set(state.setdefault("prs", []))
    new = [e for e in events if e["key"] not in seen]
    state["seen"] = sorted(seen | {e["key"] for e in events})
    if pr_key not in known:
        state["prs"] = sorted(known | {pr_key})
        return []
    return [e for e in new if not is_noise(e)]


def owner_for_pr(board, pr_url):
    """The assignee of the most recently updated card that links this PR, or "".

    A card "links" a PR when its text, a comment or a proof contains the PR URL. Live cards win
    over done/cancelled ones, then the most recently updated -- the PR belongs to whoever is
    working it now, not whoever mentioned it last month.
    """
    needle = re.compile(re.escape(pr_url.rstrip("/")) + r"(?![0-9])")
    best = None
    for task in (board.get("tasks") or {}).values():
        owner = task.get("assignee") or ""
        if not owner:
            continue
        haystack = json.dumps([task.get("text", ""), task.get("comments", []), task.get("proofs", [])])
        if not needle.search(haystack):
            continue
        live = task.get("state") not in ("done", "cancelled")
        rank = (live, float(task.get("updated") or 0))
        if best is None or rank > best[0]:
            best = (rank, owner)
    return best[1] if best else ""


def format_event(repo, n, event, owned, orphan=False):
    body = " ".join(HTML_COMMENT.sub("", event["body"]).split())
    if len(body) > SNIPPET:
        body = body[:SNIPPET - 1] + "…"
    verdict = event["state"].lower().replace("_", " ") if event["kind"] == "review" else ""
    what = {"review": "review", "review_comment": "inline review comment"}.get(event["kind"], "comment")
    head = "[PR %s] %s#%d%s by %s" % (what, repo, n, " (your PR)" if owned else "", event["user"])
    if verdict:
        head += " — %s" % verdict
    tail = ""
    if orphan:
        # Landing on the Boss with nobody owning it is how a review goes unanswered: it reads as
        # one more line among everything else the Boss is told. Say what to do with it.
        tail = (" — no card on the board links this PR, so nobody owns it: give it to an agent"
                " or handle it yourself.")
    return "%s: %s %s%s" % (head, body or "(no text)", event["url"], tail)


# ---------------------------------------------------------------- the fleet
def fetch_board():
    port = cfg("TODO_PORT", "9933")
    req = urllib.request.Request("http://127.0.0.1:%s/todo/board" % port,
                                 headers={"X-Queue-Secret": cfg("QUEUE_SECRET")})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def boss_id():
    return "%s/main:Boss" % cfg("HOST_ID", os.uname().nodename.split(".")[0])


def deliver(agent, text):
    r = subprocess.run([sys.executable, MP_BIN, "send", agent, text], capture_output=True, text=True,
                       timeout=60)
    return r.returncode == 0


def route(board, pr_url):
    owner = owner_for_pr(board, pr_url) if board else ""
    return owner or boss_id(), bool(owner)


# ---------------------------------------------------------------- loop
def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE)


def poll(author, state):
    now = time.time()
    full = now - state.get("last_full_sweep", 0) >= FULL_SWEEP_SECS
    updated = state.setdefault("updated", {})
    outbox = []
    for repo, n, upd, url in open_prs(author):
        key = "%s#%d" % (repo, n)
        if not should_fetch(key, upd, updated.get(key), full):
            continue
        updated[key] = upd
        for e in fresh_events(key, pr_events(repo, n), state):
            outbox.append((repo, n, url, e))
    if full:
        state["last_full_sweep"] = now
    board = None
    if outbox:
        try:
            board = fetch_board()
        except Exception as e:  # routing degrades to the Boss; the event is never dropped
            log("board unreadable (%s); sending to the Boss" % e)
    for repo, n, url, e in outbox:
        agent, owned = route(board, url)
        text = format_event(repo, n, e, owned, orphan=not owned)
        if not deliver(agent, text) and agent != boss_id():
            log("%s unreachable; sending to the Boss" % agent)
            agent, text = boss_id(), format_event(repo, n, e, False, orphan=True)
            deliver(agent, text)
        log("%s#%d %s -> %s" % (repo, n, e["key"], agent))
    save_state(state)


def unanswered(author, repo, n):
    """The newest review on this PR that nothing of ours answered after it, or None.

    Bootstrap never replays history on purpose, so a review that landed before this plugin
    existed -- or while it was down -- is never announced by the ordinary poll. That silence is
    exactly what leaves an approval sitting for days.
    """
    try:
        revs = gh("api", "--paginate", "repos/%s/pulls/%d/reviews?per_page=100" % (repo, n)) or []
        cms = gh("api", "--paginate", "repos/%s/issues/%d/comments?per_page=100" % (repo, n)) or []
    except RuntimeError as e:
        log("catch-up: %s#%d unreadable: %s" % (repo, n, e))
        return None
    theirs = [r for r in revs
              if (r.get("user") or {}).get("login") != author and r.get("submitted_at")]
    if not theirs:
        return None
    last = max(theirs, key=lambda r: r["submitted_at"])
    ours = [c.get("created_at") or "" for c in cms
            if (c.get("user") or {}).get("login") == author]
    if any(t > last["submitted_at"] for t in ours):
        return None
    return {"key": "review:%s" % last["id"], "kind": "review",
            "user": (last.get("user") or {}).get("login", "?"), "body": last.get("body") or "",
            "state": last.get("state") or "", "url": last.get("html_url") or ""}


def catchup(author, state, send=True):
    """Every open PR whose newest review is still unanswered, delivered like a normal event.

    Safe to run twice: what it sends is marked seen, so the ordinary poll will not repeat it.
    """
    board = None
    try:
        board = fetch_board()
    except Exception as e:
        log("board unreadable (%s); routing to the Boss" % e)
    seen = set(state.setdefault("seen", []))
    found = []
    for repo, n, _, url in open_prs(author):
        event = unanswered(author, repo, n)
        if not event:
            continue
        found.append((repo, n, event))
        if not send:
            continue
        agent, owned = route(board, url)
        if not deliver(agent, "[catch-up] " + format_event(repo, n, event, owned, orphan=not owned)) \
                and agent != boss_id():
            agent = boss_id()
            deliver(agent, "[catch-up] " + format_event(repo, n, event, False, orphan=True))
        seen.add(event["key"])
        log("catch-up %s#%d %s -> %s" % (repo, n, event["key"], agent))
    if send:
        state["seen"] = sorted(seen)
        save_state(state)
    return found


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "serve"
    author = login()
    if cmd == "status":
        try:
            board = fetch_board()
        except Exception as e:
            board = None
            print("board unreadable: %s" % e)
        prs = open_prs(author)
        print("watching %d open PRs by %s" % (len(prs), author))
        for repo, n, _, url in prs:
            agent, owned = route(board, url)
            print("  %s#%d -> %s%s" % (repo, n, agent, "" if owned else "  (no card links it)"))
        return 0
    state = load_state()
    if cmd in ("catchup", "catchup-dry"):
        found = catchup(author, state, send=(cmd == "catchup"))
        print("%d open PR(s) with an unanswered review%s"
              % (len(found), "" if cmd == "catchup" else " (dry run, nothing sent)"))
        for repo, n, event in found:
            print("  %s#%d %s by %s" % (repo, n, (event["state"] or "").lower(), event["user"]))
        return 0
    if cmd == "once":
        poll(author, state)
        return 0
    log("watching open PRs by %s every %ds" % (author, INTERVAL))
    while True:
        try:
            poll(author, state)
        except Exception as e:  # a GitHub hiccup must not kill the watcher
            log("poll failed: %s" % e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
