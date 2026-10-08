"""Birth-zombie detection: deciding a live window never became a working agent.

is_birth_zombie() authorises killing a window, so a false positive murders a healthy agent
mid-turn and a false negative leaves a deaf one parked forever. Every clause of the signal triad
(card 157dcb7c75) gets a test that flips exactly that clause and nothing else.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


def load_mp(home):
    """Load the packaged mp against a throwaway INSTALL_DIR.

    Ambient env is pinned for the same reason as test_roles/test_backends: mpcommon lets the live
    environment override the config file, and an unpinned HOST_ID/QUEUE_URL makes this suite talk
    to a developer's real queue.
    """
    state = Path(home) / "state"
    config = Path(home) / "queue.env"
    config.write_text(
        'export INSTALL_DIR="%s"\n'
        'export HOST_ID="test-node"\n'
        'export QUEUE_URL="http://127.0.0.1:1"\n'
        'export QUEUE_SECRET="secret"\n'
        'export TTYD_PORT="7681"\n'
        'export DEFAULT_BACKEND="claude"\n'
        'export DEFAULT_ENG_MODEL="claude-opus-4-8"\n'
        'export DEFAULT_CLAUDE_MODEL="claude-opus-4-8"\n'
        'export DEFAULT_CODEX_MODEL=""\n'
        'export DEFAULT_GROK_MODEL=""\n' % state,
        encoding="utf-8",
    )
    with mock.patch.dict(os.environ, {
        "HOME": str(home),
        "MYPEOPLE_CONFIG_PATH": str(config),
        "MYPEOPLE_HOME": str(state),
        "INSTALL_DIR": str(state),
        "HOST_ID": "test-node",
        "QUEUE_URL": "http://127.0.0.1:1",
        "QUEUE_SECRET": "secret",
        "DEFAULT_BACKEND": "claude",
    }, clear=False):
        sys.modules.pop("mpcommon", None)
        sys.modules.pop("mprole", None)
        sys.path.insert(0, str(BIN))
        try:
            name = "mypeople_test_zmp_%s" % id(home)
            loader = importlib.machinery.SourceFileLoader(name, str(BIN / "mp"))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(BIN))


AID = "test-node/main:eng-9"


def write_status_file(mp, aid, status, ts, session_id=""):
    p = Path(mp.status_path(aid))
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"status": status, "timestamp": ts, "backend": "claude", "state": "alive"}
    if session_id:
        doc["session_id"] = session_id
    p.write_text(json.dumps(doc), encoding="utf-8")


class BirthZombieTests(unittest.TestCase):
    """A healthy record is one clause away from each verdict, so each test flips one clause."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.mp = load_mp(self._td.name)
        self.now = 10_000.0
        # stale enough to trip the age clause, with a window that exists and an idle pane
        self.stale_ts = self.now - (self.mp.ZOMBIE_STARTING_SEC + 10)
        self.rr = {"agent_id": AID, "backend": "claude", "retired": False, "is_master": False}
        self._window = mock.patch.object(self.mp, "window_exists", return_value=True)
        self._working = mock.patch.object(self.mp, "pane_looks_working", return_value=False)
        self._window.start(); self.addCleanup(self._window.stop)
        self._working.start(); self.addCleanup(self._working.stop)
        write_status_file(self.mp, AID, "starting", self.stale_ts)

    def verdict(self):
        return self.mp.is_birth_zombie(AID, self.rr, now=self.now)

    def test_the_baseline_case_is_a_zombie(self):
        """No session_id, stuck at starting, stale, window up, pane not working."""
        self.assertTrue(self.verdict())

    def test_a_written_session_id_is_never_a_zombie(self):
        """session_id means a lifecycle hook fired: the agent booted."""
        write_status_file(self.mp, AID, "starting", self.stale_ts, session_id="abc123")
        self.assertFalse(self.verdict())

    def test_a_session_id_on_the_roster_alone_is_enough(self):
        self.rr["session_id"] = "from-roster"
        self.assertFalse(self.verdict())

    def test_a_status_past_starting_is_never_a_zombie(self):
        """Anything past 'starting' means the agent reported in."""
        write_status_file(self.mp, AID, "working", self.stale_ts)
        self.assertFalse(self.verdict())

    def test_a_recent_starting_status_is_not_yet_a_zombie(self):
        """Slow boots are normal; only staleness distinguishes them from a hang."""
        write_status_file(self.mp, AID, "starting", self.now - 1)
        self.assertFalse(self.verdict())

    def test_a_working_pane_is_never_a_zombie(self):
        """The load-bearing guard: an in-flight turn must not be killed under it."""
        with mock.patch.object(self.mp, "pane_looks_working", return_value=True):
            self.assertFalse(self.verdict())

    def test_a_missing_window_is_not_a_zombie(self):
        """No window is reconcile's job, not the reaper's."""
        with mock.patch.object(self.mp, "window_exists", return_value=False):
            self.assertFalse(self.verdict())

    def test_a_retired_agent_is_not_a_zombie(self):
        self.rr["retired"] = True
        self.assertFalse(self.verdict())

    def test_the_master_is_never_a_zombie(self):
        """The Boss is the supervisor's to restart, never the reaper's."""
        self.rr["is_master"] = True
        self.assertFalse(self.verdict())

    def test_a_missing_status_file_is_not_a_zombie(self):
        """No timestamp means no evidence of staleness -- refuse to guess."""
        os.remove(self.mp.status_path(AID))
        self.assertFalse(self.verdict())

    def test_an_empty_record_is_not_a_zombie(self):
        self.assertFalse(self.mp.is_birth_zombie(AID, {}, now=self.now))


class ZombieReapCapTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.mp = load_mp(self._td.name)

    def test_reaping_stops_at_the_cap(self):
        """A host that cannot boot the backend at all must not respawn forever."""
        rr = {"agent_id": AID, "backend": "claude", "zombie_reaps": self.mp.ZOMBIE_REAP_MAX}
        with mock.patch.object(self.mp, "do_spawn") as spawn:
            ok, detail = self.mp.reap_birth_zombie(AID, rr)
        self.assertFalse(ok)
        self.assertIn("zombie-reap-cap", detail)
        spawn.assert_not_called()

    def test_a_reap_below_the_cap_respawns_and_counts_up(self):
        rr = {"agent_id": AID, "session": "main", "tab": "eng-9", "backend": "claude",
              "zombie_reaps": 0}
        self.mp.save_roster({AID: dict(rr)})
        with mock.patch.object(self.mp, "do_spawn", return_value=0), \
             mock.patch.object(self.mp, "pane_looks_ready", return_value=True), \
             mock.patch.object(self.mp.subprocess, "run"), \
             mock.patch.object(self.mp.C, "http_json", return_value=(200, {})), \
             mock.patch.object(self.mp.C, "tmux_send_message"):
            ok, detail = self.mp.reap_birth_zombie(AID, rr)
        self.assertTrue(ok, detail)
        self.assertEqual(1, self.mp.load_roster()[AID]["zombie_reaps"])

    def test_a_reaped_agent_never_carries_its_dead_session_id_forward(self):
        """Resuming a session that never started is the trap the reap exists to break."""
        rr = {"agent_id": AID, "session": "main", "tab": "eng-9", "backend": "claude",
              "session_id": "never-booted", "zombie_reaps": 0}
        self.mp.save_roster({AID: dict(rr)})
        with mock.patch.object(self.mp, "do_spawn", return_value=0), \
             mock.patch.object(self.mp, "pane_looks_ready", return_value=True), \
             mock.patch.object(self.mp.subprocess, "run"), \
             mock.patch.object(self.mp.C, "http_json", return_value=(200, {})), \
             mock.patch.object(self.mp.C, "tmux_send_message"):
            self.mp.reap_birth_zombie(AID, rr)
        self.assertEqual("", self.mp.load_roster()[AID]["session_id"])


if __name__ == "__main__":
    unittest.main()
