"""mp send refuses to paste into a Claude session that is not at its input box.

A session still starting can drop a pasted message without trace while the send reports
"sent" (carried for months as a memory warning). And a dialog has no input box either:
on 2026-10-03 a Boss `/exit` stopped at "You have 4 unsent feedback drafts: Enter to
review & send", where the Enter after any paste would have sent them. These tests hold
the readiness rule to real screens, and the send path to it, with tmux faked.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"
RULE = "─" * 60
FOOT = "  ⏵⏵ bypass permissions on (shift+tab to cycle)"
READY = "● done.\n✻ Worked for 2s\n%s\n❯ \n%s\n%s\n" % (RULE, RULE, FOOT)
MID_TURN = "✻ Thinking… (3s · esc to interrupt)\n%s\n❯ \n%s\n%s\n" % (RULE, RULE, FOOT)
SUGGESTION = "%s\n❯ \x1b[2mwhat's\x1b[0m \x1b[2mthe\x1b[0m \x1b[2mstatus\x1b[0m\n%s\n" % (RULE, RULE)
STARTUP = " ▐▛███▜▌   Claude Code v2.1.286\n▝▜█████▛▘  Opus 5.5\n"
EXIT_DIALOG = ("%s Goal discussion ─\nYou have 4 unsent feedback drafts\n"
               "Enter to review & send · Esc to discard and exit\n" % RULE)
MCP_PANEL = "%s\n  Manage MCP servers\n    Local MCPs (/x)\n  ❯ ✘ domo\n    ✔ plow  15 tools\n" % RULE
MENU = "Do you want to proceed?\n❯ 1. Yes\n  2. No\n"
# eng-955 on 2026-10-03, 8.6h on an unanswered question; and its last option under the separator.
QUESTION = ("%s\n\u2502 Where should the PR go?\n\u276f 1. New repo (Recommended)\n  2. Unarchive\n%s\n"
            "  5. Chat about this\nEnter to select \u00b7 Tab/Arrow keys to navigate \u00b7 Esc to cancel\n" % (RULE, RULE))
QUESTION_LAST = "%s\n  1. New repo\n%s\n\u276f 5. Chat about this\nEnter to select \u00b7 Esc to cancel\n" % (RULE, RULE)


def load_common(install):
    cfg = Path(install) / "queue.env"
    cfg.write_text('export INSTALL_DIR="%s"\nexport HOST_ID="node"\n' % install)
    with mock.patch.dict(os.environ, {"MYPEOPLE_CONFIG_PATH": str(cfg), "INSTALL_DIR": install,
                                      "HOST_ID": "node"}):
        sys.modules.pop("mpcommon", None)
        sys.path.insert(0, str(BIN))
        try:
            loader = importlib.machinery.SourceFileLoader("mpcommon_ready_%d" % id(install), str(BIN / "mpcommon.py"))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            return mod
        finally:
            sys.path.remove(str(BIN))


class FakeTmux:
    def __init__(self, screen, pane=True):
        self.screen, self.pane, self.calls = screen, pane, []

    def __call__(self, argv, **kw):
        self.calls.append(argv[1])
        rc = 0 if (self.pane or argv[1] != "list-panes") else 1
        out = self.screen if argv[1] == "capture-pane" else ""
        return mock.Mock(returncode=rc, stdout=out if kw.get("text") else out.encode(), stderr="")


class ComposerReadyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.C = load_common(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_the_input_box_is_ready(self):
        ready = self.C.claude_composer_ready
        for name, screen in [("idle", READY), ("mid-turn", MID_TURN), ("dimmed suggestion", SUGGESTION)]:
            self.assertTrue(ready(screen), name)
        for name, screen in [("starting", STARTUP), ("exit dialog", EXIT_DIALOG),
                             ("/mcp panel", MCP_PANEL), ("menu", MENU), ("question", QUESTION),
                             ("question, last option", QUESTION_LAST), ("blank", "")]:
            self.assertFalse(ready(screen), name)

    def agent(self, backend):
        d = Path(self.tmp.name) / "status" / "mc-main"
        d.mkdir(parents=True, exist_ok=True)
        (d / "eng-1.json").write_text(json.dumps({"backend": backend, "status": "starting"}))
        return "mc-main:eng-1"

    def send(self, target, screen, pane=True):
        fake = FakeTmux(screen, pane)
        with mock.patch.object(self.C.subprocess, "run", side_effect=fake), \
             mock.patch.object(self.C.time, "sleep"):
            return self.C.tmux_send_message(target, "hello"), fake.calls

    def test_a_claude_session_not_at_its_box_is_refused_and_nothing_is_pasted(self):
        target = self.agent("claude")
        for screen in (STARTUP, EXIT_DIALOG):
            ok, calls = self.send(target, screen)
            self.assertFalse(ok)
            self.assertNotIn("paste-buffer", calls)
            self.assertNotIn("send-keys", calls)

    def test_a_ready_claude_session_gets_the_message(self):
        ok, calls = self.send(self.agent("claude"), MID_TURN)
        self.assertTrue(ok)
        self.assertIn("paste-buffer", calls)
        self.assertEqual(calls[-1], "send-keys")

    def test_codex_and_non_agent_panes_are_unchanged(self):
        self.assertTrue(self.send(self.agent("codex"), STARTUP)[0])
        self.assertTrue(self.send("mptest-1:0.0", STARTUP)[0])

    def test_send_when_ready_says_why(self):
        target = self.agent("claude")
        with mock.patch.object(self.C.time, "sleep"):
            with mock.patch.object(self.C.subprocess, "run", side_effect=FakeTmux(STARTUP)):
                self.assertEqual(self.C.send_when_ready(target, "hi", 0), "not_ready")
            with mock.patch.object(self.C.subprocess, "run", side_effect=FakeTmux(READY, pane=False)):
                self.assertEqual(self.C.send_when_ready(target, "hi", 0), "no_pane")
            with mock.patch.object(self.C.subprocess, "run", side_effect=FakeTmux(READY)):
                self.assertEqual(self.C.send_when_ready(target, "hi", 0), "sent")


class MpSendExitCodeTest(unittest.TestCase):
    def test_mp_send_exits_3_on_not_ready_so_a_retrying_sender_retries(self):
        with tempfile.TemporaryDirectory() as td:
            load_common(td)  # leaves mpcommon configured for this throwaway install
            cfg = Path(td) / "queue.env"
            with mock.patch.dict(os.environ, {"MYPEOPLE_CONFIG_PATH": str(cfg), "INSTALL_DIR": td,
                                              "HOST_ID": "node", "QUEUE_URL": "http://127.0.0.1:1"}):
                sys.modules.pop("mpcommon", None)
                sys.path.insert(0, str(BIN))
                try:
                    loader = importlib.machinery.SourceFileLoader("mp_ready", str(BIN / "mp"))
                    spec = importlib.util.spec_from_loader("mp_ready", loader)
                    mp = importlib.util.module_from_spec(spec)
                    loader.exec_module(mp)
                finally:
                    sys.path.remove(str(BIN))
                with mock.patch.object(mp.C, "send_when_ready", return_value="not_ready"), \
                     mock.patch.object(mp.C, "enqueue_notification_route", return_value=None):
                    self.assertEqual(mp.do_send(["node/main:eng-1", "hi"]), 3)
                with mock.patch.object(mp.C, "send_when_ready", return_value="sent"), \
                     mock.patch.object(mp.C, "enqueue_notification_route", return_value=None):
                    self.assertEqual(mp.do_send(["node/main:eng-1", "hi"]), 0)


if __name__ == "__main__":
    unittest.main()
