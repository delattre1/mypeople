"""A revive pins the doctrine an agent died with -- except on a node that updates itself in place.

The cloud image swaps releases on the same disk (cloud/update.py). If the new release bumped the
Boss's role, the pinned revive refused ("locked role drift") and the Boss never came back. There,
run.sh sets MP_REVIVE_FOLLOWS_REGISTRY=1 and the same session resumes under the installed role.
"""
import os
import tempfile
import unittest
from unittest import mock

from test_backends import load_mp

BOSS = "test-node/main:Boss"


class ReviveFollowsRegistryTests(unittest.TestCase):
    def revive(self, *flags, **env):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            roster = {BOSS: {"retired": True, "retire_reason": "boss-window-missing",
                             "session_id": "sid-1", "backend": "claude", "is_master": True, "model": "claude-opus-4-8",
                             "role": "boss", "role_ref": "boss@0.0.1", "role_digest": "old"}}
            with mock.patch.dict(os.environ, env), \
                 mock.patch.object(mp, "load_roster", return_value=roster), \
                 mock.patch.object(mp, "session_files", return_value=["/t/sid-1.jsonl"]), \
                 mock.patch.object(mp, "do_spawn", return_value=0) as spawn:
                self.assertEqual(0, mp.do_revive([BOSS, *flags]))
            return spawn.call_args

    def revive_kwargs(self, **env):
        return self.revive(**env).kwargs

    def test_pinned_by_default(self):
        kw = self.revive_kwargs(MP_REVIVE_FOLLOWS_REGISTRY="")
        self.assertEqual(("sid-1", "boss@0.0.1", "old"),
                         (kw["resume_session"], kw["locked_role_ref"], kw["expected_role_digest"]))

    def test_follows_the_installed_role_on_a_self_updating_node(self):
        kw = self.revive_kwargs(MP_REVIVE_FOLLOWS_REGISTRY="1")
        self.assertEqual("sid-1", kw["resume_session"], "the same session must come back")
        self.assertIsNone(kw.get("locked_role_ref"))
        self.assertIsNone(kw.get("expected_role_digest"))

    def test_model_flag_brings_the_same_session_back_on_another_model(self):
        # A home Boss changes an agent's model when its person asks (card 9ded1d6ebf): kill, then this.
        call = self.revive("--model", "claude-opus-5-5")
        args = call.args[0]
        self.assertEqual("claude-opus-5-5", args[args.index("--model") + 1])
        self.assertNotIn("claude-opus-4-8", args)
        self.assertEqual("sid-1", call.kwargs["resume_session"])

    def test_without_the_flag_the_recorded_model_stays(self):
        args = self.revive().args[0]
        self.assertEqual("claude-opus-4-8", args[args.index("--model") + 1])


if __name__ == "__main__":
    unittest.main()
