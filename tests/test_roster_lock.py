"""roster.json survives concurrent writers (card e949527956).

The file was read-modify-write with no mutual exclusion. Writes were atomic (no torn file)
but twelve writers -- eleven save_roster sites in bin/mp plus queue-client's 10s heartbeat --
meant last-writer-wins. An `mp kill` marking retired=True was erased by a heartbeat that had
read the file ~15ms earlier, and reconcile then correctly revived what looked like a crashed
agent: eng-554 came back twice. eng-555's spawn entry vanished the same way, leaving a live
agent the fleet had no record of.

Locking only the write would not have helped -- the stale read already happened -- so these
tests drive real concurrent PROCESSES through the read+merge+write transaction.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"

# Each worker: tracked read, dawdle (widening the window the bug needed), mutate ONE key, write.
WORKER = r'''
import sys, time
sys.path.insert(0, %r)
import mpcommon as C
path, key, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
r = C.read_json_tracked(path, {})
time.sleep(delay)
r[key] = {"agent_id": key, "retired": False}
C.write_json_merged(path, r)
'''  % str(BIN)

KILLER = r'''
import sys, time
sys.path.insert(0, %r)
import mpcommon as C
path, victim, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
r = C.read_json_tracked(path, {})
time.sleep(delay)
r[victim]["retired"] = True
r[victim]["retire_reason"] = "killed"
C.write_json_merged(path, r)
'''  % str(BIN)

HEARTBEAT = r'''
import sys, time
sys.path.insert(0, %r)
import mpcommon as C
path, delay = sys.argv[1], float(sys.argv[2])
r = C.read_json_tracked(path, {})          # reads BEFORE the kill lands
time.sleep(delay)
r["_heartbeat"] = {"touched": True}        # unrelated change, then writes its stale copy
C.write_json_merged(path, r)
'''  % str(BIN)


class RosterLockTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.path = os.path.join(self._td.name, "roster.json")

    def write(self, obj):
        Path(self.path).write_text(json.dumps(obj), encoding="utf-8")

    def read(self):
        return json.loads(Path(self.path).read_text())

    def run_all(self, procs):
        running = [subprocess.Popen([sys.executable, "-c", src] + [str(a) for a in args])
                   for src, args in procs]
        for p in running:
            self.assertEqual(p.wait(timeout=60), 0)

    def test_two_concurrent_writers_both_survive(self):
        """The plain lost update: each adds a different agent, overlapping."""
        self.write({"existing": {"agent_id": "existing"}})
        self.run_all([
            (WORKER, [self.path, "agent-A", 0.6]),
            (WORKER, [self.path, "agent-B", 0.3]),
        ])
        r = self.read()
        self.assertIn("agent-A", r)
        self.assertIn("agent-B", r)
        self.assertIn("existing", r)

    def test_a_kill_is_not_erased_by_an_overlapping_heartbeat(self):
        """The eng-554 case exactly: the heartbeat read first, the kill wrote first, the
        heartbeat wrote last. retired=True must survive -- reconcile reads ONLY that flag to
        tell a deliberate stop from a crash, and revives anything that is not retired."""
        self.write({"eng-554": {"agent_id": "eng-554", "retired": False}})
        self.run_all([
            (HEARTBEAT, [self.path, 0.8]),          # reads now, writes last
            (KILLER, [self.path, "eng-554", 0.3]),  # reads now, writes first
        ])
        entry = self.read()["eng-554"]
        self.assertTrue(entry["retired"], "the kill was erased -- reconcile would revive it")
        self.assertEqual(entry["retire_reason"], "killed")
        self.assertIn("_heartbeat", self.read(), "the heartbeat's own change must survive too")

    def test_a_spawn_is_not_erased_by_an_overlapping_heartbeat(self):
        """The eng-555 case: a new agent added while the heartbeat held an older copy."""
        self.write({"eng-550": {"agent_id": "eng-550"}})
        self.run_all([
            (HEARTBEAT, [self.path, 0.8]),
            (WORKER, [self.path, "eng-555", 0.3]),
        ])
        self.assertIn("eng-555", self.read(), "the spawn was erased -- agent invisible to the fleet")

    def test_many_concurrent_writers_lose_nothing(self):
        self.write({})
        self.run_all([(WORKER, [self.path, "agent-%02d" % i, 0.1 + (i % 5) * 0.1])
                      for i in range(12)])
        r = self.read()
        self.assertEqual(sorted(k for k in r if k.startswith("agent-")),
                         sorted("agent-%02d" % i for i in range(12)))

    def test_a_deletion_still_deletes(self):
        """The merge must not resurrect an entry this process intentionally removed."""
        self.write({"keep": {"a": 1}, "drop": {"a": 2}})
        src = r'''
import sys
sys.path.insert(0, %r)
import mpcommon as C
r = C.read_json_tracked(sys.argv[1], {})
del r["drop"]
C.write_json_merged(sys.argv[1], r)
''' % str(BIN)
        self.run_all([(src, [self.path])])
        r = self.read()
        self.assertIn("keep", r)
        self.assertNotIn("drop", r)


if __name__ == "__main__":
    unittest.main()
