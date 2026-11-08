"""The app's bundled runtime must carry a `mypeople` command, or a stranger's Boss starts before any login.

Proven failure this pins shut (5.22.159 on a clean Arch VM, 2026-11-05): boss-supervisor.sh gates the Boss on `mypeople auth-check`, and
says so in its own header: "`mypeople` being absent (runtime used standalone) means no gate at all". The desktop apps carried only
asciinema, tmux and ttyd in runtime/bin, so on every install the gate was open from the first second: a Boss was spawned before the
login landed, its first turn was "Not logged in", and that error turn stayed in its persisted session (the resumed Boss waited for a
new message, about five minutes in the live run). The login alone is meant to start the Boss.

Pinned here: bundle-runtime.sh installs the launcher into bin/, the launcher runs the real CLI out of the bundle's own layout
(bin/, python/bin/python3, pylib/), and the app puts runtime/bin on the PATH the supervisor inherits.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "desktop" / "scripts"


def fake_runtime(tmp):
    """The bundle's layout: bin/mypeople (the launcher), python/bin/python3, pylib/mypeople (the package)."""
    rt = Path(tmp) / "runtime"
    (rt / "bin").mkdir(parents=True)
    (rt / "python" / "bin").mkdir(parents=True)
    (rt / "pylib").mkdir()
    (rt / "pylib" / "mypeople").symlink_to(ROOT / "mypeople")
    py = rt / "python" / "bin" / "python3"
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')   # a wrapper: the test's own interpreter, whatever its prefix
    py.chmod(0o755)
    shutil.copy(SCRIPTS / "mypeople-launcher.sh", rt / "bin" / "mypeople")
    (rt / "bin" / "mypeople").chmod(0o755)
    return rt


class BundleLauncherTests(unittest.TestCase):
    def test_bundle_script_installs_the_launcher_and_checks_it_runs(self):
        text = (SCRIPTS / "bundle-runtime.sh").read_text()
        self.assertIn('"$HERE/mypeople-launcher.sh" "$OUT/bin/mypeople"', text)
        self.assertIn('"$OUT/bin/mypeople" auth-check', text, "the build must run the gate command, not just copy it")

    def test_launcher_runs_the_real_cli_from_the_bundle_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = fake_runtime(tmp)
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp}   # empty environment: no PYTHONPATH, no claude, no login
            r = subprocess.run([str(rt / "bin" / "mypeople"), "no-such-subcommand"], env=env, capture_output=True, text=True, timeout=60)
            self.assertIn("MyPlow CLI", r.stdout + r.stderr, "the launcher did not reach mypeople.cli")
            self.assertNotIn("Traceback", r.stderr)

    def test_gate_command_exits_0_or_1_never_crashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = fake_runtime(tmp)
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp}
            r = subprocess.run([str(rt / "bin" / "mypeople"), "auth-check", "--quiet"], env=env, capture_output=True, text=True, timeout=120)
            self.assertIn(r.returncode, (0, 1), r.stderr[-300:])   # 0 = logged in, 1 = not: a crash (127, 2, a traceback) would open the gate
            self.assertNotIn("Traceback", r.stderr)

    def test_the_app_puts_runtime_bin_on_the_supervisors_path(self):
        main_rs = (ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text()
        self.assertIn('rt.join("bin")', main_rs)


if __name__ == "__main__":
    unittest.main()
