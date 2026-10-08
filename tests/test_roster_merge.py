"""write_json_merged: a deletion needs this process's own tracked read, consumed by one write.

Card f864c568e6: a caller that wrote a partial dict twice in one process deleted every key it had
not mentioned -- roster.json lost 14 agents and their roles in a relaunch loop.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"))
import mpcommon as C  # noqa: E402


class MergeTest(unittest.TestCase):
    def setUp(self):
        C._JSON_SNAPSHOTS.clear()   # no tracked read carried over from another test
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "roster.json")
        C.write_json(self.path, {"boss": 1, "eng-1": 1, "eng-2": 1})

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return set(json.load(open(self.path)))

    def test_a_partial_dict_written_twice_deletes_nothing(self):
        C.write_json_merged(self.path, {"discord": 1})
        C.write_json_merged(self.path, {"discord": 2})      # the relaunch loop
        self.assertEqual(self.rows(), {"boss", "eng-1", "eng-2", "discord"})

    def test_after_a_full_write_a_partial_one_still_deletes_nothing(self):
        r = C.read_json_tracked(self.path, {})
        r["eng-1"] = 2
        C.write_json_merged(self.path, r)
        C.write_json_merged(self.path, {"discord": 1})      # the old snapshot would pop the rest
        self.assertEqual(self.rows(), {"boss", "eng-1", "eng-2", "discord"})

    def test_a_tracked_read_then_write_still_deletes_what_it_removed(self):
        r = C.read_json_tracked(self.path, {})
        del r["eng-2"]                                      # roster-prune's case
        C.write_json_merged(self.path, r)
        self.assertEqual(self.rows(), {"boss", "eng-1"})

    def test_another_writers_new_row_survives(self):
        r = C.read_json_tracked(self.path, {})
        C.write_json(self.path, dict(json.load(open(self.path)), **{"eng-3": 1}))   # heartbeat
        r["boss"] = 2
        C.write_json_merged(self.path, r)
        self.assertEqual(self.rows(), {"boss", "eng-1", "eng-2", "eng-3"})


if __name__ == "__main__":
    unittest.main()
