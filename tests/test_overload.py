"""An overloaded Mac never empties the fleet, loses a send, or starts a second tmux server (card a4607576c1).

11-04 19:12: at load 94 the registry emptied for over a minute and a send to a live Boss failed. Both were
proven without loading anything:

* The heartbeat did 2 tmux calls per agent BEFORE it told the queue it was alive. Replay with the real
  daemons, 35 agents and a tmux that only sleeps 1.36 s (the delay measured): every agent gone at t=46 s.
* tmux serves on one thread with about 128 connections queued behind it. Past that a client is refused and
  says "no server running", rc 1, in 0.03 s, with the server alive. That read as no pane, no window, no
  session; and `tmux new-session` on it deleted the socket and started a second server, stranding the fleet.

The stalled server here is a private one frozen with SIGSTOP. The waiting clients sleep; nothing burns CPU.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.request
import sandbox

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"
RAW_LOG = "import os,sys,tty; tty.setraw(0); f=open(sys.argv[1],'wb',0)\nwhile True: f.write(os.read(0,4096))"


def wait_for(cond, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class StalledTmuxTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")  # tmux sockets need a short path
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TMUX_TMPDIR": self.dir, "HOME": self.dir,
                    "TERM": "xterm-256color"}
        self.log = os.path.join(self.dir, "raw.bin")
        self.script = os.path.join(self.dir, "r.py")
        Path(self.script).write_text(RAW_LOG)
        self.tmux("new-session", "-d", "-s", "mc-main", "-n", "eng-1", "-x", "120", "-y", "39",
                  "%s %s %s" % (sys.executable, self.script, self.log))
        self.addCleanup(self.tmux, "kill-server")
        self.pid = int(self.tmux("display", "-p", "#{pid}").stdout)
        self.addCleanup(self.thaw)
        self.sock = self.tmux("display", "-p", "#{socket_path}").stdout.strip()
        self.inode = os.stat(self.sock).st_ino
        self.held = []
        patch = mock.patch.dict(os.environ, {"TMUX_TMPDIR": self.dir})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("TMUX", None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.C = sandbox.load(tmp.name, "mpcommon.py")

    def tmux(self, *args):
        return subprocess.run(["tmux"] + list(args), env=self.env, capture_output=True, text=True, timeout=20)

    def thaw(self):
        try:
            os.kill(self.pid, signal.SIGCONT)
        except OSError:
            pass
        for p in self.held:
            p.kill()
            p.wait()
        self.held = []

    def stall(self):
        """Freeze the server and queue clients until the next one is refused."""
        os.kill(self.pid, signal.SIGSTOP)
        for i in range(400):
            self.held.append(subprocess.Popen(["tmux", "list-panes"], env=self.env, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL))
            if i % 16 == 15:
                time.sleep(0.2)
                if any(p.poll() not in (None, 0) for p in self.held):
                    return
        self.fail("the backlog never filled")

    def test_a_refused_client_is_what_a_stall_looks_like(self):
        self.stall()
        r = subprocess.run(["tmux", "list-panes", "-t", "mc-main:eng-1"], env=self.env, capture_output=True, text=True)
        self.assertEqual(1, r.returncode)
        self.assertIn("no server running", r.stderr)
        self.assertFalse(self.C.tmux_server_gone(), "the server is alive, only stalled")

    def test_a_stalled_server_reads_as_unknown_never_as_no_windows(self):
        self.stall()
        self.assertIsNone(self.C.tmux_windows(wait=0.3))

    def test_a_send_rides_out_the_stall_and_pastes_once(self):
        self.stall()
        result = []
        import threading
        sender = threading.Thread(target=lambda: result.append(self.C.tmux_send_message("mc-main:eng-1", "hello")))
        sender.start()
        time.sleep(1.5)
        self.thaw()
        sender.join(30)
        self.assertEqual([True], result)
        self.assertTrue(wait_for(lambda: Path(self.log).read_bytes().endswith(b"\r")))
        self.assertEqual(1, Path(self.log).read_bytes().count(b"hello"))

    def test_an_enter_that_fails_does_not_paste_the_message_again(self):
        keys = iter([False, True])
        with mock.patch.object(self.C, "pane_key", lambda t, k: next(keys)), mock.patch.object(self.C.time, "sleep"):
            self.assertTrue(self.C.tmux_send_message("mc-main:eng-1", "hello"))
        self.assertTrue(wait_for(lambda: b"hello" in Path(self.log).read_bytes()))
        self.assertEqual(1, Path(self.log).read_bytes().count(b"hello"))

    def test_spawning_on_a_stalled_server_never_starts_a_second_one(self):
        mp = sandbox.load(self.tmp, "mp")
        self.stall()
        with mock.patch.object(mp.C, "TMUX_STALL_WAIT", 0.3):
            r = mp.start_tmux_session(["-d", "-s", "mc-other", "-n", "x", "sleep 300"], dict(self.env))
        self.assertNotEqual(0, r.returncode)
        self.assertEqual(self.inode, os.stat(self.sock).st_ino, "the socket was taken from the live server")
        self.thaw()
        self.assertEqual(str(self.pid), self.tmux("display", "-p", "#{pid}").stdout.strip())

    def test_a_viewer_can_never_start_a_second_server(self):
        script = (BIN / "ttyd-attach.sh").read_text()
        self.assertIn('tmux -N new-session -t "$SESS"', script)


class AnyOtherTmuxFailureTests(unittest.TestCase):
    """review-watch on 4c2a59d: 5.22.154 returned "no windows" for any failure that was not a refused
    connection, so one odd error from a live server would retire every agent and respawn them."""
    FAILURE = mock.Mock(returncode=1, stdout="", stderr="protocol version mismatch (client 8, server 9)")

    def load(self, name, roster):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        module = sandbox.load(tmp.name, name)
        Path(module.ROSTER_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(module.ROSTER_PATH).write_text(json.dumps(roster))
        return module

    def alive(self):
        return {"test-node/main:eng-1": {"agent_id": "test-node/main:eng-1", "session": "main", "tab": "eng-1",
                                         "retired": False}}

    def test_a_live_server_that_fails_oddly_is_unknown_not_empty(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        C = sandbox.load(tmp.name, "mpcommon.py")
        with mock.patch.object(C.subprocess, "run", return_value=self.FAILURE), \
             mock.patch.object(C, "tmux_server_gone", return_value=False):
            self.assertIsNone(C.tmux_windows())

    def test_a_server_really_gone_is_empty_so_a_crash_still_recovers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        C = sandbox.load(tmp.name, "mpcommon.py")
        with mock.patch.object(C.subprocess, "run", return_value=self.FAILURE), \
             mock.patch.object(C, "tmux_server_gone", return_value=True):
            self.assertEqual(set(), C.tmux_windows())

    def test_the_heartbeat_retires_nobody_on_it(self):
        q = self.load("queue-client.py", self.alive())
        with mock.patch.object(q.C.subprocess, "run", return_value=self.FAILURE), \
             mock.patch.object(q.C, "tmux_server_gone", return_value=False), \
             mock.patch.object(q, "auth_health", return_value={"state": "live"}):
            self.assertEqual([], q.live_agents())
        self.assertFalse(json.loads(Path(q.ROSTER_PATH).read_text())["test-node/main:eng-1"]["retired"])

    def test_reconcile_respawns_nobody_on_it(self):
        mp = self.load("mp", self.alive())
        with mock.patch.object(mp.subprocess, "run", return_value=self.FAILURE), \
             mock.patch.object(mp.C, "tmux_server_gone", return_value=False), \
             mock.patch.object(mp, "is_local", return_value=True), \
             mock.patch.object(mp, "do_revive") as revive, mock.patch.object(mp, "do_spawn") as spawn:
            self.assertEqual(0, mp.do_reconcile([]))
        revive.assert_not_called()
        spawn.assert_not_called()


class ServerGoneTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.C = sandbox.load(tmp.name, "mpcommon.py")

    def gone(self, lsof, platform="darwin"):
        sock = Path(self.dir) / ("tmux-%d" % os.getuid()) / "default"
        sock.parent.mkdir(exist_ok=True)
        if not sock.exists():
            left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            left.bind(str(sock))        # what a crash leaves: the socket's file, and nothing listening on it
            left.close()
        with mock.patch.object(self.C.subprocess, "run", return_value=mock.Mock(stdout=lsof)) as run, \
                mock.patch.object(self.C.sys, "platform", platform):
            self.ran = run.called or None
            gone = self.C.tmux_server_gone({"TMUX_TMPDIR": self.dir})
            self.ran = run.called
            return gone

    def test_no_socket_means_gone(self):
        self.assertTrue(self.C.tmux_server_gone({"TMUX_TMPDIR": self.dir}))

    def test_a_host_without_uids_reads_as_gone(self):
        # A Windows port on psmux has no os.getuid, no tmux-<uid> socket directory and no ps. Without the guard this raised
        # AttributeError inside ensure-boss and the Boss never started (5.22.158). Such a host cannot tell a stalled server from a gone one, so a refused command is final.
        with mock.patch.object(self.C.os, "getuid", create=True):
            del self.C.os.getuid
            self.assertTrue(self.C.tmux_server_gone({"TMUX_TMPDIR": self.dir}))

    def test_a_stale_socket_after_a_crash_means_gone_so_the_fleet_recovers(self):
        self.assertTrue(self.gone("p900\nf5\nn/private/tmp/tmux-501/other\n"))

    def test_a_tmux_holding_this_very_socket_means_it_may_only_be_stalled(self):
        own = os.path.join(self.dir, "tmux-%d" % os.getuid(), "default")
        self.assertFalse(self.gone("p900\nf5\nn%s\n" % own))
        self.assertFalse(self.gone("p900\nf5\nn%s\n" % os.path.realpath(own)), "macOS names /tmp as /private/tmp")

    def test_a_server_for_a_private_socket_never_counts_as_the_default_one(self):
        private = os.path.join(self.dir, "tmux-%d" % os.getuid(), "ck-swarm-swarm0")
        self.assertTrue(self.gone("p900\nf5\nn%s\n" % private))

    def test_on_linux_a_refusal_is_final_and_nothing_else_is_asked(self):
        self.assertTrue(self.gone("anything", platform="linux"))
        self.assertFalse(self.ran)


@unittest.skipUnless(shutil.which("tmux") and hasattr(os, "getuid"), "needs tmux")
class RealPrivateServerTests(unittest.TestCase):
    """A real private server running and the default socket dead: what a builder's Mac had (11-05)."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.env = {k: v for k, v in os.environ.items() if k != "TMUX"}   # $TMUX would point tmux at the server this runs in
        self.env["TMUX_TMPDIR"] = self.dir
        self.C = sandbox.load(tmp.name, "mpcommon.py")

    def server(self, *private):
        subprocess.run(["tmux", *private, "-f", "/dev/null", "new-session", "-d", "-s", "t"], env=self.env, check=True)
        self.addCleanup(subprocess.run, ["tmux", *private, "kill-server"], env=self.env, capture_output=True)

    def test_a_private_server_with_the_default_socket_dead_reads_as_gone(self):
        self.server("-L", "mp-private-test")
        sock = os.path.join(self.dir, "tmux-%d" % os.getuid(), "default")
        left = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        left.bind(sock)
        left.close()
        self.assertTrue(self.C.tmux_server_gone(self.env))

    def test_a_live_default_server_beside_it_reads_as_there(self):
        self.server("-L", "mp-private-test")
        self.server()
        self.assertFalse(self.C.tmux_server_gone(self.env))


class ReaperTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.q = sandbox.load(tmp.name, "queue-server.py")

    def test_a_silent_own_host_keeps_its_fleet_and_a_silent_remote_host_expires(self):
        own, other = self.q.HOST_ID, "other-mac"
        old = time.time() - 1000
        self.q.CLIENTS.update({own: {"last_seen": old}, other: {"last_seen": old}})
        self.q.AGENTS.update({"%s/main:a" % own: {"host": own}, "%s/main:b" % other: {"host": other}})
        self.q.reap(time.time())
        self.assertEqual({"%s/main:a" % own}, set(self.q.AGENTS))
        self.assertEqual({own}, set(self.q.CLIENTS))


class CensusTests(unittest.TestCase):
    def queue(self, roster):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        q = sandbox.load(tmp.name, "queue-client.py")
        Path(q.ROSTER_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(q.ROSTER_PATH).write_text(json.dumps(roster))
        return q

    def roster(self, n=35):
        return {"test-node/main:eng-%d" % i: {"agent_id": "test-node/main:eng-%d" % i, "session": "main",
                                              "tab": "eng-%d" % i, "retired": False} for i in range(n)}

    def test_an_unreadable_tmux_retires_nobody(self):
        q = self.queue(self.roster())
        with mock.patch.object(q.C, "tmux_windows", return_value=None), \
             mock.patch.object(q, "auth_health", return_value={"state": "live"}):
            self.assertEqual([], q.live_agents())
        self.assertFalse(any(r["retired"] for r in json.loads(Path(q.ROSTER_PATH).read_text()).values()))

    def test_the_census_costs_one_tmux_call_however_big_the_fleet(self):
        q = self.queue(self.roster())
        windows = "".join("mc-main:eng-%d\n" % i for i in range(35))
        with mock.patch.object(q.C.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=windows, stderr="")) as run, \
             mock.patch.object(q, "auth_health", return_value={"state": "live"}):
            self.assertEqual(35, len(q.live_agents()))
        self.assertEqual(1, sum(c.args[0][0] == "tmux" for c in run.call_args_list))


class ReconcileTests(unittest.TestCase):
    def mp(self, roster):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mp = sandbox.load(tmp.name, "mp")
        Path(mp.ROSTER_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(mp.ROSTER_PATH).write_text(json.dumps(roster))
        return mp

    def test_an_unreadable_tmux_revives_nobody(self):
        aid = "test-node/main:eng-1"
        mp = self.mp({aid: {"agent_id": aid, "session": "main", "tab": "eng-1", "retired": False}})
        with mock.patch.object(mp.C, "tmux_windows", return_value=None), \
             mock.patch.object(mp, "do_revive") as revive, mock.patch.object(mp, "do_spawn") as spawn:
            self.assertEqual(0, mp.do_reconcile([]))
        revive.assert_not_called()
        spawn.assert_not_called()

    def test_six_hundred_retired_agents_cost_no_tmux_calls(self):
        mp = self.mp({"test-node/main:eng-%d" % i: {"agent_id": "test-node/main:eng-%d" % i, "session": "main",
                                                    "tab": "eng-%d" % i, "retired": True, "retire_reason": "done"}
                      for i in range(600)})
        with mock.patch.object(mp.C, "tmux_windows", return_value=set()), \
             mock.patch.object(mp.subprocess, "run") as run:
            self.assertEqual(0, mp.do_reconcile([]))
        run.assert_not_called()

    def test_an_unreadable_tmux_leaves_the_boss_alone(self):
        aid = "test-node/main:Boss"
        mp = self.mp({aid: {"agent_id": aid, "session": "main", "tab": "Boss", "is_master": True, "retired": False}})
        with mock.patch.object(mp.C, "tmux_windows", return_value=None), \
             mock.patch.object(mp, "do_revive") as revive:
            self.assertEqual(0, mp.do_ensure_boss([aid]))
        revive.assert_not_called()
        self.assertFalse(json.loads(Path(mp.ROSTER_PATH).read_text())[aid]["retired"])


class SpawnOnAStalledServerTests(unittest.TestCase):
    """review-watch on 12b49de: new-window ran plain with its result ignored, so a refusal left a roster row
    and an /agents/register saying alive with no window. And window_exists read a refusal as "no window", so a
    spawn could start a second copy of a live agent once tmux answered again."""
    AID = "test-node/main:eng-9"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mp = sandbox.load(tmp.name, "mp", DEFAULT_BACKEND="claude")
        self.mp.ROLE_DIR = str(ROOT / "mypeople" / "runtime" / "roles")
        trust = mock.patch.object(self.mp, "pretrust")  # it edits ~/.claude.json
        trust.start()
        self.addCleanup(trust.stop)
        self.calls = []
        self.register = mock.patch.object(self.mp.C, "http_json", return_value=(200, {}))
        self.posts = self.register.start()
        self.addCleanup(self.register.stop)

    def fake_tmux(self, refuse=()):
        def run(argv, *a, **k):
            if argv[0] != "tmux":
                return mock.Mock(returncode=0, stdout="", stderr="")
            self.calls.append(argv[1])
            if argv[1] in refuse:
                return mock.Mock(returncode=1, stdout="", stderr="no server running on /tmp/tmux-501/default")
            out = "mc-main:Boss\n" if "-a" in argv else "Boss\n" if argv[1] == "list-windows" else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")
        return mock.patch.object(self.mp.subprocess, "run", side_effect=run)

    def spawn(self, refuse=()):
        with self.fake_tmux(refuse), mock.patch.object(self.mp.C, "tmux_server_gone", return_value=False), \
             mock.patch.object(self.mp.C, "TMUX_STALL_WAIT", 0), mock.patch.object(self.mp.C.time, "sleep"), \
             mock.patch.object(self.mp, "wait_for_backend_ready", return_value=("", "pane")), \
             mock.patch.object(self.mp, "is_local", return_value=True):
            return self.mp.do_spawn([self.AID, "--temporary", "--role", "engineer", "--backend", "claude"])

    def assert_nothing_happened(self):
        self.assertNotIn(self.AID, self.mp.load_roster())
        self.assertFalse([c for c in self.posts.call_args_list if "/agents/register" in c.args[1]])

    def test_a_refused_new_window_leaves_no_agent_behind(self):
        self.assertEqual(1, self.spawn(refuse=("new-window",)))
        self.assert_nothing_happened()

    def test_an_unreadable_tmux_never_starts_a_second_copy(self):
        self.assertEqual(1, self.spawn(refuse=("list-windows",)))
        self.assertNotIn("new-window", self.calls)
        self.assertNotIn("new-session", self.calls)
        self.assert_nothing_happened()

    def test_a_healthy_tmux_still_spawns(self):
        created = []

        def run(argv, *a, **k):
            if argv[0] != "tmux":
                return mock.Mock(returncode=0, stdout="", stderr="")
            if argv[1] == "new-window":
                created.append(argv)
            names = ["Boss"] + (["eng-9"] if created else [])
            out = "".join("mc-main:%s\n" % n for n in names) if "-a" in argv else "\n".join(names) if argv[1] == "list-windows" else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")
        with mock.patch.object(self.mp.subprocess, "run", side_effect=run), \
             mock.patch.object(self.mp, "wait_for_backend_ready", return_value=("", "pane")), \
             mock.patch.object(self.mp, "is_local", return_value=True):
            self.assertEqual(0, self.mp.do_spawn([self.AID, "--temporary", "--role", "engineer", "--backend", "claude"]))
        self.assertEqual(1, len(created))
        self.assertIn(self.AID, self.mp.load_roster())


class HeartbeatSurvivesASlowCensus(unittest.TestCase):
    """The real daemons on private ports, as two hosts, with a tmux shim that only sleeps."""

    def test_a_census_that_outlasts_the_ttl_does_not_expire_the_host(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        os.makedirs(tmp + "/bin")
        for d in ("run", "status/mc-main", "todos", "logs"):
            os.makedirs(tmp + "/state/" + d)
        names = ["eng-%d" % i for i in range(5)]
        Path(tmp + "/delay").write_text("0")
        Path(tmp + "/bin/tmux").write_text(
            '#!/bin/bash\nsleep "$(cat %s/delay)"\ncase "$*" in *list-windows*-a*) for n in %s; do echo "mc-main:$n"; done;; '
            '*list-windows*) for n in %s; do echo "$n"; done;; *) exit 1;; esac\n' % (tmp, " ".join(names), " ".join(names)))
        os.chmod(tmp + "/bin/tmux", 0o755)
        Path(tmp + "/state/run/roster.json").write_text(json.dumps(
            {"far-mac/main:%s" % n: {"agent_id": "far-mac/main:%s" % n, "session": "main", "tab": n, "backend": "codex",
                                     "retired": False} for n in names}))
        keys = dict(INSTALL_DIR=tmp + "/state", QUEUE_URL="http://127.0.0.1:%d" % port, QUEUE_SECRET="t", HUD_PORT=port,
                    TODO_PORT=port + 1, BIND_ADDR="127.0.0.1", QUEUE_DEAD_AFTER=6, HEARTBEAT_INTERVAL=1,
                    DEFAULT_BACKEND="codex", DEFAULT_ENG_MODEL="x", HOST_ID="far-mac")
        Path(tmp + "/queue.env").write_text("".join('export %s="%s"\n' % kv for kv in keys.items()))
        env = {"PATH": tmp + "/bin:/usr/bin:/bin", "HOME": tmp, "MYPEOPLE_CONFIG_PATH": tmp + "/queue.env",
               "LANG": "en_US.UTF-8"}
        env.update({k: str(v) for k, v in keys.items()})
        procs = [subprocess.Popen([sys.executable, str(BIN / "queue-server.py")], env=dict(env, HOST_ID="central"), cwd=tmp,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                 subprocess.Popen([sys.executable, str(BIN / "queue-client.py")], env=env, cwd=tmp,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
        for p in procs:
            self.addCleanup(p.wait)
            self.addCleanup(p.kill)

        def agents():
            try:
                req = urllib.request.Request("http://127.0.0.1:%d/agents" % port, headers={"X-Queue-Secret": "t"})
                return len(json.load(urllib.request.urlopen(req, timeout=2)))
            except OSError:
                return -1
        self.assertTrue(wait_for(lambda: agents() == 5, 15), "never registered")
        Path(tmp + "/delay").write_text("60")  # the census now outlasts the 6 s TTL many times over
        end = time.time() + 14
        while time.time() < end:
            self.assertEqual(5, agents(), "the host expired while its census was slow")
            time.sleep(0.5)


if __name__ == "__main__":
    unittest.main()
