"""cloud/pool-lease.py reads its own Claude logs right: usage once per message, limits detected, and
the Boss restarted first (its resume is the proof a new login works)."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pool_lease", ROOT / "cloud" / "pool-lease.py")
pl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pl)


def turn(ts, mid, usage=None, error=None):
    m = {"id": mid, "usage": usage or {}}
    d = {"type": "assistant", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)), "message": m}
    if error:
        m["content"] = [{"type": "text", "text": error}]
        d["isApiErrorMessage"] = True
    return json.dumps(d)


class SeenSince(unittest.TestCase):
    def setUp(self):
        self.projects = Path(tempfile.mkdtemp())
        (self.projects / "p").mkdir()
        self.log = self.projects / "p" / "s.jsonl"

    def write(self, *lines):
        self.log.write_text("\n".join(lines) + "\n")

    def test_counts_each_message_once_and_only_after_the_lease_began(self):
        start = time.time() - 100
        u = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}
        self.write(turn(start - 50, "old", u),                       # before the lease: not ours
                   turn(start + 10, "m1", u), turn(start + 11, "m1", u),   # one message logged twice
                   turn(start + 20, "m2", {"output_tokens": 7}), "not json")
        usage, limit = pl.seen_since(start, self.projects)
        self.assertEqual(usage, {"input": 10, "output": 12, "cache_read": 100, "cache_write": 0})
        self.assertFalse(limit)

    def test_a_usage_limit_turn_is_seen_and_an_unrelated_error_is_not(self):
        start = time.time() - 100
        self.write(turn(start + 5, "e1", error="API Error: 500 Internal server error"))
        self.assertFalse(pl.seen_since(start, self.projects)[1])
        self.write(turn(start + 5, "e2", error="Claude usage limit reached. Your limit will reset at 3pm."))
        self.assertTrue(pl.seen_since(start, self.projects)[1])
        self.write(turn(start - 60, "e3", error="5-hour limit reached"))   # before this lease: not ours
        self.assertFalse(pl.seen_since(start, self.projects)[1])


class Agents(unittest.TestCase):
    def test_boss_first_live_claude_only(self):
        d = Path(tempfile.mkdtemp())
        pl.ROSTER = d / "roster.json"
        pl.ROSTER.write_text(json.dumps({
            "n/main:eng-1": {"backend": "claude"},
            "n/main:Boss": {"backend": "claude", "is_master": True},
            "n/main:eng-2": {"backend": "codex"},
            "n/main:eng-3": {"backend": "claude", "retired": True},
        }))
        self.assertEqual(pl.claude_agents(), ["n/main:Boss", "n/main:eng-1"])


class OnlyItsOwnRelease(unittest.TestCase):
    """A switch or rollback must not leave this release's lease loop running next to the next one's."""

    def test_exits_when_current_points_elsewhere_or_it_was_orphaned(self):
        root = Path(tempfile.mkdtemp())
        a, b = root / "releases" / "5.0.1", root / "releases" / "5.0.2"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (root / "current").symlink_to("releases/5.0.1")
        self.assertTrue(pl.still_mine(own=a.resolve(), root=root, ppid=4242))
        self.assertFalse(pl.still_mine(own=a.resolve(), root=root, ppid=1))      # run.sh is gone
        (root / "current").unlink()
        (root / "current").symlink_to("releases/5.0.2")                         # switched
        self.assertFalse(pl.still_mine(own=a.resolve(), root=root, ppid=4242))
        self.assertTrue(pl.still_mine(own=b.resolve(), root=root, ppid=4242))


if __name__ == "__main__":
    unittest.main()
