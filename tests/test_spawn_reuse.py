"""Spawning into a window that already exists.

A standing tmux window is not proof an agent is in it. do_spawn used to treat it as proof: it
printed "already exists, reusing", wrote the roster row and returned 0 without ever running
`launch` -- the only place the permission-bypass flag lives. That is how an installer ended up with
a Boss that stopped on the first tool prompt, and why the Spawn buttons looked dead while Revive
worked (Revive is only reachable once the window is gone, so it takes the branch that launches).

These tests pin the three outcomes of the liveness check, because the cost is asymmetric in both
directions: a false "it's fine" reinstates the silent no-op, and a false "it's broken" kills a
healthy agent mid-turn.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


def load_mp(home):
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
            name = "mypeople_test_spawnmp_%s" % id(home)
            loader = importlib.machinery.SourceFileLoader(name, str(BIN / "mp"))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(BIN))


class Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class WindowLivenessTest(unittest.TestCase):
    """window_runs_launched_agent() decides whether a pane may be destroyed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mp = load_mp(self.tmp.name)

    def _with_ps(self, pane_rc, ps_rows):
        """Fake `tmux list-panes` + `ps -eo pid=,ppid=,command=`."""
        def fake_run(argv, *a, **k):
            if argv[0] == "tmux" and argv[1] == "list-panes":
                return Proc(returncode=pane_rc, stdout="100\n")
            if argv[0] == "ps":
                return Proc(stdout=ps_rows)
            return Proc()
        return mock.patch.object(self.mp.subprocess, "run", side_effect=fake_run)

    def test_launched_agent_is_recognised(self):
        rows = "100 1 env AGENT_ID=x claude --dangerously-skip-permissions --model opus\n"
        with self._with_ps(0, rows):
            self.assertIs(self.mp.window_runs_launched_agent("main", "Boss", "claude"), True)

    def test_bare_backend_without_bypass_flag_is_not_launched(self):
        """The reported failure: an interactive `claude` occupying the Boss window."""
        rows = "100 1 claude\n"
        with self._with_ps(0, rows):
            self.assertIs(self.mp.window_runs_launched_agent("main", "Boss", "claude"), False)

    def test_agent_found_in_a_child_process_counts(self):
        """tmux may run the launch through a shell, so the agent is a descendant, not the pane pid."""
        rows = ("100 1 -sh\n"
                "204 100 env AGENT_ID=x claude --dangerously-skip-permissions\n")
        with self._with_ps(0, rows):
            self.assertIs(self.mp.window_runs_launched_agent("main", "Boss", "claude"), True)

    def test_leftover_shell_is_not_an_agent(self):
        with self._with_ps(0, "100 1 -zsh\n"):
            self.assertIs(self.mp.window_runs_launched_agent("main", "Boss", "claude"), False)

    def test_undeterminable_pane_is_unknown_not_broken(self):
        """tmux unreachable must never read as 'broken' -- that would authorise killing a pane."""
        with self._with_ps(1, ""):
            self.assertIsNone(self.mp.window_runs_launched_agent("main", "Boss", "claude"))

    def test_each_backend_requires_its_own_bypass_flag(self):
        cases = {
            "codex": "100 1 codex --dangerously-bypass-approvals-and-sandbox\n",
            "grok": "100 1 grok --permission-mode bypassPermissions\n",
        }
        for backend, rows in cases.items():
            with self.subTest(backend=backend):
                with self._with_ps(0, rows):
                    self.assertIs(
                        self.mp.window_runs_launched_agent("main", "Boss", backend), True)
        # a backend launched without its bypass flag is not acceptable either
        with self._with_ps(0, "100 1 codex\n"):
            self.assertIs(self.mp.window_runs_launched_agent("main", "Boss", "codex"), False)

    def test_other_backend_does_not_satisfy_the_check(self):
        """A codex agent sitting in a window we were asked to fill with claude is not a match."""
        rows = "100 1 codex --dangerously-bypass-approvals-and-sandbox\n"
        with self._with_ps(0, rows):
            self.assertIs(self.mp.window_runs_launched_agent("main", "Boss", "claude"), False)


class EnsureBossLivenessTest(unittest.TestCase):
    """do_ensure_boss must not accept a window that holds no launched agent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mp = load_mp(self.tmp.name)

    def test_broken_boss_window_is_not_reported_alive(self):
        """The first-run bug: window present, no launched agent, ensure-boss declared victory."""
        with mock.patch.object(self.mp, "window_exists", return_value=True), \
             mock.patch.object(self.mp, "window_runs_launched_agent", return_value=False), \
             mock.patch.object(self.mp, "load_roster", return_value={}), \
             mock.patch.object(self.mp, "do_spawn", return_value=0) as spawn:
            rc = self.mp.do_ensure_boss(["test-node/main:Boss", "--backend", "claude"])
        self.assertEqual(rc, 0)
        spawn.assert_called_once()
        args = spawn.call_args[0][0]
        self.assertIn("--master", args)
        self.assertIn("--role", args)

    def test_healthy_boss_is_left_alone(self):
        with mock.patch.object(self.mp, "window_exists", return_value=True), \
             mock.patch.object(self.mp, "window_runs_launched_agent", return_value=True), \
             mock.patch.object(self.mp, "do_spawn") as spawn:
            rc = self.mp.do_ensure_boss(["test-node/main:Boss", "--backend", "claude"])
        self.assertEqual(rc, 0)
        spawn.assert_not_called()

    def test_undeterminable_boss_is_left_alone(self):
        """Unknown must not trigger a respawn of a possibly-healthy Boss."""
        with mock.patch.object(self.mp, "window_exists", return_value=True), \
             mock.patch.object(self.mp, "window_runs_launched_agent", return_value=None), \
             mock.patch.object(self.mp, "do_spawn") as spawn:
            rc = self.mp.do_ensure_boss(["test-node/main:Boss", "--backend", "claude"])
        self.assertEqual(rc, 0)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
