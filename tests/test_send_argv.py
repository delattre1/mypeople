"""`mp send` fails loudly instead of delivering junk (card 5676f76673).

The message is ARGV. Getting that wrong used to be silent: `mp send X --by Y <<EOF` sent the
literal string "--by Y" and discarded the real body on stdin nothing read, while
`mp send X "body" --by Y` delivered the body with " --by Y" glued on. Both printed "sent".
Several agents lost most of a day to it, and the tell was that the recipient got the SAME
string every time -- real truncation varies with the text, operator error repeats verbatim.
"""
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"
AGENT = "test-node/main:eng-1"


def load_mp():
    sys.path.insert(0, str(BIN))
    try:
        loader = importlib.machinery.SourceFileLoader("mp_cli_argv", str(BIN / "mp"))
        spec = importlib.util.spec_from_loader("mp_cli_argv", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(BIN))


class SendArgvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mp = load_mp()

    def send(self, argv, stdin_text=None, tty=True):
        """Run do_send with the transport stubbed. Returns (rc, delivered_message, stderr)."""
        got = {}
        err = io.StringIO()
        stdin = mock.Mock()
        stdin.isatty.return_value = tty
        stdin.read.return_value = stdin_text or ""
        with mock.patch.object(self.mp.C, "tmux_send_message",
                               lambda t, m: got.__setitem__("msg", m) or True), \
             mock.patch.object(self.mp.C, "enqueue_notification_route", lambda *a, **k: ""), \
             mock.patch.object(self.mp.C, "tmux_target", lambda aid: "mc-main:eng-1"), \
             mock.patch.object(self.mp, "is_local", lambda aid: True), \
             mock.patch.object(self.mp.sys, "stdin", stdin), \
             mock.patch.object(self.mp.sys, "stderr", err), \
             mock.patch.object(self.mp.sys, "stdout", io.StringIO()):
            rc = self.mp.do_send(argv)
        return rc, got.get("msg"), err.getvalue()

    def test_a_normal_message_is_delivered_whole(self):
        rc, msg, _ = self.send([AGENT, "para one\n\npara two"])
        self.assertEqual(rc, 0)
        self.assertEqual(msg, "para one\n\npara two")

    def test_a_leading_flag_is_refused_instead_of_sent_as_the_message(self):
        """`mp send X --by Y <<EOF` used to deliver the literal string "--by Y"."""
        rc, msg, err = self.send([AGENT, "--by", AGENT])
        self.assertEqual(rc, 2)
        self.assertIsNone(msg)
        self.assertIn("no --by flag", err)

    def test_a_trailing_flag_is_refused_too(self):
        """The commoner habit: `mp send X "body" --by Y` silently appended " --by Y".
        A first-token-only check would have missed this one entirely."""
        rc, msg, err = self.send([AGENT, "the body", "--by", AGENT])
        self.assertEqual(rc, 2)
        self.assertIsNone(msg)
        self.assertIn("no --by flag", err)

    def test_the_error_names_the_fix(self):
        """"invalid arguments" would have saved nobody. It must say: argument not stdin,
        no --by flag, reply_to comes from AGENT_ID."""
        _, _, err = self.send([AGENT, "--by", AGENT])
        self.assertIn("ARGUMENTS", err)
        self.assertIn("--by", err)
        self.assertIn("AGENT_ID", err)
        self.assertIn("correct: mp send", err)

    def test_a_message_merely_mentioning_a_flag_still_sends(self):
        """A real message is ONE quoted argument, so --by inside it is not a standalone
        token. Agents discuss this very flag; the guard must not eat those messages."""
        body = "you passed --by test-node/main:eng-2, drop it"
        rc, msg, _ = self.send([AGENT, body])
        self.assertEqual(rc, 0)
        self.assertEqual(msg, body)

    def test_a_piped_body_is_read_when_stdin_is_not_a_tty(self):
        rc, msg, _ = self.send([AGENT], stdin_text="body from a heredoc", tty=False)
        self.assertEqual(rc, 0)
        self.assertEqual(msg, "body from a heredoc")

    def test_an_interactive_call_with_no_message_prints_usage_and_does_not_hang(self):
        """The hazard in adding a stdin read: without the isatty gate a human typing
        `mp send <agent>` blocks forever on an empty terminal instead of getting usage."""
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        stdin.read.side_effect = AssertionError("read stdin on a tty -- would hang")
        err = io.StringIO()
        with mock.patch.object(self.mp.sys, "stdin", stdin), \
             mock.patch.object(self.mp.sys, "stderr", err):
            rc = self.mp.do_send([AGENT])
        self.assertEqual(rc, 2)
        self.assertIn("usage: mp send", err.getvalue())


if __name__ == "__main__":
    unittest.main()
