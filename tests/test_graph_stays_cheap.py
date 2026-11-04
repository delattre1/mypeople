"""An open Graph must not cost the Mac, and a dead tile must not stay dead.

Measured on Mac 1, 2026-11-04, 35 tiles. The dashed edge animation alone held 90% of a core in
WebKit's GPU process, with 0 terminals connected, in a window parked as a Stage Manager thumbnail,
which WebKit still counts as visible. The tiles' status-line jobs, one set per client on a timer
in the one tmux server, took a real client's keystroke p90 from 0.4 ms to 299 ms. And after a ttyd
recycle every tile closed with 1006 and none came back: ttyd's client does not reconnect.
"""
import json
import os
from pathlib import Path
import pty
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import sandbox

BIN = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"
PAGE = (BIN / "terminal-graph.html").read_text()


class EdgesAreStatic(unittest.TestCase):
    def test_no_edge_is_animated(self):
        css = PAGE[:PAGE.index("</style>")]
        for rule in re.findall(r"\.edge[^{]*\{[^}]*\}", css):
            self.assertNotIn("animation", rule, rule)
        self.assertNotIn("stroke-dashoffset", css, "a moving dash repaints every edge every frame")


@unittest.skipUnless(shutil.which("node"), "needs node")
class DeadTilesHeal(unittest.TestCase):
    """healTile, run for real: lifted out of the page, since a tile's frame only exists in a browser."""

    def run_heal(self, polls):
        fn = re.search(r"const TILE_WAIT=.*?\nfunction reconcile", PAGE, re.S).group(0).rsplit("\n", 1)[0]
        js = fn + """
const frame={};const reloads=[];let roPort=7682;const ttyUrl=(p,t)=>{return p+t};
Object.defineProperty(frame,"src",{set(v){reloads.push([now,v])}});
let now=0;for(const [t,viewers] of %s){now=t;healTile(frame,{target:"T",viewers:viewers===null?undefined:viewers},t)}
console.log(JSON.stringify(reloads.map(r=>r[0])));""" % json.dumps(polls)
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
        self.assertEqual(0, out.returncode, out.stderr)
        return json.loads(out.stdout)

    def test_a_live_tile_is_left_alone(self):
        self.assertEqual([], self.run_heal([[t, 1] for t in range(0, 600000, 2000)]))

    def test_a_tile_with_no_viewer_reloads_after_the_wait(self):
        self.assertEqual([32000], self.run_heal([[0, 1], [2000, 0], [30000, 0], [32000, 0], [34000, 1], [90000, 1]]))

    def test_one_that_cannot_attach_backs_off_instead_of_looping(self):
        got = self.run_heal([[t, 0] for t in range(0, 900001, 2000)])
        self.assertGreaterEqual(len(got), 3)
        gaps = [b - a for a, b in zip(got, got[1:])]
        self.assertEqual(gaps, sorted(gaps), "each retry waits at least as long as the last")
        self.assertLessEqual(max(gaps), 302000)

    def test_tmux_not_answering_never_reloads_a_tile(self):
        self.assertEqual([], self.run_heal([[t, None] for t in range(0, 600000, 2000)]))

    def test_the_page_really_calls_it_on_every_poll(self):
        self.assertIn("healTile(frame,n,Date.now())", PAGE)


class OnlyWhatIsInViewCosts(unittest.TestCase):
    """At 34 tiles, 11 in view: 2.4 s of attach against 235 ms for 9, and 533 MB of page against 152 MB."""

    def test_a_new_tile_waits_until_it_is_seen(self):
        self.assertIn("new IntersectionObserver", PAGE)
        self.assertIn("tileSeen.observe(el)", PAGE)
        update = re.search(r"function updateNode\(.*?\n", PAGE).group(0)
        self.assertIn("if(frame.dataset.on)frame.src=", update, "a poll must not attach an unseen tile")
        self.assertIn("if(frame.dataset.on)healTile", update, "an unseen tile has no viewer and is not dead")

    def test_a_poll_that_changed_nothing_touches_nothing(self):
        update = re.search(r"function updateNode\(.*?\n", PAGE).group(0)
        self.assertIn("if(el.dataset.label!==", update)
        draw = re.search(r"function drawEdges\(\).*?\n", PAGE).group(0)
        self.assertRegex(draw, r"if\(now===edgesDrawn\)return")


class ServerSaysWhichTilesAreLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.srv = sandbox.load(self.tmp.name, "todo-server.py")

    def viewers(self, stdout, returncode=0):
        with mock.patch.object(self.srv.subprocess, "run",
                               lambda *a, **k: mock.Mock(returncode=returncode, stdout=stdout)):
            return self.srv._tmux_viewers()

    def test_counts_viewer_sessions_per_window(self):
        got = self.viewers("mc-main\n_v_eng-1_123_456\n_v_eng-1_124_7\n_v_my_tab_9_8\nmc-rt\n")
        self.assertEqual({"eng-1": 2, "my_tab": 1}, got)

    def test_no_answer_is_none_not_zero(self):
        self.assertIsNone(self.viewers("", returncode=1))
        with mock.patch.object(self.srv.subprocess, "run", side_effect=OSError("no tmux")):
            self.assertIsNone(self.srv._tmux_viewers())


class AgentsReadsTheRosterOnce(unittest.TestCase):
    """Every open Graph asks every 2 s. A read per row was 35 parses of 700 KB, 95 ms, under LOCK."""

    def test_the_whole_list_shares_one_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = sandbox.load(tmp, "queue-server.py")
            srv.AGENTS.update({"h/main:e%d" % i: {"host": "h"} for i in range(5)})
            reads = []
            real = srv.load_roster
            srv.load_roster = lambda: (reads.append(1), real())[1]
            roster = srv.load_roster()
            rows = [srv.agent_row(a, r, roster) for a, r in srv.AGENTS.items()]
            self.assertEqual(5, len(rows))
            self.assertEqual(1, len(reads))
        code = (BIN / "queue-server.py").read_text()
        self.assertIn("agent_row(a, r, roster) for a, r in AGENTS.items()", code)


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class TilesRunNoStatusJobs(unittest.TestCase):
    """Against a real private tmux server: a tile's session gets no jobs, an interactive one keeps them."""

    def attach(self, *args):
        mux = tempfile.mkdtemp(dir="/tmp")  # tmux sockets need a short path
        self.addCleanup(shutil.rmtree, mux, True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMUX_TMPDIR": mux, "HOME": mux, "TERM": "xterm"}
        tm = lambda *a: subprocess.run(["tmux", *a], env=env, capture_output=True, text=True).stdout
        subprocess.run(["tmux", "new-session", "-d", "-s", "mc-t", "-n", "w"], env=env, check=True)
        self.addCleanup(subprocess.run, ["tmux", "kill-server"], env=env, capture_output=True)
        tm("set-option", "-g", "status-right", "#(echo job)")
        tm("set-option", "-g", "status-interval", "5")
        pid, fd = pty.fork()
        if pid == 0:
            os.execve("/bin/bash", ["bash", str(BIN / "ttyd-attach.sh"), *args, "-t", "mc-t:w"], env)
        # master closed before the wait: macOS holds an exiting process until its pty output drains
        self.addCleanup(lambda: (os.kill(pid, 9), os.close(fd), os.waitpid(pid, 0)))
        for _ in range(100):
            v = [s for s in tm("list-sessions", "-F", "#{session_name}").split() if s.startswith("_v_")]
            if v and "destroy-unattached" in tm("show-options", "-t", v[0]) or v and args:
                return tm, v[0]
            time.sleep(0.05)
        self.fail("no viewer session")

    def test_a_tile_has_no_status_job_and_no_status_timer(self):
        tm, v = self.attach("--viewer")
        for _ in range(100):
            if "status-interval 0" in tm("show-options", "-t", v):
                break
            time.sleep(0.05)
        self.assertIn("status-right ''", tm("show-options", "-t", v))
        self.assertIn("status-interval 0", tm("show-options", "-t", v))

    def test_an_interactive_terminal_keeps_its_status_line(self):
        tm, v = self.attach()
        self.assertNotIn("status-right", tm("show-options", "-t", v))

    def test_the_supervisor_starts_only_the_read_only_ttyd_as_tiles(self):
        sup = (BIN / "supervise.sh").read_text()
        self.assertRegex(sup, r"exec ttyd -a -p \$TTYD_RO_PORT .*ttyd-attach\.sh' --viewer\"")
        self.assertNotRegex(sup, r"exec ttyd -W .*--viewer")


if __name__ == "__main__":
    unittest.main()
