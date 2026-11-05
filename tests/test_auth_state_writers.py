"""status/auth.json is what the login banner on every page reads, so it must not say "no login" when that is false.

Seen on the owner's board, 11-05: an agent ran `mypeople auth-check` from a scratch copy with HOME set to a temp folder. Every agent's
shell carries MYPEOPLE_HOME, so that wrote "not logged in" into the real install, and since the Boss supervisor's login gate latches
open, nothing rechecked it until a later hand-run auth-check.
"""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR = (ROOT / "mypeople" / "runtime" / "bin" / "boss-supervisor.sh").read_text()
GATE = re.search(r"^authenticated\(\)\{.*?^\}", SUPERVISOR, re.S | re.M).group(0)


class AnotherHome(unittest.TestCase):
    def check(self, home, install):
        status = Path(install) / "status" / "auth.json"
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps({"authenticated": True, "backend": "claude", "requested": "claude"}))
        r = subprocess.run([sys.executable, "-c", "import sys; from mypeople.cli import main; sys.argv=['mypeople','auth-check','--quiet']; sys.exit(main())"],
                           env={"PATH": "/usr/bin:/bin", "HOME": str(home), "MYPEOPLE_HOME": str(install), "PYTHONPATH": str(ROOT)},
                           cwd=str(home), capture_output=True, text=True, timeout=60)
        self.assertIn(r.returncode, (0, 1), r.stderr[-300:])
        return r.returncode, json.loads(status.read_text())["authenticated"]

    def test_a_check_from_a_temp_home_answers_and_leaves_the_real_install_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, install = Path(tmp, "home"), Path(tmp, "real-install")
            home.mkdir()
            rc, said = self.check(home, install)
            self.assertEqual(1, rc, "it still answers for that HOME: no login there")
            self.assertTrue(said, "the real install's status must not change")

    def test_an_install_under_home_still_publishes_a_real_no_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, said = self.check(Path(tmp), Path(tmp, ".local", "share", "mypeople"))
            self.assertEqual(1, rc)
            self.assertFalse(said)


@unittest.skipUnless(os.path.exists("/bin/bash"), "needs bash")
class GateRechecksWhileItSaysNoLogin(unittest.TestCase):
    def run_gate(self, status):
        with tempfile.TemporaryDirectory() as tmp:
            install, bin_dir, log = Path(tmp, "i"), Path(tmp, "b"), Path(tmp, "called")
            (install / "status").mkdir(parents=True)
            bin_dir.mkdir()
            if status is not None:
                (install / "status" / "auth.json").write_text(status)
            fake = bin_dir / "mypeople"
            fake.write_text('#!/bin/sh\necho "$@" >> %s\nexit 1\n' % log)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            r = subprocess.run(["/bin/bash", "-c", "%s\nLOGIN_GATE=open\nauthenticated; echo rc=$? gate=$LOGIN_GATE" % GATE],
                               env={"PATH": "%s:/usr/bin:/bin" % bin_dir, "INSTALL_DIR": str(install), "HOME": tmp},
                               capture_output=True, text=True, timeout=20)
            return r.stdout.strip(), log.read_text().strip() if log.exists() else ""

    def test_a_false_status_is_rechecked_and_the_gate_stays_open_whatever_it_answers(self):
        out, called = self.run_gate('{"authenticated": false, "backend": ""}')
        self.assertEqual("auth-check --quiet", called)
        self.assertEqual("rc=0 gate=open", out)

    def test_a_healthy_node_pays_nothing(self):
        self.assertEqual("", self.run_gate('{"authenticated": true}')[1])
        self.assertEqual("", self.run_gate(None)[1])


if __name__ == "__main__":
    unittest.main()
