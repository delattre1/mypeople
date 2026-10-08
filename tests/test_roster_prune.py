"""roster-prune shrinks the file without losing a kill tombstone (card e949527956).

The dangerous version of this job is deleting retired entries. do_revive treats a MISSING
entry as a new node's first Boss and spawns it with --master, so deleting a tombstone would
turn `mp revive <long-dead-agent>` from "deliberately retired; not respawning" into spawning a
second Boss. These tests pin that every key survives and only restore-only payload is dropped.
"""
import glob
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
unittest.mock = mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"
DAY = 86400


def load_prune():
    sys.path.insert(0, str(BIN))
    try:
        loader = importlib.machinery.SourceFileLoader("roster_prune", str(BIN / "roster-prune.py"))
        spec = importlib.util.spec_from_loader("roster_prune", loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        return m
    finally:
        sys.path.remove(str(BIN))


class PrunableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = load_prune()

    def slim(self, **entry):
        return self.p.prunable(entry, time.time(), 7)

    def test_a_live_agent_is_never_touched(self):
        self.assertFalse(self.slim(retired=False))

    def test_a_revivable_retirement_is_never_touched(self):
        """Accidental deaths are the only ones revive can restore -- they keep every field."""
        self.assertFalse(self.slim(retired=True, retire_reason="died-no-window",
                                   retired_ts=time.time() - 90 * DAY))

    def test_a_recent_retirement_is_never_touched(self):
        self.assertFalse(self.slim(retired=True, retire_reason="killed",
                                   retired_ts=time.time() - 2 * DAY))

    def test_an_entry_with_no_timestamp_is_never_touched(self):
        """No age evidence, no action -- guessing here would drop a live agent's history."""
        self.assertFalse(self.slim(retired=True, retire_reason="killed"))

    def test_an_old_deliberate_retirement_is_pruned(self):
        self.assertTrue(self.slim(retired=True, retire_reason="killed",
                                  retired_ts=time.time() - 30 * DAY))

    def test_a_retired_boss_is_never_pruned(self):
        """do_ensure_boss treats a MISSING Boss entry as a new node's first Boss and spawns one
        with --master. Removing a Boss tombstone would manufacture a second Boss."""
        self.assertFalse(self.slim(retired=True, retire_reason="killed", is_master=True,
                                   retired_ts=time.time() - 30 * DAY))


class PruneRunTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.install = self._td.name
        os.makedirs(os.path.join(self.install, "run"))
        self.roster_path = os.path.join(self.install, "run", "roster.json")
        old = time.time() - 30 * DAY
        self.roster = {
            "n/main:live": {"agent_id": "n/main:live", "retired": False, "spawn_cmd": "x" * 500},
            "n/main:old": {"agent_id": "n/main:old", "session": "main", "tab": "old",
                           "retired": True, "retire_reason": "killed", "retired_ts": old,
                           "is_master": False, "spawn_cmd": "y" * 5000,
                           "role_bundle": {"big": "z" * 5000}, "session_id": "s-123"},
            "n/main:crashed": {"agent_id": "n/main:crashed", "retired": True,
                               "retire_reason": "died-no-window", "retired_ts": old,
                               "spawn_cmd": "w" * 5000},
        }
        Path(self.roster_path).write_text(json.dumps(self.roster), encoding="utf-8")
        self.p = load_prune()

    def run_prune(self, *extra):
        argv = ["roster-prune.py", "--install-dir", self.install] + list(extra)
        with unittest.mock.patch.object(sys, "argv", argv), \
             unittest.mock.patch("sys.stdout", new=open(os.devnull, "w")):
            return self.p.main()

    def result(self):
        return json.loads(Path(self.roster_path).read_text())

    def test_an_old_deliberate_entry_is_removed(self):
        self.run_prune()
        self.assertNotIn("n/main:old", self.result())

    def test_live_and_revivable_entries_are_kept_whole(self):
        self.run_prune()
        r = self.result()
        self.assertIn("spawn_cmd", r["n/main:live"])
        self.assertIn("spawn_cmd", r["n/main:crashed"])

    def test_a_retired_boss_is_kept(self):
        self.roster["n/main:Boss"] = {"agent_id": "n/main:Boss", "retired": True,
                                      "retire_reason": "killed", "is_master": True,
                                      "retired_ts": time.time() - 30 * DAY}
        Path(self.roster_path).write_text(json.dumps(self.roster), encoding="utf-8")
        self.run_prune()
        self.assertIn("n/main:Boss", self.result())

    def test_the_full_entry_is_archived_before_slimming(self):
        self.run_prune()
        arc = json.loads(Path(os.path.join(self.install, "run", "roster-archive.json")).read_text())
        self.assertEqual(arc["n/main:old"], self.roster["n/main:old"])

    def test_a_timestamped_backup_is_written(self):
        self.run_prune()
        self.assertTrue(glob.glob(self.roster_path + ".bak-prune-*"))

    def test_dry_run_changes_nothing(self):
        self.run_prune("--dry-run")
        self.assertEqual(self.result(), self.roster)
        self.assertFalse(glob.glob(self.roster_path + ".bak-prune-*"))


if __name__ == "__main__":
    unittest.main()
