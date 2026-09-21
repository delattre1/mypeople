"""The terminal graph must ask tmux for window sizes ONCE, not once per agent.

The graph polls this metadata every 2s for every open page. Asking per agent made the cost
grow with the fleet for an answer tmux returns as a single list: measured on a live 27-agent
install, the per-agent loop took 149ms and one batched call took 5ms.

The regression these guard against is silent — a per-agent loop still returns correct sizes,
it just gets slower the more agents there are, which is exactly when it hurts.
"""
import os
import tempfile
import unittest
from unittest import mock

from test_board_store import load_module


class TmuxWindowSizesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.srv = load_module(self.tmp.name, "todo-server.py", "ts_graph_batch")

    def _run(self, stdout, returncode=0):
        """Patch subprocess.run and capture how many times tmux was called."""
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=returncode, stdout=stdout)

        with mock.patch.object(self.srv.subprocess, "run", fake):
            return self.srv._tmux_window_sizes(), calls

    def test_one_call_regardless_of_fleet_size(self):
        lines = "\n".join("mc-main:eng-%d 160 48" % i for i in range(50))
        sizes, calls = self._run(lines + "\n")
        self.assertEqual(len(calls), 1, "one tmux call for the whole fleet, not one per agent")
        self.assertEqual(len(sizes), 50)
        self.assertIn("list-windows", calls[0])
        self.assertIn("-a", calls[0], "-a is what makes it every session in one call")

    def test_sizes_are_keyed_by_the_tmux_target_the_nodes_use(self):
        sizes, _ = self._run("mc-main:Boss 124 39\nmc-main:eng-2 200 50\n")
        # C.tmux_target() builds exactly this key, so the graph can look up without reformatting.
        self.assertEqual(sizes["mc-main:Boss"], (124, 39))
        self.assertEqual(sizes["mc-main:eng-2"], (200, 50))

    def test_window_names_containing_spaces_still_parse(self):
        # rsplit from the right: the size is the last two fields, the name is everything before.
        sizes, _ = self._run("mc-main:my window 80 24\n")
        self.assertEqual(sizes["mc-main:my window"], (80, 24))

    def test_garbage_lines_are_skipped_not_fatal(self):
        sizes, _ = self._run("mc-main:ok 80 24\nnonsense\nmc-main:bad x y\n")
        self.assertEqual(sizes, {"mc-main:ok": (80, 24)})

    def test_failure_is_soft_so_the_fleet_still_renders(self):
        # A non-zero tmux exit, or no tmux at all, must not blank the graph: the nodes fall
        # back to the standard 160x48 the same way the per-agent version did.
        self.assertEqual(self._run("", returncode=1)[0], {})
        with mock.patch.object(self.srv.subprocess, "run", side_effect=OSError("no tmux")):
            self.assertEqual(self.srv._tmux_window_sizes(), {})

    def test_it_does_not_talk_to_the_callers_tmux_server(self):
        """Every tmux call in this runtime clears TMUX; inheriting it targets the live server."""
        seen = {}

        def fake(cmd, **kw):
            seen.update(kw.get("env") or {})
            return mock.Mock(returncode=0, stdout="")

        with mock.patch.dict(os.environ, {"TMUX": "/private/tmp/tmux-501/default,1,0"}):
            with mock.patch.object(self.srv.subprocess, "run", fake):
                self.srv._tmux_window_sizes()
        self.assertEqual(seen.get("TMUX"), "")


if __name__ == "__main__":
    unittest.main()
