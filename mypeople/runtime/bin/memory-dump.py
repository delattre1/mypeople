#!/usr/bin/env python3
"""memory-dump.py — dump the ENTIRE board (cards + comments) into one plain-text corpus file.

This is the "memory" half of the agent-memory feature (card 041d9f98ea). An agent that needs to
remember what the team already did regenerates this file on demand, loads it into a single Python
variable in its own session, and recalls by running code (find/regex/slice) over that string — the
RLM "context-as-variable" strategy. There is NO database, no embeddings, no server call: the corpus
is read straight from the local board store (SQLite or JSON, whichever this install uses).

Usage:
    python3 memory-dump.py            # write the corpus to $INSTALL_DIR/memory/board-corpus.txt
    python3 memory-dump.py --out PATH # write somewhere else
    python3 memory-dump.py --quiet    # only print the final path

Read-only with respect to the board: it never writes the board, only reads it.
"""
import os, sys, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpcommon as C
import boardstore as BS

CFG = C.CFG
INSTALL_DIR = CFG["INSTALL_DIR"]
BOARD_PATH = os.environ.get("BOARD_PATH", os.path.join(INSTALL_DIR, "todos", "board.v2.json"))


def read_board():
    """Read the live board whatever the storage engine (mirrors board-exporter.read_live_board)."""
    backend = BS.select_backend(BOARD_PATH, CFG.get("BOARD_BACKEND"))
    if backend == "sqlite":
        db = BS.db_path_for(BOARD_PATH)
        if not os.path.exists(db):
            return None
        return BS.load_board(db)
    return C.read_json(BOARD_PATH, None)


def build_corpus(board, comment_cap=4000):
    """One block per card: a header line (id/state/assignee) + TEXT + each comment. Newlines inside
    a field are flattened so every card/comment is greppable on a single logical line."""
    tasks = (board or {}).get("tasks", {}) or {}
    flat = lambda s: (s or "").replace("\r", " ").replace("\n", " ")
    out = []
    for tid, t in tasks.items():
        out.append("==== CARD %s [state=%s assignee=%s] ====" % (
            tid, t.get("state"), t.get("assignee")))
        out.append("TEXT: " + flat(t.get("text")))
        for c in (t.get("comments") or []):
            body = flat(c.get("body"))
            if len(body) > comment_cap:
                body = body[:comment_cap] + "…"
            out.append("  COMMENT by %s: %s" % (c.get("by"), body))
        out.append("")
    return "\n".join(out), len(tasks)


def main():
    ap = argparse.ArgumentParser(description="Dump the whole board into a text corpus for agent memory.")
    ap.add_argument("--out", default=os.path.join(INSTALL_DIR, "memory", "board-corpus.txt"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    board = read_board()
    if board is None:
        print("ERROR: could not read the board store (looked for %s)" % BOARD_PATH, file=sys.stderr)
        return 1
    corpus, ncards = build_corpus(board)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    C.atomic_write(args.out, corpus.encode("utf-8"))

    if args.quiet:
        print(os.path.abspath(args.out))
    else:
        print("wrote %s (%d cards, %d chars)" % (os.path.abspath(args.out), ncards, len(corpus)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
