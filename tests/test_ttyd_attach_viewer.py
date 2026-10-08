"""A ttyd viewer session never outlives the client that opened it (card 8f490e73e5).

ttyd-attach.sh relied on `set-option destroy-unattached` at the end of its command chain, which
never runs when select-window fails (the tab is gone) or when ttyd kills the viewer first: the
_v_ session then stayed behind with nobody attached, for 10 hours in the case found.
"""
import os
from pathlib import Path
import pty
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin" / "ttyd-attach.sh"


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class ViewerTests(unittest.TestCase):
    def test_a_viewer_session_goes_when_ttyd_closes_it(self):
        mux = tempfile.mkdtemp(dir="/tmp")  # tmux sockets need a short path
        self.addCleanup(shutil.rmtree, mux, True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMUX_TMPDIR": mux, "HOME": mux,
               "TERM": "xterm"}
        sessions = lambda: subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], env=env,
                                          capture_output=True, text=True).stdout.split()
        subprocess.run(["tmux", "new-session", "-d", "-s", "mc-t", "-n", "w"], env=env, check=True)
        self.addCleanup(subprocess.run, ["tmux", "kill-server"], env=env, capture_output=True)
        pid, fd = pty.fork()
        if pid == 0:  # the viewer, as ttyd runs it, on a tab that no longer exists
            os.execve("/bin/bash", ["bash", str(SCRIPT), "-t", "mc-t:gone"], env)
        deadline = time.time() + 5
        while time.time() < deadline and not any(s.startswith("_v_") for s in sessions()):
            time.sleep(0.05)
        self.assertTrue(any(s.startswith("_v_") for s in sessions()), "viewer never attached")
        os.kill(pid, signal.SIGHUP)  # how ttyd closes a viewer
        os.close(fd)
        os.waitpid(pid, 0)
        deadline = time.time() + 3
        while time.time() < deadline and any(s.startswith("_v_") for s in sessions()):
            time.sleep(0.05)
        self.assertEqual(["mc-t"], sessions())


if __name__ == "__main__":
    unittest.main()
