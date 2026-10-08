"""The Instagram comments plugin: only signed pushes count, each comment is delivered once, our
own comments never, and a comment survives its agent not being up yet.

Instagram, docker and the clock are stubbed, so nothing leaves the box.
"""
import hashlib
import hmac
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "mypeople" / "runtime" / "plugins" / "instagram-comments" / "instagram-comments.py"
ENV = {"INSTAGRAM_APP_SECRET": "appsecret", "INSTAGRAM_VERIFY_TOKEN": "vt", "INSTAGRAM_USER_ID": "1784",
       "HOST_ID": "node", "MYPEOPLE_CONFIG_PATH": "/nonexistent"}


def load(state_dir):
    with mock.patch.dict(os.environ, dict(ENV, INSTAGRAM_COMMENTS_STATE_DIR=state_dir)):
        loader = importlib.machinery.SourceFileLoader("ig_%d" % id(state_dir), str(PLUGIN))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod


def push(*comments):
    return {"object": "instagram", "entry": [{"id": "1784", "changes": [
        {"field": "comments", "value": {"id": cid, "text": text, "from": {"id": uid, "username": user},
                                        "media": {"id": "m1"}}} for cid, text, uid, user in comments]}]}


class InstagramCommentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        p = mock.patch.dict(os.environ, ENV)
        p.start()
        self.addCleanup(p.stop)

    def test_only_meta_signed_bodies_pass(self):
        body = b'{"object":"instagram"}'
        good = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
        self.assertTrue(self.m.signature_ok(body, good))
        self.assertFalse(self.m.signature_ok(body + b" ", good))
        self.assertFalse(self.m.signature_ok(body, None))

    def test_own_comments_skipped_and_a_retried_push_spools_once(self):
        payload = push(("c1", "how  do I\njoin?", "999", "fan"), ("c2", "thanks!", "1784", "danedelattre"))
        self.assertEqual(["c1"], [c["id"] for c in self.m.spool_add(self.m.comments_in(payload, "1784"))])
        self.assertEqual([], self.m.spool_add(self.m.comments_in(payload, "1784")))
        c = self.m.spool_pending()[0]
        self.assertEqual("[IG REPLY] comment_id=c1 user=fan permalink=https://ig/p/1: how do I join?",
                         self.m.format_comment(c, "https://ig/p/1"))

    def test_unreachable_fleet_keeps_the_comment_until_it_lands(self):
        self.m.spool_add(self.m.comments_in(push(("c1", "hi", "999", "fan")), "1784"))
        calls = []

        def once(timeout=None):   # stop the loop after one pass
            raise StopIteration

        with mock.patch.object(self.m, "permalink", return_value=""), \
             mock.patch.object(self.m.WAKE, "wait", side_effect=once):
            with mock.patch.object(self.m, "deliver", side_effect=OSError("asleep")):
                with self.assertRaises(StopIteration):
                    self.m.forward_loop()
            self.assertEqual(["c1"], [c["id"] for c in self.m.spool_pending()])
            with mock.patch.object(self.m, "deliver", side_effect=lambda t: calls.append(t) or True):
                with self.assertRaises(StopIteration):
                    self.m.forward_loop()
        self.assertEqual([], self.m.spool_pending())
        self.assertEqual(["[IG REPLY] comment_id=c1 user=fan permalink=media:m1: hi"], calls)

    def test_a_cold_agent_gets_nothing_typed_into_it(self):
        # The session is still starting: the comment must stay spooled, not vanish into a pane
        # that is not taking input yet.
        with mock.patch.object(self.m, "ensure_agent", return_value=False), \
             mock.patch.object(self.m, "docker") as docker:
            self.assertFalse(self.m.deliver("[IG REPLY] comment_id=c1 user=fan permalink=x: hi"))
        docker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
