"""`mp comment` refuses a flag-shaped body instead of posting it (card e949527956).

Two hits in one day: a bare "-" went up as a one-character comment, and `mp comment <card>
--stdin <<EOF` posted the literal string "--stdin" while the real deliverable stayed on stdin
that nothing read. Both printed the success line. This is the mirror of the mp send trap fixed
in 5.7.8 -- and the commands are opposites, which is why it keeps catching people: mp comment
reads stdin and DOES parse --by; mp send does neither.

--by is the only flag mp comment has, and it must keep working.
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
CARD = "abc1234567"
AGENT = "test-node/main:eng-1"


def load_mp():
    sys.path.insert(0, str(BIN))
    try:
        loader = importlib.machinery.SourceFileLoader("mp_cli_comment", str(BIN / "mp"))
        spec = importlib.util.spec_from_loader("mp_cli_comment", loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        return m
    finally:
        sys.path.remove(str(BIN))


class CommentArgvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mp = load_mp()

    def comment(self, argv, stdin_text=None, tty=True):
        """Run do_comment with the HTTP post stubbed. Returns (rc, posted_payload, stderr)."""
        sent = {}
        err = io.StringIO()
        stdin = mock.Mock()
        stdin.isatty.return_value = tty
        stdin.read.return_value = stdin_text or ""

        def fake_post(method, url, body=None, headers=None, timeout=None):
            sent.update(body or {})
            return 200, {"ok": True, "id": "cid"}

        with mock.patch.object(self.mp.C, "http_json", fake_post), \
             mock.patch.object(self.mp.sys, "stdin", stdin), \
             mock.patch.object(self.mp.sys, "stderr", err), \
             mock.patch.object(self.mp.sys, "stdout", io.StringIO()), \
             mock.patch.dict(os.environ, {"AGENT_ID": AGENT}, clear=False):
            rc = self.mp.do_comment(argv)
        return rc, (sent.get("body") if sent else None), err.getvalue()

    # --- the two real incidents ---
    def test_a_bare_dash_is_refused_not_posted(self):
        rc, body, err = self.comment([CARD, "-"])
        self.assertEqual(rc, 2)
        self.assertIsNone(body)
        self.assertIn("ONE quoted argument", err)

    def test_a_stdin_flag_is_refused_and_the_heredoc_is_not_lost_silently(self):
        rc, body, err = self.comment([CARD, "--stdin"], stdin_text="the real deliverable", tty=False)
        self.assertEqual(rc, 2)
        self.assertIsNone(body)
        self.assertIn("no --stdin", err)

    def test_the_documented_file_flag_mistake_is_refused(self):
        """`mp comment <card> --file notes.md` used to post that literal string."""
        rc, body, err = self.comment([CARD, "--file", "notes.md"])
        self.assertEqual(rc, 2)
        self.assertIsNone(body)

    def test_the_error_names_both_correct_forms(self):
        _, _, err = self.comment([CARD, "--stdin"])
        self.assertIn('mp comment %s "your comment text"' % CARD, err)
        self.assertIn("< body.md", err)
        self.assertIn("--by", err)

    # --- everything that must keep working ---
    def test_a_normal_body_still_posts(self):
        rc, body, _ = self.comment([CARD, "a real comment"])
        self.assertEqual(rc, 0)
        self.assertEqual(body, "a real comment")

    def test_the_by_flag_still_works(self):
        rc, body, _ = self.comment([CARD, "text", "--by", "test-node/main:eng-9"])
        self.assertEqual(rc, 0)
        self.assertEqual(body, "text")

    def test_a_body_piped_on_stdin_still_works(self):
        rc, body, _ = self.comment([CARD], stdin_text="piped body", tty=False)
        self.assertEqual(rc, 0)
        self.assertEqual(body, "piped body")

    def test_stdin_with_the_by_flag_still_works(self):
        """The shape every agent uses to post a long report."""
        rc, body, _ = self.comment([CARD, "--by", AGENT], stdin_text="long report", tty=False)
        self.assertEqual(rc, 0)
        self.assertEqual(body, "long report")

    def test_a_bulleted_body_starting_with_a_dash_still_posts(self):
        """"- item one" is one quoted argument with whitespace, not a bare flag token."""
        rc, body, _ = self.comment([CARD, "- item one\n- item two"])
        self.assertEqual(rc, 0)
        self.assertEqual(body, "- item one\n- item two")

    def test_an_interactive_call_with_no_body_does_not_hang(self):
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        stdin.read.side_effect = AssertionError("read stdin on a tty -- would hang")
        err = io.StringIO()
        with mock.patch.object(self.mp.sys, "stdin", stdin), \
             mock.patch.object(self.mp.sys, "stderr", err):
            rc = self.mp.do_comment([CARD])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
