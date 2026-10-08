#!/usr/bin/env python3
"""migrate_board — reversible one-shot migrator between board.v2.json and the SQLite BoardStore.

  migrate_board.py to-sqlite <board.v2.json> <board.sqlite3>   # JSON  -> SQLite
  migrate_board.py to-json   <board.sqlite3> <board.v2.json>   # SQLite -> JSON  (rollback)
  migrate_board.py verify    <board.v2.json>                   # prove JSON->SQLite->JSON lossless

`verify` does a full round-trip through a throwaway DB and deep-compares against the original,
reporting task / comment / proof / ownerHistory counts and any diff. Exit 0 iff lossless.
stdlib only."""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boardstore as B


def _read_json(path):
    with open(path) as fh:
        return json.load(fh)


def _counts(board):
    tasks = board.get("tasks", {})
    c = p = h = 0
    for t in tasks.values():
        c += len(t.get("comments", []) or [])
        p += len(t.get("proofs", []) or [])
        h += len(t.get("ownerHistory", []) or [])
    return {"tasks": len(tasks), "comments": c, "proofs": p, "ownerHistory": h,
            "order": len(board.get("order", [])), "pinSeq": board.get("pinSeq")}


def to_sqlite(json_path, db_path, fresh=True):
    if fresh and os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
    board = _read_json(json_path)
    ok = B.save_board(db_path, board, shrink_guard=False)
    if not ok:
        raise SystemExit("save_board refused the migration")
    return board


def to_json(db_path, json_path):
    board = B.load_board(db_path)
    tmp = json_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(board, fh, ensure_ascii=False)
    os.replace(tmp, json_path)
    return board


def _diff_paths(a, b, path="$"):
    """Yield human-readable difference locations between two JSON-ish structures (order-sensitive
    for lists, key-set-sensitive for dicts). Empty generator == deeply equal."""
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))):
        yield "%s: type %s != %s" % (path, type(a).__name__, type(b).__name__)
        return
    if isinstance(a, dict):
        ka, kb = set(a), set(b)
        for k in ka - kb:
            yield "%s: key %r only in A" % (path, k)
        for k in kb - ka:
            yield "%s: key %r only in B" % (path, k)
        for k in ka & kb:
            yield from _diff_paths(a[k], b[k], "%s.%s" % (path, k))
    elif isinstance(a, list):
        if len(a) != len(b):
            yield "%s: list len %d != %d" % (path, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            yield from _diff_paths(x, y, "%s[%d]" % (path, i))
    else:
        if a != b:
            yield "%s: %r != %r" % (path, a, b)


def verify(json_path):
    original = _read_json(json_path)
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "roundtrip.sqlite3")
        to_sqlite(json_path, db)
        restored = B.load_board(db)
    oc, rc = _counts(original), _counts(restored)
    print("original :", json.dumps(oc))
    print("roundtrip:", json.dumps(rc))
    diffs = list(_diff_paths(original, restored))
    # strict deep equality (dict key order ignored, list order enforced)
    deep_equal = (original == restored)
    if diffs:
        print("DIFFS (first 40):")
        for d in diffs[:40]:
            print("  " + d)
    print("counts_match:", oc == rc)
    print("deep_equal  :", deep_equal)
    lossless = deep_equal and not diffs and oc == rc
    print("LOSSLESS    :", lossless)
    return 0 if lossless else 1


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "to-sqlite" and len(argv) == 4:
        board = to_sqlite(argv[2], argv[3])
        print("migrated -> %s (%d tasks)" % (argv[3], len(board.get("tasks", {}))))
        return 0
    if cmd == "to-json" and len(argv) == 4:
        board = to_json(argv[2], argv[3])
        print("exported -> %s (%d tasks)" % (argv[3], len(board.get("tasks", {}))))
        return 0
    if cmd == "verify" and len(argv) == 3:
        return verify(argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
