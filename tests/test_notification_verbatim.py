"""Agent notifications carry the message whole (card 5676f76673).

Every ping used to be cut before delivery -- card text at 60, a comment body at 120, watchdog
quotes at 400 -- silently, with no ellipsis. The Boss then relayed a reason it had never fully
received: a 1932-char comment reached it as 120 chars. The transport was never the problem
(`mp send` and `tmux_send_message` pass a string through untouched), so these tests pin the
composed text, which is where the truncation actually lived.
"""
import tempfile
import time
import unittest
from unittest import mock
import sandbox


BOSS = "test-node/main:Boss"

# Comfortably past every cap that used to be here (60, 120, 400).
LONG_TEXT = "T" + "he ask that used to be cut. " * 40      # ~1121 chars
LONG_BODY = "B" + "ody the Boss never received. " * 40     # ~1161 chars


def load_todo_server(home):
    return sandbox.load(home, "todo-server.py", MYPEOPLE_BOARD_BACKEND="json")


class NotificationVerbatimTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ts = load_todo_server(self._td.name)
        self.task = {"id": "c1", "state": "working", "assignee": "", "text": LONG_TEXT,
                     "comments": []}

    def test_a_comment_ping_carries_the_whole_body_and_title(self):
        with mock.patch.object(self.ts, "ping_boss") as ping:
            self.ts.emit_comment_event({}, self.task, "test-node/main:eng-1", LONG_BODY)
        sent = ping.call_args[0][0]
        self.assertIn(LONG_BODY, sent)
        self.assertIn(LONG_TEXT, sent)

    def test_a_task_event_ping_carries_the_whole_title(self):
        with mock.patch.object(self.ts, "ping_boss") as ping:
            self.ts.emit_task_event({}, self.task, "new task added")
        self.assertIn(LONG_TEXT, ping.call_args[0][0])

    def test_a_watchdog_unanswered_incident_quotes_the_whole_comment(self):
        self.task["comments"] = [{"id": "x1", "by": "CEO", "kind": "comment",
                                  "body": LONG_BODY, "ts": time.time()}]
        job = {"kind": "unanswered", "card": "c1", "comment_id": "x1", "by": "CEO"}
        self.assertIn(LONG_BODY, self.ts.wd_incident_text(job, self.task))

    def test_a_watchdog_unowned_incident_carries_the_whole_card_text(self):
        job = {"kind": "unowned", "card": "c1", "by": "CEO"}
        self.assertIn(LONG_TEXT, self.ts.wd_incident_text(job, self.task))

    def test_a_watchdog_silent_owner_incident_carries_the_whole_card_text(self):
        self.task["assignee"] = "test-node/main:eng-1"
        job = {"kind": "unowned", "card": "c1", "by": "CEO"}
        self.assertIn(LONG_TEXT, self.ts.wd_incident_text(job, self.task))


if __name__ == "__main__":
    unittest.main()
