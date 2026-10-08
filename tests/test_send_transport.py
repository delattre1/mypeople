"""mp send delivers a message as ONE atomic paste (card 5676f76673).

Delivery used to be `tmux send-keys -l`, which failed three ways: no bracketed-paste framing
(so a composer read every newline as Enter and submitted one message as N fragments -- the
agent saw only the tail), the message rode in as a command argument (tmux refuses past ~17KB
with "command too long", dropping it whole), and the result was never checked (both failures
returned True and printed "sent").

These tests drive real tmux panes. The receiver enables bracketed-paste mode and logs raw
bytes, which is the only way to see the framing the composer actually gets.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"

RECEIVER = r'''
import os, sys, termios, tty
out = open(sys.argv[1], "wb", buffering=0)
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
sys.stdout.write("\x1b[?2004h"); sys.stdout.flush()
try:
    while True:
        b = os.read(fd, 4096)
        if not b or b"\x04" in b:
            out.write(b.replace(b"\x04", b"")); break
        out.write(b)
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
'''

PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"


def have_tmux():
    return subprocess.run(["which", "tmux"], capture_output=True).returncode == 0


@unittest.skipUnless(have_tmux(), "tmux not available")
class SendTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(BIN))
        import mpcommon
        cls.C = mpcommon

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(BIN))

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.recv = os.path.join(self._td.name, "raw.bin")
        self.script = os.path.join(self._td.name, "receiver.py")
        Path(self.script).write_text(RECEIVER, encoding="utf-8")
        self.sess = "mptest-%d" % os.getpid()
        self._kill()
        self.addCleanup(self._kill)
        subprocess.run(["tmux", "new-session", "-d", "-s", self.sess, "-x", "200", "-y", "50",
                        "%s %s %s" % (sys.executable, self.script, self.recv)], check=True)
        time.sleep(0.8)
        self.target = subprocess.run(
            ["tmux", "list-panes", "-t", self.sess, "-F",
             "#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True).stdout.split()[0]

    def _kill(self):
        subprocess.run(["tmux", "kill-session", "-t", self.sess], capture_output=True)

    def deliver(self, message):
        ok = self.C.tmux_send_message(self.target, message)
        time.sleep(1.5)
        subprocess.run(["tmux", "send-keys", "-t", self.target, "C-d"], capture_output=True)
        time.sleep(0.5)
        raw = Path(self.recv).read_bytes() if os.path.exists(self.recv) else b""
        return ok, raw

    def test_a_multi_paragraph_message_arrives_as_one_bracketed_paste(self):
        msg = "Para one.\n\nPara two.\n\nPara three, point me at the repo."
        ok, raw = self.deliver(msg)
        self.assertTrue(ok)
        # One paste, not N keystroke-submits: the framing is what tells the composer so.
        self.assertEqual(raw.count(PASTE_START), 1)
        self.assertEqual(raw.count(PASTE_END), 1)
        pasted = raw.split(PASTE_START)[1].split(PASTE_END)[0]
        # Inside the paste a newline is data (CR), never a submit.
        self.assertEqual(pasted.replace(b"\r", b"\n").decode(), msg)

    def test_a_multi_paragraph_message_causes_exactly_one_submit(self):
        """The heart of the bug: 4 newlines used to arrive bare, as 4 Enters, so one message
        was submitted as several and the agent kept only the tail. Everything outside the
        paste markers is a keystroke the composer acts on -- there must be exactly one, the
        submit we send on purpose."""
        ok, raw = self.deliver("A\n\nB\n\nC")
        self.assertTrue(ok)
        outside = raw.split(PASTE_START)[0] + raw.split(PASTE_END)[-1]
        self.assertEqual(outside.count(b"\r") + outside.count(b"\n"), 1)

    def test_a_message_past_the_send_keys_argument_limit_still_arrives(self):
        # 18000 chars was dropped whole by send-keys -l ("command too long"); 16000 survived.
        msg = "X" * 40000
        ok, raw = self.deliver(msg)
        self.assertTrue(ok)
        pasted = raw.split(PASTE_START)[1].split(PASTE_END)[0]
        self.assertEqual(len(pasted), len(msg))

    def test_delivery_to_a_missing_pane_reports_failure(self):
        # has-session only checked the session name, so a wrong window returned True.
        self.assertFalse(self.C.tmux_send_message("%s:nosuchwindow" % self.sess, "hello"))

    def test_an_empty_message_is_refused(self):
        self.assertFalse(self.C.tmux_send_message(self.target, "   "))


if __name__ == "__main__":
    unittest.main()
