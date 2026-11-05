"""A Boss or an agent on a backend this machine cannot run is refused before any window, with the command that fixes it, and the
Boss card says so: queued, the refusal reached only a log, and a Codex Boss chosen on a Mac with Claude only showed Down and nothing (11-05)."""
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"))
import mpcommon as C  # noqa: E402


class BackendReady(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"PATH": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def cli(self, name, code):
        path = Path(self.tmp.name, name)
        path.write_text("#!/bin/sh\nexit %d\n" % code)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def test_missing_says_how_to_install_and_log_in(self):
        why = C.backend_unready("codex")
        self.assertIn("Codex is not installed on this machine", why)
        self.assertIn("codex login", why)

    def test_installed_but_signed_out_says_to_sign_in(self):
        self.cli("codex", 1)
        self.assertEqual("Codex is installed but not signed in. In a terminal, run: codex login", C.backend_unready("codex"))

    def test_ready_is_empty(self):
        self.cli("codex", 0)
        self.cli("claude", 0)
        self.assertEqual("", C.backend_unready("codex"))
        self.assertEqual("", C.backend_unready("claude"))

    def test_the_boss_card_and_mp_spawn_ask_it_first(self):
        bin_ = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"
        server = (bin_ / "todo-server.py").read_text()
        spawn = server[server.index('elif action == "spawn":'):server.index('payload = {"backend": backend, "is_master": True')]
        self.assertIn("backend_unready(backend)", spawn)
        revive = server[server.index('elif action == "revive":'):server.index("payload = {}\n            target = BOSS_AGENT")]
        self.assertIn("backend_unready(was)", revive)
        mp = (bin_ / "mp").read_text()
        self.assertLess(mp.index("why = C.backend_unready(backend)"), mp.index('C.tmux_run("new-window"'), "refused before any window")


if __name__ == "__main__":
    unittest.main()
