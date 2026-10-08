import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


def load_runtime(name, filename, home):
    config = Path(home) / "queue.env"
    state = Path(home) / "state"
    config.write_text(
        'export INSTALL_DIR="%s"\n'
        'export HOST_ID="test-node"\n'
        'export QUEUE_URL="http://127.0.0.1:9900"\n'
        'export QUEUE_SECRET="secret"\n'
        'export TTYD_PORT="7681"\n'
        'export DEFAULT_ENG_MODEL="claude-opus-4-8"\n'
        'export HEARTBEAT_INTERVAL="10"\n' % state,
        encoding="utf-8",
    )
    env = {"HOME": str(home), "MYPEOPLE_CONFIG_PATH": str(config),
           "MYPEOPLE_HOME": str(state), "INSTALL_DIR": str(state),
           "HOST_ID": "test-node", "QUEUE_URL": "http://127.0.0.1:9900",
           "QUEUE_SECRET": "secret", "TTYD_PORT": "7681",
           "DEFAULT_ENG_MODEL": "claude-opus-4-8", "HEARTBEAT_INTERVAL": "10"}
    with mock.patch.dict(os.environ, env, clear=False):
        sys.modules.pop("mpcommon", None)
        sys.path.insert(0, str(BIN))
        try:
            loader = importlib.machinery.SourceFileLoader(name, str(BIN / filename))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(BIN))


class RecorderOptInTests(unittest.TestCase):
    """Recording is OPT-IN, and the default is the load-bearing part.

    A recorder attaches a READ-ONLY tmux client. When it is the only client on a pane, tmux
    refuses `send-keys` with "client is read-only" -- so an always-on recorder makes every agent
    on a headless install unreachable. Default-off is therefore a correctness property, not a
    preference, and it is what these tests pin.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.home = self._td.name

    def load(self):
        # MYPEOPLE_RECORD is read at CALL time, so tests set it around the call, not the import.
        with mock.patch.dict(os.environ, {"MYPEOPLE_RECORD": ""}, clear=False):
            return load_runtime("mp_optin_%s" % id(self.home), "mp", self.home)

    def test_recording_is_off_by_default(self):
        mp = self.load()
        with mock.patch.dict(os.environ, {"MYPEOPLE_RECORD": ""}, clear=False):
            self.assertFalse(mp.recording_enabled_global())
            self.assertFalse(mp.want_recorder(False, {}))

    def test_recording_is_off_when_the_host_never_mentions_it(self):
        """The true default: the variable does not exist at all."""
        mp = self.load()
        env = {k: v for k, v in os.environ.items() if k != "MYPEOPLE_RECORD"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(mp.recording_enabled_global())
            self.assertFalse(mp.want_recorder(False, {}))

    def test_the_record_flag_opts_in(self):
        mp = self.load()
        with mock.patch.dict(os.environ, {"MYPEOPLE_RECORD": ""}, clear=False):
            self.assertTrue(mp.want_recorder(True, {}))

    def test_the_host_env_opts_in(self):
        mp = self.load()
        for truthy in ("1", "true", "yes", "on", "TRUE", "On"):
            with mock.patch.dict(os.environ, {"MYPEOPLE_RECORD": truthy}, clear=False):
                self.assertTrue(mp.recording_enabled_global(), truthy)
                self.assertTrue(mp.want_recorder(False, {}), truthy)

    def test_a_non_truthy_host_env_stays_off(self):
        mp = self.load()
        for falsy in ("0", "off", "no", "false", ""):
            with mock.patch.dict(os.environ, {"MYPEOPLE_RECORD": falsy}, clear=False):
                self.assertFalse(mp.recording_enabled_global(), falsy)

    def test_an_opted_in_agent_keeps_recording_across_a_respawn(self):
        """The roster carries the intent, so reconcile/revive does not silently stop recording."""
        mp = self.load()
        with mock.patch.dict(os.environ, {"MYPEOPLE_RECORD": ""}, clear=False):
            self.assertTrue(mp.want_recorder(False, {"record": True}))

    def test_an_opted_out_agent_stays_opted_out_across_a_respawn(self):
        mp = self.load()
        with mock.patch.dict(os.environ, {"MYPEOPLE_RECORD": ""}, clear=False):
            self.assertFalse(mp.want_recorder(False, {"record": False}))

    def test_a_legacy_roster_row_without_the_field_stays_off(self):
        mp = self.load()
        with mock.patch.dict(os.environ, {"MYPEOPLE_RECORD": ""}, clear=False):
            self.assertFalse(mp.want_recorder(False, {"backend": "claude"}))


class RecorderLifecycleTests(unittest.TestCase):
    def test_pid_fallback_terms_validated_attach_child_first(self):
        with tempfile.TemporaryDirectory() as td:
            common = load_runtime("test_recorder_common", "mpcommon.py", td)
            ident = common.recorder_identity("main", "eng-1", "test-node")
            metadata = dict(ident, pid=4242, server_pid=99)
            with mock.patch.object(common, "current_recorder", return_value=None), \
                 mock.patch.object(common, "_recorder_pid_matches", return_value=True), \
                 mock.patch.object(common, "_recorder_child_pids", return_value=[4243]), \
                 mock.patch.object(common, "_recorder_child_matches", return_value=True), \
                 mock.patch.object(common, "_process_alive", return_value=False), \
                 mock.patch.object(common, "_wait_for_exit", return_value=True), \
                 mock.patch.object(common.os, "kill") as kill:
                result = common.stop_recorder("main", "eng-1", metadata, "test-node")
            kill.assert_called_once_with(4243, signal.SIGTERM)
            self.assertEqual([4242], result["validated"])
            self.assertEqual([], result["parent_terms"])
            self.assertEqual([], result["survivors"])

    def test_mismatched_metadata_cannot_signal_reused_pid(self):
        with tempfile.TemporaryDirectory() as td:
            common = load_runtime("test_recorder_reuse", "mpcommon.py", td)
            metadata = {"pid": 4242, "target": "mc-main:someone-else",
                        "cast": str(Path(td) / "unrelated.cast")}
            with mock.patch.object(common, "current_recorder", return_value=None), \
                 mock.patch.object(common.os, "kill") as kill:
                result = common.stop_recorder("main", "eng-1", metadata, "test-node")
            kill.assert_not_called()
            self.assertEqual([], result["validated"])

    def test_explicit_kill_reaps_recorder_without_tmux_session_kill(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_runtime("test_recorder_mp", "mp", td)
            aid = "test-node/main:eng-1"
            metadata = dict(mp.C.recorder_identity("main", "eng-1", "test-node"), pid=4242)
            Path(mp.ROSTER_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(mp.ROSTER_PATH).write_text(json.dumps({aid: {
                "agent_id": aid, "session": "main", "tab": "eng-1",
                "recorder": metadata, "retired": False,
            }}))
            stopped = {"validated": [4242], "survivors": []}
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(mp.C, "stop_recorder", return_value=stopped) as stop, \
                 mock.patch.object(mp.C, "http_json", return_value=(200, {})), \
                 mock.patch.object(mp.subprocess, "run", return_value=completed) as run:
                self.assertEqual(0, mp.do_kill([aid, "--reason", "done"]))
            stop.assert_called_once_with("main", "eng-1", metadata, "test-node")
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn(["tmux", "kill-window", "-t", "mc-main:eng-1"], commands)
            self.assertFalse(any(cmd[:2] == ["tmux", "kill-session"] for cmd in commands))
            roster = json.loads(Path(mp.ROSTER_PATH).read_text())
            self.assertTrue(roster[aid]["retired"])
            self.assertEqual(stopped, roster[aid]["recorder_stop"])

    def test_natural_window_loss_reaps_before_retirement(self):
        with tempfile.TemporaryDirectory() as td:
            queue = load_runtime("test_recorder_queue", "queue-client.py", td)
            aid = "test-node/main:eng-1"
            metadata = dict(queue.C.recorder_identity("main", "eng-1", "test-node"), pid=4242)
            Path(queue.ROSTER_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(queue.ROSTER_PATH).write_text(json.dumps({aid: {
                "agent_id": aid, "session": "main", "tab": "eng-1",
                "recorder": metadata, "retired": False,
            }}))
            stopped = {"validated": [4242], "survivors": []}
            with mock.patch.object(queue, "window_alive", return_value=False), \
                 mock.patch.object(queue, "auth_health", return_value={"state": "live"}), \
                 mock.patch.object(queue.C, "stop_recorder", return_value=stopped) as stop:
                self.assertEqual([], queue.live_agents())
            stop.assert_called_once_with("main", "eng-1", metadata, "test-node")
            roster = json.loads(Path(queue.ROSTER_PATH).read_text())
            self.assertTrue(roster[aid]["retired"])
            self.assertEqual("died-no-window", roster[aid]["retire_reason"])
            self.assertEqual(stopped, roster[aid]["recorder_stop"])

    def test_socket_generation_change_reaps_old_pid_and_backfills_current(self):
        with tempfile.TemporaryDirectory() as td:
            queue = load_runtime("test_recorder_generation", "queue-client.py", td)
            aid = "test-node/main:eng-1"
            old = dict(queue.C.recorder_identity("main", "eng-1", "test-node"), pid=4242,
                       server_pid=10)
            current = dict(queue.C.recorder_identity("main", "eng-1", "test-node"), pid=5000,
                           server_pid=20)
            Path(queue.ROSTER_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(queue.ROSTER_PATH).write_text(json.dumps({aid: {
                "agent_id": aid, "session": "main", "tab": "eng-1",
                "recorder": old, "retired": False,
            }}))
            with mock.patch.object(queue, "window_alive", return_value=True), \
                 mock.patch.object(queue, "auth_health", return_value={"state": "live"}), \
                 mock.patch.object(queue.C, "current_recorder", return_value=current), \
                 mock.patch.object(queue.C, "stop_recorder", return_value={}) as stop:
                agents = queue.live_agents()
            self.assertEqual([aid], [agent["agent_id"] for agent in agents])
            stop.assert_called_once_with(
                "main", "eng-1", old, "test-node", include_current=False)
            roster = json.loads(Path(queue.ROSTER_PATH).read_text())
            self.assertEqual(current, roster[aid]["recorder"])


if __name__ == "__main__":
    unittest.main()
