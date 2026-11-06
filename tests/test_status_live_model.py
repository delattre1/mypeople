"""`mp status` flags a Claude agent by the model and effort its session last ran on, not its launch flags.

Mac 2's engineers launched on --model claude-opus-4-8 without --effort, then switched with /model and
/effort; status kept showing "!! runs claude-opus-4-8/?" because it read only the launch flags.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from sandbox import load as load_mp

AID = "test-node/main:eng-1"


def reply(model, effort, **extra):
    return dict({"type": "assistant", "message": {"model": model}, "effort": effort}, **extra)


class StatusLiveModelTests(unittest.TestCase):
    def status(self, transcript):
        """The !! part of AID's status line, for a session whose transcript holds these entries."""
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            mp.save_roster({AID: {"backend": "claude", "model": "claude-opus-4-8", "session_id": "old"}})
            # The hook keeps the live session id here; a /clear moves it past the roster's.
            os.makedirs(os.path.join(mp.STATUS_DIR, "mc-main"))
            with open(os.path.join(mp.STATUS_DIR, "mc-main", "eng-1.json"), "w") as f:
                json.dump({"session_id": "live"}, f)
            if transcript is not None:
                os.makedirs(os.path.join(td, ".claude", "projects", "-work"))
                with open(os.path.join(td, ".claude", "projects", "-work", "live.jsonl"), "w") as f:
                    f.write("".join(json.dumps(e) + "\n" for e in transcript) + '{"cut')
            answers = {mp.QUEUE_URL + "/agents": [{"agent_id": AID, "state": "alive"}]}
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"HOME": td}), \
                    mock.patch.object(mp.C, "http_json", lambda m, url, *a, **k: (200, answers.get(url, []))), \
                    mock.patch.object(mp.C, "live_drift", return_value=[]), contextlib.redirect_stdout(out):
                mp.do_status([])
            line = next(l for l in out.getvalue().splitlines() if l.startswith(AID))
            return line.partition("!!")[2].strip()

    def test_a_session_switched_with_model_and_effort_is_not_flagged(self):
        self.assertEqual("", self.status([reply("claude-opus-4-8", None), reply("claude-opus-5-5", "xhigh")]))

    def test_it_reads_the_last_reply_of_the_main_session_only(self):
        self.assertEqual("runs claude-sonnet-5-5/high, not claude-opus-5-5/xhigh", self.status([
            reply("claude-sonnet-5-5", "high"),
            reply("claude-haiku-5-5", "medium", isSidechain=True),
            reply("<synthetic>", None),
            {"type": "user", "message": {"content": "hi"}}]))

    def test_without_a_reply_it_falls_back_to_the_launch_flags(self):
        self.assertEqual("runs claude-opus-4-8/?, not claude-opus-5-5/xhigh", self.status(None))


if __name__ == "__main__":
    unittest.main()
