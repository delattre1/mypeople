"""Claude must come up in bypass-permissions mode on a FRESH install (card 293fc81898).

`build_launch` has always passed `--dangerously-skip-permissions`, so the flag was never the
problem. Claude Code only honours that flag after an interactive consent modal -- and the modal's
default button is "No, exit". `write_claude_config()` is what pre-accepts it (plus onboarding and
folder trust), but it used to sit BELOW the unauthenticated `sys.exit(2)` in `ensure()`. A fresh
install is unauthenticated on its first pass by definition, so the one function that suppresses
the modals was the one function the fresh-install path skipped: the first agent spawned after the
login landed typed its prompt into a dialog, ate it, and died on the next Enter.

These tests pin the ordering invariant (seed before the auth gate), the login-lands path
(`auth-check`, which boss-supervisor polls immediately before spawning the Boss), and the
belt-and-braces role-bundle overlay.
"""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mypeople import cli, firstrun  # noqa: E402
from test_login_required import with_auth  # noqa: E402
from test_roles import load_mp  # noqa: E402


class SeedRunsBeforeTheAuthGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.install = os.path.join(self.tmp.name, "install")
        os.makedirs(self.home)
        config = os.path.join(self.install, "config", "queue.env")
        for key in ("MYPEOPLE_BACKEND", "DEFAULT_BACKEND", "MYPEOPLE_CONTAINER", "MYPEOPLE_DESKTOP"):
            p = mock.patch.dict(os.environ)
            p.start()
            self.addCleanup(p.stop)
            os.environ.pop(key, None)
        p = mock.patch.dict(os.environ, {"HOME": self.home})
        p.start()
        self.addCleanup(p.stop)
        for p in [mock.patch.object(firstrun, "CONFIG_PATH", config),
                  mock.patch.object(firstrun, "CONFIG_DIR", os.path.dirname(config)),
                  mock.patch.object(firstrun, "install_dir", lambda: self.install),
                  mock.patch.object(firstrun, "install_tmux_conf", lambda i: None),
                  mock.patch.object(firstrun, "_echo", lambda m: None)]:
            p.start()
            self.addCleanup(p.stop)

    def _settings(self):
        with open(os.path.join(self.home, ".claude", "settings.json")) as f:
            return json.load(f)

    def _claude_json(self):
        with open(os.path.join(self.home, ".claude.json")) as f:
            return json.load(f)

    def test_native_refusal_still_seeds_the_dialogs_away(self):
        """The regression. Refusing to start is correct; refusing to seed is what broke users."""
        with with_auth():
            with self.assertRaises(SystemExit) as caught:
                firstrun.ensure()
        self.assertEqual(caught.exception.code, 2, "the clean refusal must not change")
        self.assertTrue(self._settings()["skipDangerousModePermissionPrompt"],
                        "without this, --dangerously-skip-permissions blocks on a modal whose "
                        "default button is 'No, exit'")
        self.assertTrue(self._claude_json()["hasCompletedOnboarding"])
        self.assertTrue(os.path.exists(os.path.join(self.home, ".codex", "hooks.json")))

    def test_the_install_dir_cwds_are_pre_trusted(self):
        """Every path an agent is spawned into, or the folder-trust dialog eats its prompt."""
        with with_auth():
            with self.assertRaises(SystemExit):
                firstrun.ensure()
        projects = self._claude_json()["projects"]
        for path in (self.install, os.path.join(self.install, "run", "boss"),
                     os.path.join(self.install, "run", "eng")):
            self.assertTrue(projects.get(path, {}).get("hasTrustDialogAccepted"), path)

    def test_container_degraded_start_seeds_too(self):
        with with_auth(), mock.patch.dict(os.environ, {"MYPEOPLE_CONTAINER": "1"}):
            _, _, backend = firstrun.ensure()
        self.assertIsNone(backend)
        self.assertTrue(self._settings()["skipDangerousModePermissionPrompt"])

    def test_auth_check_seeds_when_the_login_lands(self):
        """boss-supervisor spawns the Boss the moment this returns 0 -- seed before it does."""
        with mock.patch.object(cli.firstrun, "install_dir", lambda: self.install), \
             mock.patch.object(cli.firstrun, "write_queue_env", lambda i, b: None), \
             mock.patch.object(cli.firstrun, "write_auth_state", lambda *a, **k: None), \
             mock.patch.object(cli.firstrun, "resolve_auth",
                               lambda requested, chooser=None: (True, "claude", "claude ready")):
            self.assertEqual(cli.cmd_auth_check(["--quiet"]), 0)
        self.assertTrue(self._settings()["skipDangerousModePermissionPrompt"])

    def test_seeding_never_blocks_a_landed_login(self):
        """A read-only ~ must not turn a good login into a failed auth-check."""
        with mock.patch.object(cli.firstrun, "install_dir", lambda: self.install), \
             mock.patch.object(cli.firstrun, "write_queue_env", lambda i, b: None), \
             mock.patch.object(cli.firstrun, "write_auth_state", lambda *a, **k: None), \
             mock.patch.object(cli.firstrun, "write_claude_config",
                               mock.Mock(side_effect=OSError("read-only home"))), \
             mock.patch.object(cli.firstrun, "resolve_auth",
                               lambda requested, chooser=None: (True, "claude", "claude ready")):
            self.assertEqual(cli.cmd_auth_check(["--quiet"]), 0)


class RoleBundleOverlayTests(unittest.TestCase):
    """`--settings <bundle>` is Claude's `flagSettings` layer, which it ORs with user settings --
    so a role-mounted spawn stays in bypass mode even on a node whose ~ was never seeded."""

    def test_every_claude_spawn_carries_the_consent(self):
        """CEO mandate: EVERY spawn self-sufficient, not just the role-mounted ones.

        A Boss is forced to carry --role, but an engineer spawned without one gets no bundle and
        therefore no --settings -- on an unseeded node that spawn blocks on the modal. Walk the
        real matrix and assert each launch names a settings file that actually declares consent.
        """
        matrix = [("boss", True), ("engineer", False), (None, False)]
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            for role, is_master in matrix:
                with self.subTest(role=role, master=is_master):
                    bundle = None
                    if role:
                        bundle = mp.materialize_role(mp.resolve_role(role, "claude"),
                                                     "t/main:a", "claude")
                    launch = mp.build_launch("t/main:a", td, "" if is_master else "t/main:Boss",
                                             is_master, None, "claude", role_bundle=bundle)
                    words = shlex.split(launch)
                    self.assertIn("--dangerously-skip-permissions", words)
                    self.assertIn("--settings", words, "no consent file on this launch")
                    self.assertEqual(words.count("--settings"), 1, "claude takes one --settings")
                    with open(words[words.index("--settings") + 1]) as f:
                        self.assertTrue(json.load(f)["skipDangerousModePermissionPrompt"])

    def test_overlay_declares_the_consent(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            bundle = mp.materialize_role(mp.resolve_role("engineer", "claude"),
                                         "t/main:eng", "claude")
            launch = mp.build_launch("t/main:eng", td, "t/main:Boss", False, None, "claude",
                                     role_bundle=bundle)
            with open(bundle["settings_path"]) as f:
                overlay = json.load(f)
        self.assertTrue(overlay["skipDangerousModePermissionPrompt"])
        self.assertIn("SessionStart", overlay["hooks"], "must not displace the lifecycle hooks")
        self.assertIn("--settings", launch, "the overlay only counts if claude is handed it")


if __name__ == "__main__":
    unittest.main()
