#!/usr/bin/env python3
"""Shrink run/roster.json without losing a single kill tombstone (card e949527956).

The roster grows forever: retired agents are never removed, and at 780 entries / 971 KB every
read-modify-write took ~15 ms. That width is what let a heartbeat clobber an `mp kill`.

Entries removed here are **deliberately retired, older than the retention window, and not a
Boss**. Nothing can legitimately bring them back:

  * `do_revive` needs the entry (`if not rr: "unknown agent"`), so a removed agent is refused
    rather than respawned. Left in place, an ancient agent with a surviving transcript would
    actually be RE-SPAWNED by `mp revive`, which is worse.
  * `do_reconcile` iterates the roster, so a removed entry is never a revive candidate.
  * `is_master` entries are never removed: `do_ensure_boss` treats a missing Boss entry as a
    new node's first Boss and would spawn one with `--master`. That is the one real hazard
    here, and it is guarded explicitly.

Accidentally-retired agents (the revivable kind) and anything inside the retention window are
never touched, so `mp revive` keeps working for everything retired recently. The full original
entries are copied to run/roster-archive.json first, so the history stays queryable.

Usage: roster-prune.py [--retention-days N] [--dry-run] [--install-dir DIR]
"""
import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mpcommon as C  # noqa: E402

# Mirrors ACCIDENTAL_REASONS in bin/mp: a death the system observed, so still revivable.
ACCIDENTAL_REASONS = frozenset({"died-no-window", "tmux-server-crash", "window-missing",
                                "tmux-window-vanished", "boss-window-missing"})



def prunable(entry, now, retention_days):
    if not entry.get("retired"):
        return False
    if entry.get("is_master"):
        return False                      # a missing Boss entry makes ensure-boss spawn a new one
    if entry.get("retire_reason") in ACCIDENTAL_REASONS:
        return False                      # revivable: never touch
    ts = entry.get("retired_ts")
    if not ts:
        return False                      # no age evidence: leave it alone
    return (now - float(ts)) > retention_days * 86400


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retention-days", type=float, default=7)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--install-dir", default=C.CFG.get("INSTALL_DIR"))
    a = ap.parse_args()

    roster_path = os.path.join(a.install_dir, "run", "roster.json")
    archive_path = os.path.join(a.install_dir, "run", "roster-archive.json")
    now = time.time()

    roster = C.read_json_tracked(roster_path, {}) or {}
    before = len(json.dumps(roster, ensure_ascii=False))
    targets = {k: v for k, v in roster.items() if prunable(v, now, a.retention_days)}

    print("roster : %d entries, %.0f KB" % (len(roster), before / 1024))
    print("prune  : %d entries (deliberate, non-Boss, retired over %g days ago)"
          % (len(targets), a.retention_days))
    print("keep   : %d entries (live, recent, revivable, or Boss)"
          % (len(roster) - len(targets)))
    if not targets:
        print("nothing to do"); return 0

    pruned = {k: v for k, v in roster.items() if k not in targets}
    after = len(json.dumps(pruned, ensure_ascii=False))
    print("result : %.0f KB -> %.0f KB (%.1fx smaller)" % (before / 1024, after / 1024,
                                                           before / max(after, 1)))

    if a.dry_run:
        print("(dry run; nothing written)"); return 0

    backup = "%s.bak-prune-%d" % (roster_path, int(now))
    shutil.copy2(roster_path, backup)
    print("backup : %s" % backup)

    archive = C.read_json(archive_path, {}) or {}
    archive.update(targets)                      # FULL original entries
    C.write_json(archive_path, archive)
    print("archive: %s (%d entries, full detail)" % (archive_path, len(archive)))

    C.write_json_merged(roster_path, pruned)     # locked: concurrent writers are not clobbered
    print("written: %s" % roster_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
