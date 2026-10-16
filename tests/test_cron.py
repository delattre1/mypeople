"""`mp cron`: a schedule is one `mp send` on a timer (card 0176b7cafd).

Pins the cron matcher, and that the timer delivers each scheduled minute exactly once: it retries
a busy agent inside the window, never re-sends a delivered minute, and never fires for a minute
that passed before the schedule existed.
"""
from datetime import datetime
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


def load_mp():
    sys.path.insert(0, str(BIN))
    try:
        loader = importlib.machinery.SourceFileLoader("mp_cli_cron", str(BIN / "mp"))
        spec = importlib.util.spec_from_loader("mp_cli_cron", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(BIN))


class CronTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mp = load_mp()

    def match(self, expr, *ymdhm):
        return self.mp.cron_match(self.mp.cron_parse(expr), datetime(*ymdhm))

    def test_matcher(self):
        # 2026-10-11 is a Sunday, 2026-10-12 a Monday.
        self.assertTrue(self.match("@hourly", 2026, 10, 11, 14, 0))
        self.assertFalse(self.match("@hourly", 2026, 10, 11, 14, 1))
        self.assertTrue(self.match("*/15 9-17 * * 1-5", 2026, 10, 12, 9, 45))
        self.assertFalse(self.match("*/15 9-17 * * 1-5", 2026, 10, 11, 9, 45))
        self.assertTrue(self.match("5/20 * * * *", 2026, 10, 11, 3, 45))
        self.assertTrue(self.match("0 0 * * 7", 2026, 10, 11, 0, 0))   # 7 is Sunday too
        # Both day fields restricted: either one matches (the 1st, and every Monday).
        self.assertTrue(self.match("0 9 1 * 1", 2026, 10, 12, 9, 0))
        self.assertFalse(self.match("0 9 1 * 1", 2026, 10, 11, 9, 0))
        # Only the weekday restricted: the day-of-month star does not widen it.
        self.assertFalse(self.match("0 9 * * 1", 2026, 10, 11, 9, 0))
        for bad in ("* * * *", "60 * * * *", "*/0 * * * *", "MON * * * *", "5-1 * * * *"):
            with self.assertRaises(ValueError, msg=bad):
                self.mp.cron_parse(bad)

    def test_fires_each_minute_once_with_retry_window(self):
        mp = self.mp
        created = datetime(2026, 10, 11, 13, 59, 30).timestamp()
        sends = []
        replies = []

        def fake_fire(s):
            sends.append(s["message"])
            return replies.pop(0)

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(mp, "CRON_PATH", os.path.join(d, "cron.json")), \
                mock.patch.object(mp, "CRON_STATE_PATH", os.path.join(d, "cron-state.json")), \
                mock.patch.object(mp, "CRON_LOG", os.path.join(d, "cron.log")), \
                mock.patch.object(mp, "cron_fire", fake_fire):
            mp.C.write_json(mp.CRON_PATH, {"a1": {"when": "0 * * * *", "agent": "main:eng-1",
                                                  "message": "scan", "created": created}})
            replies[:] = [(3, "not_ready")]
            mp.cron_tick(datetime(2026, 10, 11, 14, 0, 0))     # busy: refused, kept due
            replies[:] = [(0, "sent")]
            mp.cron_tick(datetime(2026, 10, 11, 14, 0, 15))    # retried: delivered
            mp.cron_tick(datetime(2026, 10, 11, 14, 3, 0))     # same 14:00 fire: not again
            mp.cron_tick(datetime(2026, 10, 11, 14, 30, 0))    # nothing scheduled
            self.assertEqual(sends, ["scan", "scan"])
            self.assertEqual(mp.C.read_json(mp.CRON_STATE_PATH)["a1"]["sent"], "2026-10-11 14:00")
            # A busy agent past the retry window misses that fire instead of getting it late.
            replies[:] = [(3, "not_ready")]
            mp.cron_tick(datetime(2026, 10, 11, 15, 0, 0))
            mp.cron_tick(datetime(2026, 10, 11, 15, mp.CRON_RETRY_MIN, 0))
            self.assertEqual(len(sends), 3)
            with open(mp.CRON_LOG) as f:
                self.assertEqual(len(f.read().splitlines()), 3)

    def test_a_run_names_who_set_it_and_how_to_reply_to_them(self):
        s = {"when": "0 * * * *", "agent": "main:eng-1", "message": "run the test scan",
             "for": "Dana", "reply": 'chat.py reply cht_x "your reply"'}
        self.assertEqual(self.mp.cron_package(s),
                         "[cronjob] Task Dana set up: run the test scan\n"
                         'When done, reply to Dana here: chat.py reply cht_x "your reply"')
        # A schedule made without either still says it is a cronjob, and no reply line is invented.
        self.assertEqual(self.mp.cron_package({"message": "scan"}),
                         "[cronjob] Task the owner set up: scan")

    def test_reply_channel_saved_at_add_and_changeable_later(self):
        mp = self.mp
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(mp, "CRON_PATH", os.path.join(d, "cron.json")):
            self.assertEqual(mp.do_cron(["add", "0 * * * *", "main:eng-1", "run the test scan",
                                         "--for", "Dana", "--reply", "chat.py reply cht_x"]), 0)
            (sid, s), = mp.C.read_json(mp.CRON_PATH).items()
            self.assertEqual((s["message"], s["for"], s["reply"]),
                             ("run the test scan", "Dana", "chat.py reply cht_x"))
            self.assertEqual(mp.do_cron(["set", sid, "--reply", "wa.py reply chat_y"]), 0)
            s = mp.C.read_json(mp.CRON_PATH)[sid]
            self.assertEqual((s["for"], s["reply"]), ("Dana", "wa.py reply chat_y"))
            self.assertEqual(mp.do_cron(["set", "nope", "--for", "x"]), 1)
            self.assertEqual(mp.do_cron(["set", sid]), 2)                # nothing to change

    def test_never_fires_for_a_minute_before_the_schedule_existed(self):
        parsed = self.mp.cron_parse("0 * * * *")
        created = datetime(2026, 10, 11, 14, 5, 0).timestamp()
        self.assertIsNone(self.mp.cron_due(parsed, created, datetime(2026, 10, 11, 14, 6, 0)))
        self.assertEqual(self.mp.cron_due(parsed, created, datetime(2026, 10, 11, 15, 0, 15)),
                         datetime(2026, 10, 11, 15, 0))


if __name__ == "__main__":
    unittest.main()
