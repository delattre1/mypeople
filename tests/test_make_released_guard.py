"""`make live` / `make app` accept only the tagged commit of __version__ (card 8f490e73e5).

On 10-03 a second commit reused 5.21.17 and 5.21.26-28 went live untagged by hand; a version
number that names two trees makes every later claim about what shipped unreliable.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git") and shutil.which("make"), "needs git and make")
class ReleasedGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.d = self.tmp.name
        shutil.copy(ROOT / "Makefile", d)
        Path(d, "mypeople").mkdir()
        Path(d, "mypeople", "__init__.py").write_text('__version__ = "9.9.9"\n')
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "Release 9.9.9"]):
            sh(d, *cmd)

    def released(self):
        return sh(self.d, "make", "-s", "released").returncode == 0

    def test_only_the_tagged_clean_commit_passes(self):
        self.assertFalse(self.released(), "an untagged commit must not go live")
        sh(self.d, "git", "tag", "v9.9.9")
        self.assertTrue(self.released())
        Path(self.d, "mypeople", "__init__.py").write_text('__version__ = "9.9.9"\n# edit\n')
        self.assertFalse(self.released(), "uncommitted changes would ship under the tag")

    def test_a_second_commit_reusing_the_number_is_refused(self):
        sh(self.d, "git", "tag", "v9.9.9")
        Path(self.d, "mypeople", "x.py").write_text("x = 1\n")
        sh(self.d, "git", "add", "-A")
        sh(self.d, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "Release 9.9.9 again")
        self.assertFalse(self.released(), "8064953 read 5.21.17 too; the tag names d7961d3 only")

    def test_live_runs_the_whole_suite_first(self):
        rule = next(l for l in (ROOT / "Makefile").read_text().splitlines() if l.startswith("live:"))
        self.assertIn("test", rule.split(":", 1)[1].split(), "5.22.113 shipped failing tests: no make live without make test")


FAKE_UV = """#!/bin/sh
echo "$*" >> "$UV_CALLS"
case "$1 $2" in
  "tool list") [ -n "$CLI_VERSION" ] && printf 'mypeople v%s\\n- mypeople\\n' "$CLI_VERSION" ;;
  "build -q") d=""; while [ $# -gt 0 ]; do [ "$1" = --out-dir ] && d="$2"; shift; done
              touch "$d/mypeople-9.9.9-py3-none-any.whl" ;;
esac
exit 0
"""


@unittest.skipUnless(shutil.which("git") and shutil.which("make"), "needs git and make")
class CliRefreshTests(unittest.TestCase):
    """`make live` reinstalls the `mypeople` CLI boss-supervisor runs when it is not this release:
    a 5.14.0 copy nobody refreshed kept an auth probe that started the 1Password MCP, one macOS
    App Data prompt per release (card 70a20b5dcf)."""

    setUp = ReleasedGuardTests.setUp

    def cli(self, installed):
        sh(self.d, "git", "tag", "v9.9.9")
        bin_dir = Path(self.d, "fakebin")
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "uv").write_text(FAKE_UV)
        (bin_dir / "uv").chmod(0o755)
        calls = Path(self.d, "calls")
        calls.write_text("")
        env = {"PATH": "%s:/usr/bin:/bin" % bin_dir, "UV_CALLS": str(calls), "CLI_VERSION": installed,
               "HOME": self.d}
        r = subprocess.run(["make", "-s", "cli"], cwd=self.d, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [c for c in calls.read_text().splitlines() if c.startswith("tool install")]

    def test_a_stale_cli_is_reinstalled_from_this_release(self):
        (install,) = self.cli("5.14.0")
        self.assertRegex(install, r"^tool install -q --force \S+/mypeople-9\.9\.9-py3-none-any\.whl$")

    def test_a_current_or_absent_cli_is_left_alone(self):
        self.assertEqual(self.cli("9.9.9"), [])
        self.assertEqual(self.cli(""), [], "no CLI = no login gate; a release must not add one")


if __name__ == "__main__":
    unittest.main()
