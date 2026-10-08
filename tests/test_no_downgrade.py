"""An older build must never overwrite a newer install.

materialize() copies whatever the caller carries over INSTALL_DIR, and the desktop app carries its
own runtime. A 5.8.0 .app left in a build folder -- reopened by macOS after a reboot -- rolled the
CEO's 5.13.1 install back to 5.8.0 twice, taking Cmd+F search, live timestamps, the Grok default,
the PR watcher and owner-direct message delivery with it. The second time, his messages stopped
reaching the agent that owned his card.
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mypeople import cli, firstrun


class RefuseDowngradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = self.tmp.name
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                           if k != "MYPEOPLE_ALLOW_DOWNGRADE"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def installed(self, version):
        Path(self.install, "VERSION").write_text(version + "\n")

    def running(self, version):
        """Pretend this build is `version`: firstrun imports __version__ from the package."""
        return mock.patch("mypeople.__version__", version)

    def test_an_older_build_yields_to_a_newer_install(self):
        self.installed("5.13.1")
        with self.running("5.8.0"):
            self.assertTrue(firstrun.refuse_downgrade(self.install))

    def test_the_same_version_proceeds(self):
        self.installed("5.13.1")
        with self.running("5.13.1"):
            self.assertFalse(firstrun.refuse_downgrade(self.install))

    def test_a_newer_build_upgrades_as_before(self):
        self.installed("5.8.0")
        with self.running("5.13.1"):
            self.assertFalse(firstrun.refuse_downgrade(self.install))

    def test_a_fresh_machine_with_no_install_proceeds(self):
        with self.running("5.8.0"):
            self.assertFalse(firstrun.refuse_downgrade(self.install))

    def test_an_unreadable_version_proceeds_rather_than_blocking_an_install(self):
        self.installed("not-a-version")
        with self.running("5.8.0"):
            self.assertFalse(firstrun.refuse_downgrade(self.install))

    def test_it_compares_numerically_not_as_text(self):
        self.installed("5.9.0")                      # "5.9.0" > "5.13.1" as strings
        with self.running("5.13.1"):
            self.assertFalse(firstrun.refuse_downgrade(self.install),
                             "5.13.1 is newer than 5.9.0 and must be allowed to upgrade")

    def test_a_deliberate_rollback_still_has_a_way_through(self):
        self.installed("5.13.1")
        with self.running("5.8.0"), mock.patch.dict(os.environ, {"MYPEOPLE_ALLOW_DOWNGRADE": "1"}):
            self.assertFalse(firstrun.refuse_downgrade(self.install))

    def test_materialize_copies_nothing_when_it_would_downgrade(self):
        self.installed("5.13.1")
        marker = Path(self.install, "bin")
        with self.running("5.8.0"), mock.patch.object(firstrun, "_echo", lambda m: None):
            firstrun.materialize(self.install)
        self.assertFalse(marker.exists(), "a refused materialize must not write anything")

    def test_an_old_app_does_not_cycle_a_newer_installs_daemons(self):
        self.installed("5.13.1")
        with self.running("5.8.0"), \
             mock.patch.object(cli, "_daemons_running", lambda i: True), \
             mock.patch.object(cli, "cmd_down") as down:
            self.assertFalse(cli.restart_if_serving_stale({}, self.install))
        down.assert_not_called()

    def test_serving_version_names_what_runs_not_the_app_that_ran_up(self):
        """An older app that left a newer install alone stamped its OWN version into
        run/serving.version: a label for code nobody was serving (card 8f490e73e5)."""
        self.installed("5.13.1")
        with self.running("5.8.0"), mock.patch.object(cli, "__version__", "5.8.0"):
            cli._stamp_serving_version(self.install)
        self.assertEqual("5.13.1", Path(self.install, "run", "serving.version").read_text().strip())


class ChildEnvTests(unittest.TestCase):
    def test_daemons_carry_no_agent_identity(self):
        """card 8f490e73e5: the app, launched from eng-961's shell, handed every daemon
        AGENT_ID=eng-961, so each `mp send` a daemon made claimed to come from that agent."""
        agent = {"AGENT_ID": "n/main:eng-961", "BOSS_ID": "n/main:Boss", "TMUX": "/tmp/t,1,0",
                 "TMUX_PANE": "%1", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s",
                 "MYPEOPLE_BACKEND": "claude", "MYPEOPLE_ROLE": "eng", "HOME": "/home/x"}
        with mock.patch.dict(os.environ, agent, clear=True):
            env = cli.child_env({"QUEUE_URL": "http://q"})
        self.assertEqual(set(), set(agent) - {"HOME"} & set(env))
        self.assertEqual(("/home/x", "http://q"), (env["HOME"], env["QUEUE_URL"]))

    def test_a_cloud_login_reaches_the_fleet(self):
        """A cloud MyPlow's Claude login lives only in CLAUDE_CODE_OAUTH_TOKEN: stripping it with
        the identity keys logged every cloud Boss out ("Not logged in")."""
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-x",
                                          "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                                          "CLAUDE_CODE_SESSION_ID": "s"}, clear=True):
            env = cli.child_env({})
        self.assertEqual("sk-ant-x", env.get("CLAUDE_CODE_OAUTH_TOKEN"))
        self.assertEqual("1", env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"))
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)


if __name__ == "__main__":
    unittest.main()
