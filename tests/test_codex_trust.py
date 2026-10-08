"""Codex trust override: marking an agent's own cwd trusted, and nothing else.

Codex otherwise stops at a trust prompt in a fresh directory, which a headless agent cannot
answer. The override says "trust exactly this directory, for this process only" -- so the
interesting property is not that it trusts the run roots, it is everything it REFUSES to trust.
A bug here silently grants Codex trust over a directory the operator never offered.
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
        "HOME": str(home), "MYPEOPLE_CONFIG_PATH": str(config), "MYPEOPLE_HOME": str(state),
        "INSTALL_DIR": str(state), "HOST_ID": "test-node", "QUEUE_URL": "http://127.0.0.1:1",
        "QUEUE_SECRET": "secret", "DEFAULT_BACKEND": "claude",
    }, clear=False):
        sys.modules.pop("mpcommon", None)
        sys.modules.pop("mprole", None)
        sys.path.insert(0, str(BIN))
        try:
            name = "mypeople_test_ct_%s" % id(home)
            loader = importlib.machinery.SourceFileLoader(name, str(BIN / "mp"))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(BIN))


class CodexTrustOverrideTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.mp = load_mp(self._td.name)
        self.install = Path(self.mp.INSTALL_DIR)
        for sub in ("run/eng", "run/boss"):
            (self.install / sub).mkdir(parents=True, exist_ok=True)

    def test_the_engineer_run_root_is_trusted(self):
        out = self.mp.codex_trust_override(str(self.install / "run" / "eng"))
        self.assertIn('trust_level="trusted"', out)
        self.assertIn(str((self.install / "run" / "eng").resolve()), out)

    def test_the_boss_run_root_is_trusted(self):
        self.assertIn('trust_level="trusted"',
                      self.mp.codex_trust_override(str(self.install / "run" / "boss")))

    def test_a_subdirectory_of_a_run_root_is_trusted(self):
        """Agents get their own cwd under the run root; that is still managed ground."""
        d = self.install / "run" / "eng" / "card-123"
        d.mkdir(parents=True)
        self.assertIn('trust_level="trusted"', self.mp.codex_trust_override(str(d)))

    def test_an_unmanaged_directory_is_not_trusted(self):
        self.assertEqual("", self.mp.codex_trust_override(str(self.install)))
        self.assertEqual("", self.mp.codex_trust_override("/tmp"))

    def test_the_users_home_is_never_trusted(self):
        """The blast radius that matters: agents must not get blanket trust over a real home."""
        self.assertEqual("", self.mp.codex_trust_override(os.path.expanduser("~")))

    def test_a_sibling_that_merely_shares_a_prefix_is_not_trusted(self):
        """`run/engineering` starts with `run/eng` as a STRING but is a different directory."""
        d = self.install / "run" / "engineering"
        d.mkdir(parents=True)
        self.assertEqual("", self.mp.codex_trust_override(str(d)))

    def test_traversal_out_of_a_run_root_is_not_trusted(self):
        self.assertEqual("", self.mp.codex_trust_override(
            str(self.install / "run" / "eng" / ".." / ".." / "..")))

    def test_a_symlink_pointing_outside_is_not_trusted(self):
        """Resolution happens before the check, so a link cannot smuggle a path in."""
        outside = Path(self._td.name) / "outside"
        outside.mkdir()
        link = self.install / "run" / "eng" / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertEqual("", self.mp.codex_trust_override(str(link)))

    def test_the_path_is_json_quoted_so_a_quirky_path_cannot_break_the_config(self):
        d = self.install / "run" / "eng" / "we're here"
        d.mkdir(parents=True)
        out = self.mp.codex_trust_override(str(d))
        self.assertIn('trust_level="trusted"', out)
        self.assertIn('"', out)

    def test_the_codex_launch_carries_the_override_for_a_managed_cwd(self):
        cwd = str(self.install / "run" / "eng")
        launch = self.mp.build_launch("test-node/main:x", cwd, "test-node/main:Boss", False,
                                      None, "codex")
        self.assertIn("--config", launch)
        self.assertIn("trust_level", launch)

    def test_the_codex_launch_carries_no_override_for_an_unmanaged_cwd(self):
        launch = self.mp.build_launch("test-node/main:x", "/tmp", "test-node/main:Boss", False,
                                      None, "codex")
        self.assertNotIn("trust_level", launch)

    def test_only_codex_gets_the_override(self):
        """claude/grok have their own trust mechanisms; this flag is codex-only."""
        cwd = str(self.install / "run" / "eng")
        for backend in ("claude", "grok"):
            launch = self.mp.build_launch("test-node/main:x", cwd, "test-node/main:Boss", False,
                                          None, backend)
            self.assertNotIn("trust_level", launch, backend)


if __name__ == "__main__":
    unittest.main()
