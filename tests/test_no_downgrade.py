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


if __name__ == "__main__":
    unittest.main()
