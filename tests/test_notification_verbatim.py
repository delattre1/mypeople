"""Agent notifications carry the message whole (card 5676f76673).

Every ping used to be cut before delivery -- card text at 60, a comment body at 120, watchdog
quotes at 400 -- silently, with no ellipsis. The Boss then relayed a reason it had never fully
received: a 1932-char comment reached it as 120 chars. The transport was never the problem
(`mp send` and `tmux_send_message` pass a string through untouched), so these tests pin the
composed text, which is where the truncation actually lived.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"
BOSS = "test-node/main:Boss"

# Comfortably past every cap that used to be here (60, 120, 400).
LONG_TEXT = "T" + "he ask that used to be cut. " * 40      # ~1121 chars
LONG_BODY = "B" + "ody the Boss never received. " * 40     # ~1161 chars


def load_todo_server(home):
    state = Path(home) / "state"
    (state / "todos").mkdir(parents=True, exist_ok=True)
    config = Path(home) / "queue.env"
    config.write_text(
        'export INSTALL_DIR="%s"\n'
        'export HOST_ID="test-node"\n'
        'export QUEUE_URL="http://127.0.0.1:1"\n'
        'export QUEUE_SECRET="secret"\n'
        'export TTYD_PORT="7681"\n'
        'export HUD_PORT="9901"\n'
        'export TODO_PORT="9933"\n'
        'export DEFAULT_ENG_MODEL="claude-opus-4-8"\n' % state,
        encoding="utf-8",
    )
    with mock.patch.dict(os.environ, {
        "HOME": str(home), "MYPEOPLE_CONFIG_PATH": str(config), "MYPEOPLE_HOME": str(state),
        "INSTALL_DIR": str(state), "HOST_ID": "test-node", "QUEUE_URL": "http://127.0.0.1:1",
        "QUEUE_SECRET": "secret", "MYPEOPLE_BOARD_BACKEND": "json",
    }, clear=False):
        sys.modules.pop("mpcommon", None)
        sys.modules.pop("boardstore", None)
        sys.path.insert(0, str(BIN))
        try:
            name = "mypeople_test_verbatim_%s" % id(home)
            loader = importlib.machinery.SourceFileLoader(name, str(BIN / "todo-server.py"))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(BIN))


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
