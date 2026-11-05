"""A task's thread shows several agents: mp send --card puts the message on the card as sender -> receiver.

The board takes a message from another MyPlow only on a card that already addressed that MyPlow, so a paired
peer cannot write itself into a card by guessing its id. A thread line pings nobody: mp send delivered it.
"""
import tempfile
import unittest
from unittest import mock

import sandbox


class CardThreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.srv = sandbox.load(self.tmp.name, "todo-server.py")
        with self.srv.LOCK:
            b = self.srv.load_board()
            b["tasks"]["c1"] = {"id": "c1", "text": "t", "state": "working", "assignee": "test-node/main:eng-7", "comments": [], "proofs": []}
            self.srv.save_board(b)
        p = mock.patch.object(self.srv, "emit_comment_event")
        self.emit = p.start()
        self.addCleanup(p.stop)

    def post(self, ident="machine", **body):
        h, out = mock.Mock(), {}
        h._send = lambda code, data: out.update(data) or None
        self.srv.Handler._comment(h, dict(task_id="c1", body="hello", **body), ident)
        return out

    def thread(self):
        return self.srv.load_board()["tasks"]["c1"]["comments"]

    def test_a_local_line_names_both_agents_and_pings_nobody(self):
        self.assertTrue(self.post(by="test-node/main:eng-1", to="test-node/main:eng-2")["ok"])
        c = self.thread()[-1]
        self.assertEqual((c["by"], c["to"], c.get("toPeer")), ("test-node/main:eng-1", "test-node/main:eng-2", None))
        self.emit.assert_not_called()

    def test_a_peer_lands_only_on_a_card_that_addressed_it(self):
        self.assertEqual(self.post(by="beta/main:x", to="test-node/main:eng-1", peer="beta")["error"], "not_on_card")
        self.assertEqual(self.thread(), [])
        self.post(by="test-node/main:eng-1", to="beta/main:x")
        self.assertEqual(self.thread()[-1]["toPeer"], "beta")
        self.assertTrue(self.post(by="beta/main:x", to="test-node/main:eng-1", peer="beta")["ok"])
        self.assertEqual(self.thread()[-1]["peer"], "beta")

    def test_a_peer_cannot_speak_as_someone_else_or_come_from_the_browser(self):
        self.post(by="test-node/main:eng-1", to="beta/main:x")
        self.assertEqual(self.post(by="test-node/main:Boss", peer="beta")["error"], "not_on_card")
        self.assertEqual(self.post(ident="browser", by="beta/main:x", peer="beta")["error"], "not_on_card")

    def test_a_plain_comment_still_reaches_the_owner(self):
        self.post(by="test-node/main:eng-1")
        self.emit.assert_called_once()

    def test_a_plugins_ask_shows_on_the_boss_card_where_the_agent_can_confirm_it(self):
        with self.srv.LOCK:
            b = self.srv.load_board()
            b["bossChat"] = "c1"
            self.srv.save_board(b)
        note = self.srv.Handler._on_boss_card(mock.Mock(), "connect Slack")
        self.assertIn("mp card c1", note)
        c = self.thread()[-1]
        self.assertEqual((c["by"], c["to"]), ("CEO", self.srv.BOSS_AGENT))
        self.assertIn("connect Slack", c["body"])


if __name__ == "__main__":
    unittest.main()
