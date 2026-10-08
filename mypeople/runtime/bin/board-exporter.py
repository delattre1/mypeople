#!/usr/bin/env python3
"""Decoupled READ-ONLY board exporter (§3). Watches the live board.v2.json and commits a canonical
snapshot into a SEPARATE per-instance git repo OUTSIDE any server working tree. Never writes the live
board; never runs git in the live tree. Carries the <50%-shrink quarantine guard.

Usage: board-exporter.py            # daemon: watch + export
       board-exporter.py --once     # single export pass (for tests)"""
import os, sys, json, time, subprocess, hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpcommon as C
import boardstore as BS

CFG = C.CFG
INSTALL_DIR = CFG["INSTALL_DIR"]
BOARD_PATH = os.environ.get("BOARD_PATH", os.path.join(INSTALL_DIR, "todos", "board.v2.json"))
REPO = C.export_repo_path(CFG)
BOARD_BACKEND = BS.select_backend(BOARD_PATH, CFG.get("BOARD_BACKEND"))


def read_live_board():
    """Read the live board whatever the storage engine. The SQLite backend reconstructs the identical
    board dict via BoardStore; the git snapshot stays canonical JSON (human-diffable + rollback source)."""
    if BOARD_BACKEND == "sqlite":
        db = BS.db_path_for(BOARD_PATH)
        if not os.path.exists(db):
            return None
        return BS.load_board(db)
    return C.read_json(BOARD_PATH, None)


def git(*args, check=False):
    return subprocess.run(["git", "-C", REPO] + list(args), capture_output=True, text=True)


def ensure_repo():
    os.makedirs(REPO, exist_ok=True)
    if not os.path.isdir(os.path.join(REPO, ".git")):
        git("init", "-q")
        git("config", "user.email", "exporter@mypeople.local")
        git("config", "user.name", "mypeople-exporter")


def task_count(obj):
    try:
        return len(obj.get("tasks", {}))
    except Exception:
        return 0


def head_board():
    r = git("show", "HEAD:board.v2.json")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def canonical(obj):
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False)


def export_once():
    ensure_repo()
    live = read_live_board()
    if live is None:
        return "no_board"
    new_n = task_count(live)
    head = head_board()
    if head is not None:
        old_n = task_count(head)
        if old_n > 5 and new_n < 0.5 * old_n:
            # wipe suspected: quarantine, keep HEAD at last good full board (never promote)
            sp = os.path.join(REPO, "board.v2.json.SUSPECT.%d" % int(time.time()))
            with open(sp, "w") as f:
                f.write(canonical(live))
            git("add", "-A")
            git("commit", "-q", "-m", "SUSPECT shrink %d->%d quarantined" % (old_n, new_n))
            # notify boss (best-effort)
            try:
                subprocess.run(["mp", "send", "%s/main:Boss" % CFG["HOST_ID"],
                                "[board-backup] SUSPECT shrink quarantined (%d->%d)" % (old_n, new_n)],
                               capture_output=True, timeout=15)
            except Exception:
                pass
            return "quarantined"
    # normal snapshot
    dst = os.path.join(REPO, "board.v2.json")
    with open(dst, "w") as f:
        f.write(canonical(live))
    git("add", "-A")
    r = git("commit", "-q", "-m", "board snapshot %s tasks=%d" % (time.strftime("%FT%TZ", time.gmtime()), new_n))
    return "committed" if r.returncode == 0 else "nochange"


def main():
    if "--once" in sys.argv:
        print(export_once())
        return
    ensure_repo()
    watch = BS.db_path_for(BOARD_PATH) if BOARD_BACKEND == "sqlite" else BOARD_PATH
    # in WAL mode writes hit board.sqlite3-wal first, so stat both or changes go unnoticed until checkpoint
    paths = [watch, watch + "-wal"] if BOARD_BACKEND == "sqlite" else [watch]
    last_sig = None
    while True:
        try:
            parts = []
            for pth in paths:
                if os.path.exists(pth):
                    st = os.stat(pth)
                    parts.append((st.st_mtime, st.st_size))
            sig = tuple(parts) if parts else None
            if sig != last_sig:
                export_once()
                last_sig = sig
        except Exception:
            pass
        time.sleep(3)


if __name__ == "__main__":
    main()
