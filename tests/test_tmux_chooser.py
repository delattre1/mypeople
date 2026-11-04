"""`prefix w` shows the fleet from a real attached client, and retiring an agent leaves nothing behind.

Daniel, card a4607576c1: "I cannot run cmd + b + w there because it bugs? there is too much stuff?"
It was not the amount. tmux 3.7b/c test the NEWEST session for every session they squash, so while a
web viewer was the newest session the chooser drew a blank screen on any other session: 0 rows with 3
windows or 38. The client here is real: it runs in a pane of a second private tmux server, whose
capture-pane is the terminal, and the keys travel its pty like a keyboard's.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import sandbox

ROOT = Path(__file__).resolve().parents[1]
CONF = ROOT / "mypeople" / "runtime" / "config" / "tmux.conf"
BIN = ROOT / "mypeople" / "runtime" / "bin"
SESSION_ROW = re.compile(r"^\(\w[-\w]*\)\s+[-+] (\S+): \d+ windows", re.M)
RAW_LOG = "import os,sys,tty; tty.setraw(0); f=open(sys.argv[1],'wb',0)\nwhile True: f.write(os.read(0,4096))"


class Mux:
    """A private tmux server loading MyPlow's real conf, minus the plugin manager."""

    def __init__(self, test):
        self.dir = tempfile.mkdtemp(dir="/tmp")  # tmux sockets need a short path
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.conf = os.path.join(self.dir, "tmux.conf")
        Path(self.conf).write_text("".join(l for l in CONF.read_text().splitlines(True) if not l.startswith("run ")))
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMUX_TMPDIR": self.dir, "HOME": self.dir,
                    "TERM": "xterm-256color", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
        test.addCleanup(self.stop)

    def t(self, *args, sock="fleet"):
        return subprocess.run(["tmux", "-L", sock, "-f", self.conf if sock == "fleet" else "/dev/null"] + list(args),
                              env=self.env, capture_output=True, text=True)

    def stop(self):
        self.t("kill-server", sock="term")
        self.t("kill-server")

    def attach(self, session):
        self.t("kill-server", sock="term")
        client = "tmux -L fleet attach -t %s" % session
        self.t("new-session", "-d", "-s", "o", "-x", "120", "-y", "40", client, sock="term")
        self.t("set", "-g", "status", "off", sock="term")
        self.wait(lambda: session in self.t("list-clients", "-F", "#{client_session}").stdout)

    def keys(self, *keys):
        self.t("send-keys", "-t", "o", *keys, sock="term")

    def screen(self):
        return self.t("capture-pane", "-p", "-t", "o", sock="term").stdout

    @staticmethod
    def wait(cond, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class ChooserTests(unittest.TestCase):
    def fleet(self):
        m = Mux(self)
        m.t("new-session", "-d", "-s", "mc-main", "-n", "Boss", "-x", "120", "-y", "39", "sleep 300")
        for name in ("eng-2", "eng-3"):
            m.t("new-window", "-d", "-t", "mc-main", "-n", name, "sleep 300")
        m.t("new-session", "-d", "-t", "mc-main", "-s", "_v_old")
        m.t("new-session", "-d", "-s", "mc-rt", "-n", "site", "sleep 300")
        m.t("new-session", "-d", "-t", "mc-main", "-s", "_v_newest")  # what makes stock tmux draw nothing
        return m

    def test_prefix_w_lists_the_windows_from_every_session(self):
        m = self.fleet()
        for session, expected in (("mc-main", ["mc-main", "mc-rt"]), ("mc-rt", ["mc-main", "mc-rt"]),
                                  ("_v_old", ["_v_old", "mc-rt"]), ("_v_newest", ["_v_newest", "mc-rt"])):
            with self.subTest(session=session):
                m.attach(session)
                m.keys("C-b", "w")
                self.addCleanup(m.keys, "q")
                self.assertTrue(m.wait(lambda: "eng-3" in m.screen()), "blank chooser:\n" + m.screen())
                screen = m.screen()
                self.assertEqual(expected, sorted(SESSION_ROW.findall(screen)), screen)
                m.keys("q")
                self.assertTrue(m.wait(lambda: m.t("display-message", "-p", "-t", session, "#{pane_in_mode}").stdout.strip() == "0"))

    def test_prefix_s_lists_the_sessions(self):
        m = self.fleet()
        m.attach("mc-main")
        m.keys("C-b", "s")
        self.assertTrue(m.wait(lambda: "mc-rt" in m.screen()), "blank chooser:\n" + m.screen())
        self.assertEqual(["mc-main", "mc-rt"], sorted(SESSION_ROW.findall(m.screen())), m.screen())

    def test_picking_a_row_keeps_a_viewer_on_its_own_session(self):
        m = self.fleet()
        m.attach("_v_old")
        m.keys("C-b", "w")
        self.assertTrue(m.wait(lambda: "eng-3" in m.screen()), m.screen())
        m.keys("Down")
        time.sleep(0.2)
        m.keys("Enter")
        moved = lambda: m.t("list-clients", "-F", "#{client_session}:#{window_name}:#{pane_in_mode}").stdout.strip()
        self.assertTrue(m.wait(lambda: moved() == "_v_old:eng-2:0"), moved())


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class PaneInModeTests(unittest.TestCase):
    def setUp(self):
        self.m = Mux(self)
        self.log = os.path.join(self.m.dir, "raw.bin")
        self.script = os.path.join(self.m.dir, "r.py")
        Path(self.script).write_text(RAW_LOG)
        with mock.patch.dict(os.environ, {"TMUX_TMPDIR": self.m.dir}):
            os.environ.pop("TMUX", None)
            subprocess.run(["tmux", "-f", self.m.conf, "new-session", "-d", "-s", "mc-main", "-n", "eng-1",
                            "-x", "120", "-y", "39", "%s %s %s" % (sys.executable, self.script, self.log)],
                           check=True, env=dict(self.m.env))
            subprocess.run(["tmux", "new-window", "-d", "-t", "mc-main", "-n", "eng-2", "sleep 300"], env=dict(self.m.env))
        self.addCleanup(subprocess.run, ["tmux", "kill-server"], env=dict(self.m.env), capture_output=True)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.C = sandbox.load(tmp.name, "mpcommon.py")

    def mode(self):
        return subprocess.run(["tmux", "display-message", "-p", "-t", "mc-main:eng-1", "#{pane_mode}"],
                              env=dict(self.m.env), capture_output=True, text=True).stdout.strip()

    def test_a_delivery_reaches_the_program_and_leaves_the_chooser_open(self):
        subprocess.run(["tmux", "choose-tree", "-Zw", "-t", "mc-main:eng-1"], env=dict(self.m.env), check=True)
        self.assertEqual("tree-mode", self.mode())
        with mock.patch.dict(os.environ, {"TMUX_TMPDIR": self.m.dir}):
            self.assertTrue(self.C.tmux_send_message("mc-main:eng-1", "hello"))
        self.assertEqual("tree-mode", self.mode(), "the Enter landed in the chooser")
        self.assertTrue(Mux.wait(lambda: Path(self.log).read_bytes().endswith(b"\r")), Path(self.log).read_bytes())
        self.assertIn(b"hello", Path(self.log).read_bytes())


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class RetireTests(unittest.TestCase):
    def setUp(self):
        self.m = Mux(self)
        self.tmux = lambda *a: subprocess.run(["tmux"] + list(a), env=dict(self.m.env), capture_output=True, text=True)
        self.tmux("-f", self.m.conf, "new-session", "-d", "-s", "mc-main", "-n", "Boss", "sleep 300")
        for name in ("eng-1155", "eng-20"):
            self.tmux("new-window", "-d", "-t", "mc-main", "-n", name, "sleep 300")
        self.addCleanup(self.tmux, "kill-server")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mp = sandbox.load(tmp.name, "mp")
        self.ctx = mock.patch.dict(os.environ, {"TMUX_TMPDIR": self.m.dir})
        self.ctx.start()
        self.addCleanup(self.ctx.stop)

    def windows(self):
        return self.tmux("list-windows", "-t", "mc-main", "-F", "#W").stdout.split()

    def kill(self, tab):
        with mock.patch.object(self.mp.C, "http_json", return_value=(200, {})), \
             mock.patch.object(self.mp.C, "stop_recorder", return_value={}):
            return self.mp.do_kill(["%s/main:%s" % (self.mp.HOST_ID, tab), "--reason", "done"])

    def test_killing_an_agent_closes_its_window_and_only_its_window(self):
        self.assertEqual(0, self.kill("eng-20"))
        self.assertEqual(["Boss", "eng-1155"], self.windows())

    def test_a_name_that_is_a_prefix_of_a_live_agent_never_closes_it(self):
        self.assertEqual(0, self.kill("eng-11"))
        self.assertIn("eng-1155", self.windows())

    def test_a_viewer_on_the_dead_window_goes_and_one_that_moved_stays(self):
        for name, window in (("_v_eng-20_1_1", "eng-20"), ("_v_eng-20_2_2", "eng-1155"), ("_v_eng-2_3_3", "eng-20")):
            self.tmux("new-session", "-d", "-t", "mc-main", "-s", name)
            self.tmux("select-window", "-t", "=%s:=%s" % (name, window))
        self.assertEqual(0, self.kill("eng-20"))
        left = self.tmux("list-sessions", "-F", "#S").stdout.split()
        self.assertNotIn("_v_eng-20_1_1", left)
        self.assertIn("_v_eng-20_2_2", left)
        self.assertIn("_v_eng-2_3_3", left)

    def test_a_window_that_will_not_close_is_reported(self):
        with mock.patch.object(self.mp, "window_exists", return_value=True):
            self.assertEqual(1, self.kill("eng-20"))


if __name__ == "__main__":
    unittest.main()
