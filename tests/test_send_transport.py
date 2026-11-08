"""mp send delivers a message as ONE atomic paste (card 5676f76673).

Delivery used to be `tmux send-keys -l`, which failed three ways: no bracketed-paste framing
(so a composer read every newline as Enter and submitted one message as N fragments -- the
agent saw only the tail), the message rode in as a command argument (tmux refuses past ~17KB
with "command too long", dropping it whole), and the result was never checked (both failures
returned True and printed "sent").

These tests drive real tmux panes. The receiver enables bracketed-paste mode and logs raw
bytes, which is the only way to see the framing the composer actually gets.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


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


SLOW_CLAUDE = r'''
import base64, json, os, sys, time, tty
log, fd = open(sys.argv[1], "a"), sys.stdin.fileno()
tty.setraw(fd)
got = b""
tick = 0
def draw():   # Claude Code's input box: a rule, a redraw count, the end of what came in, a rule
    text = got.replace(b"\x1b[200~", b"").replace(b"\x1b[201~", b"")
    tail = text[-50:].decode("utf-8", "replace").replace("\r", " ").replace("\n", " ")
    os.write(1, ("\x1b[?2004h\x1b[2J\x1b[H" + "─" * 40 + "\r\n❯ [%d] " % tick + tail + "\r\n" + "─" * 40 + "\r\n").encode())
draw()
while b"\x04" not in got:
    for _ in range(3):   # a busy agent: nothing is read for 0.3 s, so the pty fills behind it, while
        tick += 1        # its box keeps redrawing (the Boss's re-wraps on every resize of its window)
        draw()
        time.sleep(0.1)
    b = os.read(fd, 65536)
    log.write(json.dumps(base64.b64encode(b).decode()) + "\n"); log.flush()
    got += b
    draw()
'''


@unittest.skipUnless(have_tmux(), "tmux not available")
class SlowClaudeTests(unittest.TestCase):
    """11-07: an agent busy enough to stall between two pty reads of one long paste got it cut in two
    (Claude Code ends a paste that has had no bytes for 2 s) and sometimes lost its head: 83 of the
    Boss's 271 long messages. So a long message reaches a Claude pane as pastes that each start and
    end inside one read, then one Enter. Real tmux, on a server of its own."""

    def setUp(self):
        import sandbox
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)
        env = mock.patch.dict(os.environ, {"TMUX_TMPDIR": str(self.dir)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TMUX", None)   # an agent's shell would aim every tmux call at the live server
        sock = self.dir / ("tmux-%d" % os.getuid()) / "default"
        self.addCleanup(subprocess.run, ["tmux", "-S", str(sock), "kill-server"], capture_output=True)
        self.C = sandbox.load(str(self.dir), "mpcommon.py", HOST_ID="node")
        status = Path(self.C.CFG["INSTALL_DIR"]) / "status" / "mc-slow"
        status.mkdir(parents=True)
        (status / "boss.json").write_text(json.dumps({"backend": "claude"}))
        (self.dir / "claude.py").write_text(SLOW_CLAUDE, encoding="utf-8")
        self.reads = self.dir / "reads.jsonl"
        subprocess.run(["tmux", "new-session", "-d", "-s", "mc-slow", "-n", "boss", "-x", "150", "-y", "40",
                        "%s %s %s" % (sys.executable, self.dir / "claude.py", self.reads)], check=True)
        for _ in range(50):
            if self.C.pane_input_state("mc-slow:boss") == "ready":
                break
            time.sleep(0.1)

    def test_a_long_message_reaches_a_slow_claude_one_whole_paste_per_read_then_one_enter(self):
        msg = "\n\n".join("Para %d, ação ⚉: %s." % (i, " ".join(["word"] * 200)) for i in range(5))
        self.assertTrue(self.C.tmux_send_message("mc-slow:boss", msg))
        time.sleep(1)
        subprocess.run(["tmux", "send-keys", "-t", "mc-slow:boss", "C-d"], check=True)
        for _ in range(50):
            reads = [base64.b64decode(json.loads(l)) for l in self.reads.read_text().splitlines()]
            if reads and b"\x04" in reads[-1]:
                break
            time.sleep(0.1)
        self.assertGreater(len(reads), 3)
        for r in reads:
            self.assertEqual(r.count(PASTE_START), r.count(PASTE_END), "a paste spanned two reads: %r" % r[:80])
            self.assertLessEqual(r.count(PASTE_START), 1, "pieces piled up in the pty: %r" % r[:80])
        raw = b"".join(reads).replace(b"\x04", b"")
        pastes = [p.split(PASTE_END)[0] for p in raw.split(PASTE_START)[1:]]
        self.assertGreater(len(pastes), 1)
        self.assertEqual(msg, b"".join(pastes).replace(b"\r", b"\n").decode("utf-8"))
        outside = raw.split(PASTE_START)[0] + b"".join(p.split(PASTE_END)[1] for p in raw.split(PASTE_START)[1:])
        self.assertEqual(b"\r", outside, "one Enter, after the last piece")

    def test_a_second_sender_waits_for_a_long_message_to_finish(self):
        """11-07: another sender's Enter between two pieces submitted a [swarm chief] envelope to the
        Boss as two messages. Deliveries to one pane take turns."""
        import threading
        long_msg, short = "Long, ação: %s." % " ".join(["word"] * 400), "a short one"
        first = threading.Thread(target=self.C.tmux_send_message, args=("mc-slow:boss", long_msg))
        first.start()
        time.sleep(0.2)   # the long one holds the pane and is still landing piece by piece
        self.assertTrue(self.C.tmux_send_message("mc-slow:boss", short))
        first.join(30)
        time.sleep(1)
        subprocess.run(["tmux", "send-keys", "-t", "mc-slow:boss", "C-d"], check=True)
        for _ in range(50):
            raw = b"".join(base64.b64decode(json.loads(l)) for l in self.reads.read_text().splitlines())
            if b"\x04" in raw:
                break
            time.sleep(0.1)
        # The stream as the agent gets it, with "|" for each Enter (neither message has a line break).
        stream = raw.replace(b"\x04", b"").replace(PASTE_START, b"").replace(PASTE_END, b"").replace(b"\r", b"|")
        self.assertEqual(long_msg + "|" + short + "|", stream.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
