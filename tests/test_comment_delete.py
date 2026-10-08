"""Deleting a comment: one id, and only its author or the Boss may do it."""
import tempfile
import unittest

from test_board_store import load_module


class CommentDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.srv = load_module(self.tmp.name, "todo-server.py", "ts_cdel_%d" % id(self))
        self.board = {"tasks": {"c1": {"id": "c1", "comments": [
            {"id": "a1", "by": "test-node/main:eng-7", "body": "mine"},
            {"id": "b2", "by": "CEO", "body": "his"}]}}}

    def ids(self):
        return [c["id"] for c in self.board["tasks"]["c1"]["comments"]]

    def test_a_stranger_cannot_delete_someone_elses_comment(self):
        err, c = self.srv.delete_comment(self.board, "c1", "b2", "test-node/main:eng-9")
        self.assertEqual("not_author", err)
        self.assertIsNone(c)
        self.assertEqual(["a1", "b2"], self.ids(), "a refused delete removes nothing")

    def test_an_anonymous_actor_is_refused(self):
        self.assertEqual("not_author", self.srv.delete_comment(self.board, "c1", "a1", "")[0])
        self.assertEqual(["a1", "b2"], self.ids())

    def test_the_author_deletes_exactly_their_one_comment(self):
        err, c = self.srv.delete_comment(self.board, "c1", "a1", "test-node/main:eng-7")
        self.assertIsNone(err)
        self.assertEqual("mine", c["body"])
        self.assertEqual(["b2"], self.ids())

    def test_the_boss_may_delete_any_comment(self):
        self.assertIsNone(self.srv.delete_comment(self.board, "c1", "b2", self.srv.BOSS_AGENT)[0])
        self.assertEqual(["a1"], self.ids())

    def test_unknown_ids_are_reported_not_guessed(self):
        self.assertEqual("no_task", self.srv.delete_comment(self.board, "zz", "a1", "CEO")[0])
        self.assertEqual("no_comment", self.srv.delete_comment(self.board, "c1", "zz", "CEO")[0])


if __name__ == "__main__":
    unittest.main()
