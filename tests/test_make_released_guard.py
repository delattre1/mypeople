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


if __name__ == "__main__":
    unittest.main()
