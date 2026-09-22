"""A comment on a card goes straight to the card's owner.

It used to ping only the Boss, so every word the CEO said waited for a whole agent turn before it
reached the person doing the work: measured on his own card, a median of 18s and a worst case of
439s. The board knows the owner, so it delivers first and tells the Boss it already did.
"""
import os
import tempfile
import unittest
from unittest import mock

from test_board_store import load_module


class CommentDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.srv = load_module(self.tmp.name, "todo-server.py", "ts_deliver_%d" % id(self))
        self.sent = []

        def fake_send(agent, message):
            self.sent.append((agent, message))
            return 0 if agent in self.reachable else 1

        self.reachable = {"test-node/main:eng-7", self.srv.BOSS_AGENT}
        p = mock.patch.object(self.srv, "mp_send", fake_send)
        p.start()
        self.addCleanup(p.stop)
        # deliver_comment runs in a thread; run it inline so the test sees the result.
        p2 = mock.patch.object(self.srv.threading, "Thread",
                               lambda target, args=(), daemon=None: mock.Mock(
                                   start=lambda: target(*args)))
        p2.start()
        self.addCleanup(p2.stop)

    def task(self, **kw):
        t = {"id": "c1", "text": "Ship the thing", "assignee": "test-node/main:eng-7"}
        t.update(kw)
        return t

    def emit(self, task, by="CEO", body="do the thing"):
        self.srv.emit_comment_event({}, task, by, body)
        return dict(self.sent)

    def test_the_owner_gets_it_first_and_verbatim(self):
        self.emit(self.task())
        agent, message = self.sent[0]
        self.assertEqual("test-node/main:eng-7", agent, "the owner is told before the Boss")
        self.assertIn("do the thing", message)
        self.assertIn("card c1", message)
        self.assertIn("CEO", message)

    def test_the_boss_is_told_it_was_already_delivered(self):
        self.emit(self.task())
        boss_msg = dict(self.sent)[self.srv.BOSS_AGENT]
        self.assertIn("already delivered to test-node/main:eng-7", boss_msg)
        self.assertIn("do the thing", boss_msg, "the Boss still sees what was said")

    def test_an_unowned_card_still_reaches_the_boss(self):
        self.emit(self.task(assignee=""))
        self.assertEqual([self.srv.BOSS_AGENT], [a for a, _ in self.sent])
        self.assertNotIn("already delivered", self.sent[0][1])

    def test_an_unreachable_owner_leaves_the_boss_to_relay(self):
        self.reachable = {self.srv.BOSS_AGENT}          # owner's window is gone
        self.emit(self.task())
        boss_msg = dict(self.sent)[self.srv.BOSS_AGENT]
        self.assertNotIn("already delivered", boss_msg,
                         "if delivery failed the Boss must still route it by hand")

    def test_an_agent_never_gets_its_own_comment_back(self):
        self.emit(self.task(), by="test-node/main:eng-7")
        self.assertEqual([self.srv.BOSS_AGENT], [a for a, _ in self.sent])

    def test_the_boss_own_comment_pings_nobody(self):
        self.emit(self.task(), by=self.srv.BOSS_AGENT)
        self.assertEqual([], self.sent)

    def test_a_test_card_is_still_silent(self):
        self.emit(self.task(test=True))
        self.assertEqual([], self.sent)

    def test_the_ping_counter_still_moves(self):
        t = self.task()
        self.srv.emit_comment_event({}, t, "CEO", "hi")
        self.assertEqual(1, t["pingsToBoss"])


if __name__ == "__main__":
    unittest.main()
