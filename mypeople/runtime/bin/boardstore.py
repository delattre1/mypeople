#!/usr/bin/env python3
"""boardstore — SQLite storage engine for the MyPlow TODO board.

Drop-in replacement for todo-server's JSON persistence (board.v2.json). Preserves the exact
load/save contract the REST handlers rely on:

  * load_board(db_path) -> a FRESH board dict (identical shape to the JSON board). Fresh means
    un-saved mutations by a caller never leak into the next load — the todo-server relies on this
    (e.g. rejection paths mutate the in-memory board then return WITHOUT save_board()).
  * save_board(db_path, board) -> bool. Atomic (single transaction), row-per-card so only the
    cards that actually changed are written — no 10 MB whole-file rewrite per comment.

Design (lossless by construction):
  * Each card is one row in `tasks`, storing the COMPLETE task object as JSON in `data`
    (comments/proofs/ownerHistory nested, exactly as in the JSON board). Hot fields are mirrored
    into indexed columns (state, assignee, pinned, pin_rank, created, updated, done, verified)
    purely for fast queries — `data` remains the source of truth, so no field is ever dropped.
  * Top-level board keys other than `tasks` (version, order, pinSeq, orderView, and any unknown
    key) live in `meta` as JSON. `__task_order__` records the tasks-dict key order so the dict
    round-trips in its original order.
  * A per-card content hash drives change detection: save_board upserts only cards whose hash
    changed and deletes cards no longer present. WAL + synchronous=FULL matches the durability of
    the old fsync JSON writer. foreign_keys ON per the storage contract.

stdlib only (sqlite3, json, hashlib, os, time). No new dependencies.
"""
import os
import json
import time
import hashlib
import sqlite3

SCHEMA_VERSION = 1
CHILD_KEYS = ("comments", "proofs", "ownerHistory")  # informational; children stay nested in data

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id        TEXT PRIMARY KEY,
    state     TEXT,
    assignee  TEXT,
    pinned    INTEGER,
    pin_rank  INTEGER,
    created   REAL,
    updated   REAL,
    done      INTEGER,
    verified  INTEGER,
    hash      TEXT NOT NULL,
    data      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_state    ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
CREATE INDEX IF NOT EXISTS idx_tasks_pinned   ON tasks(pinned);
CREATE INDEX IF NOT EXISTS idx_tasks_updated  ON tasks(updated);
"""


def default_board():
    return {"version": 2, "order": [], "pinSeq": 0, "tasks": {}}


def db_path_for(board_path):
    """Derive the SQLite path from a board.v2.json path, so callers that only know BOARD_PATH
    (including the existing test harness that overrides module.BOARD_PATH) route to the right DB."""
    if board_path.endswith(".json"):
        return board_path[:-len(".json")] + ".sqlite3"
    return board_path + ".sqlite3"


def select_backend(board_path, cfg_value=None):
    """Pick the board storage engine: "sqlite" or "json".

    Every board daemon (todo-server, board-exporter, board-restore) MUST route through this, or they
    disagree about where the board lives -- e.g. the server writing SQLite while the exporter reads
    JSON, which silently stops backups.

    Explicit choice wins: env (tests/manual) then the persistent queue.env value. With no explicit
    choice a fresh install gets sqlite, but an install that already has a JSON board and no SQLite db
    stays on json -- switching it silently would present an empty board, reading as total data loss.
    Convert such a board with migrate_board.py (reversible).
    """
    choice = (os.environ.get("MYPEOPLE_BOARD_BACKEND") or cfg_value or "").strip().lower()
    if choice:
        return choice
    if os.path.exists(board_path) and not is_initialized(db_path_for(board_path)):
        return "json"
    return "sqlite"


def connect(db_path):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)  # autocommit; we manage txns
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('__schema__',?) "
        "ON CONFLICT(key) DO NOTHING", (json.dumps(SCHEMA_VERSION),))
    return conn


def _canonical(obj):
    """Stable serialization for hashing only (key order irrelevant to identity)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def task_hash(task):
    return hashlib.sha1(_canonical(task).encode("utf-8")).hexdigest()


def _to_int(v):
    return 1 if v else 0


def _row_for(task):
    """Mirror hot fields for indexing; `data` keeps the whole object verbatim (key order preserved)."""
    return {
        "id": task.get("id"),
        "state": task.get("state"),
        "assignee": task.get("assignee", ""),
        "pinned": _to_int(task.get("pinned")),
        "pin_rank": task.get("pinRank"),
        "created": task.get("created"),
        "updated": task.get("updated"),
        "done": _to_int(task.get("done")),
        "verified": _to_int(task.get("verified")),
        "hash": task_hash(task),
        "data": json.dumps(task, ensure_ascii=False),  # preserve original key order
    }


def is_initialized(db_path):
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False


def load_board(db_path):
    """Reconstruct a FRESH board dict from SQLite. Returns default_board() if empty/uninitialized."""
    if not os.path.exists(db_path):
        return default_board()
    conn = connect(db_path)
    try:
        meta = {row[0]: row[1] for row in conn.execute("SELECT key,value FROM meta")}
        # top-level board keys (skip internal __*__ markers)
        board = {}
        for k, v in meta.items():
            if k.startswith("__") and k.endswith("__"):
                continue
            board[k] = json.loads(v)
        # tasks, reconstructed in original insertion order
        rows = {tid: data for tid, data in conn.execute("SELECT id,data FROM tasks")}
        order = json.loads(meta["__task_order__"]) if "__task_order__" in meta else list(rows.keys())
        tasks = {}
        for tid in order:
            if tid in rows:
                tasks[tid] = json.loads(rows[tid])
        # include any task rows missing from the order list (defensive; keeps them, never drops)
        for tid, data in rows.items():
            if tid not in tasks:
                tasks[tid] = json.loads(data)
        board["tasks"] = tasks
        # match the JSON load_board normalization
        board.setdefault("order", [])
        board.setdefault("pinSeq", 0)
        board.setdefault("tasks", {})
        if not board:  # totally empty db
            return default_board()
        return board
    finally:
        conn.close()


def save_board(db_path, board, shrink_guard=True):
    """Persist `board` to SQLite atomically. Only changed/added/removed cards are written.

    Mirrors todo-server.save_board semantics: catastrophic-shrink guard (writes a .SUSPECT.<ts>
    JSON dump and refuses if the card count more than halves), returns True on success / False on
    a refused shrink."""
    if not isinstance(board, dict):
        return False
    new_tasks = board.get("tasks", {})
    if not isinstance(new_tasks, dict):
        new_tasks = {}
    conn = connect(db_path)
    try:
        existing = {tid: h for tid, h in conn.execute("SELECT id,hash FROM tasks")}
        old_n, new_n = len(existing), len(new_tasks)
        if shrink_guard and old_n > 5 and new_n < 0.5 * old_n:
            sp = "%s.SUSPECT.%d" % (db_path, int(time.time()))
            with open(sp, "w") as fh:
                json.dump(board, fh, ensure_ascii=False)
            import sys
            sys.stderr.write("SUSPECT shrink refused: %d->%d, wrote %s\n" % (old_n, new_n, sp))
            return False

        conn.execute("BEGIN IMMEDIATE")
        # upsert changed / new cards
        for tid, task in new_tasks.items():
            r = _row_for(task)
            if existing.get(tid) == r["hash"]:
                continue  # unchanged card — skip write
            conn.execute(
                "INSERT INTO tasks(id,state,assignee,pinned,pin_rank,created,updated,done,verified,hash,data)"
                " VALUES(:id,:state,:assignee,:pinned,:pin_rank,:created,:updated,:done,:verified,:hash,:data)"
                " ON CONFLICT(id) DO UPDATE SET state=:state,assignee=:assignee,pinned=:pinned,"
                " pin_rank=:pin_rank,created=:created,updated=:updated,done=:done,verified=:verified,"
                " hash=:hash,data=:data", r)
        # delete cards no longer present
        for tid in existing:
            if tid not in new_tasks:
                conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
        # rewrite meta (tiny): every top-level board key except `tasks`, plus internal markers
        conn.execute("DELETE FROM meta")
        conn.execute("INSERT INTO meta(key,value) VALUES('__schema__',?)", (json.dumps(SCHEMA_VERSION),))
        conn.execute("INSERT INTO meta(key,value) VALUES('__task_order__',?)",
                     (json.dumps(list(new_tasks.keys())),))
        for k, v in board.items():
            if k == "tasks":
                continue
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
        conn.execute("COMMIT")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()
