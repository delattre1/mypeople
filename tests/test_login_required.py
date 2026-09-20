"""Coming up on a node that has no AI login yet.

`ensure()` used to `sys.exit(2)` whenever no backend was authenticated. That is right for an
installer, but in a container that process IS the container: `restart: unless-stopped` then
restarts a process whose only possible outcome is the same exit code, so a user who runs
`docker compose up -d` before logging in gets an endless restart loop and board/HUD ports that
answer nothing at all -- no login screen, just a product that does not come up (card f0e7101c94,
proven on substrate card 8629f582e9).

These tests pin the three halves of the fix: the container comes up degraded instead of dying,
every page says what is missing, and the Boss starts on its own once the login lands -- while a
native install keeps refusing cleanly, which was never the bug.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mypeople import firstrun  # noqa: E402

BIN = ROOT / "mypeople" / "runtime" / "bin"


def with_auth(*live):
    return mock.patch.multiple(
        firstrun,
        _claude_authenticated=mock.Mock(return_value="claude" in live),
        _codex_authenticated=mock.Mock(return_value="codex" in live),
        _grok_authenticated=mock.Mock(return_value="grok" in live),
    )


def load_mpcommon(install):
    """Import mpcommon the way a daemon does: standalone, off INSTALL_DIR."""
    env_keys = ("INSTALL_DIR", "MYPEOPLE_CONFIG_PATH", "MYPEOPLE_VERSION")
    saved = {k: os.environ.get(k) for k in env_keys}
    os.environ["INSTALL_DIR"] = install
    os.environ["MYPEOPLE_CONFIG_PATH"] = os.path.join(install, "config", "queue.env")
    os.environ.pop("MYPEOPLE_VERSION", None)
    sys.path.insert(0, str(BIN))
    sys.modules.pop("mpcommon", None)
    try:
        import mpcommon
        return mpcommon
    finally:
        sys.path.remove(str(BIN))
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class EnsureWithoutLoginTests(unittest.TestCase):
    """The crash-loop itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = os.path.join(self.tmp.name, "install")
        config = os.path.join(self.install, "config", "queue.env")
        # A MyPlow agent runs with MYPEOPLE_BACKEND/DEFAULT_BACKEND exported, and ensure()
        # honours those as an explicit request. Left in place, every test below would silently
        # be testing "claude was demanded" rather than "nothing was demanded".
        for key in ("MYPEOPLE_BACKEND", "DEFAULT_BACKEND", "MYPEOPLE_CONTAINER", "MYPEOPLE_DESKTOP"):
            p = mock.patch.dict(os.environ)
            p.start()
            self.addCleanup(p.stop)
            os.environ.pop(key, None)
        # keep the developer's real ~/.config/mypeople and ~/.claude out of this
        patches = [
            mock.patch.object(firstrun, "CONFIG_PATH", config),
            mock.patch.object(firstrun, "CONFIG_DIR", os.path.dirname(config)),
            mock.patch.object(firstrun, "install_dir", lambda: self.install),
            mock.patch.object(firstrun, "write_claude_config", lambda i: None),
            mock.patch.object(firstrun, "write_codex_config", lambda i: None),
            mock.patch.object(firstrun, "install_tmux_conf", lambda i: None),
            mock.patch.object(firstrun, "_echo", lambda m: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _state(self):
        with open(firstrun.auth_state_path(self.install)) as f:
            return json.load(f)

    def test_container_comes_up_instead_of_exiting(self):
        """The regression: PID 1 must not die, because dying is what loops forever."""
        with with_auth(), mock.patch.dict(os.environ, {"MYPEOPLE_CONTAINER": "1"}):
            install, host, backend = firstrun.ensure()
        self.assertIsNone(backend, "no login means no backend to spawn against...")
        self.assertTrue(host, "...but the node still has an identity and must come up")
        self.assertTrue(os.path.exists(firstrun.CONFIG_PATH),
                        "the daemons need QUEUE_SECRET/HOST_ID even with nobody logged in")

    def test_desktop_app_comes_up_and_points_at_its_own_terminal(self):
        """A double-clicked .app has no terminal to refuse into: exiting is a window that never
        opens. It must come up like the container does, and must not tell a Mac user to run
        `docker compose exec`."""
        with with_auth(), mock.patch.dict(os.environ, {"MYPEOPLE_DESKTOP": "1"}):
            _, host, backend = firstrun.ensure()
        self.assertIsNone(backend)
        self.assertTrue(host)
        howto = self._state()["howto"]
        self.assertTrue(any("auth login" in line for line in howto))
        self.assertFalse(any("docker" in line for line in howto), howto)

    def test_native_install_still_refuses_cleanly(self):
        """An installer has a human reading stderr and no restart policy. Unchanged on purpose."""
        with with_auth(), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MYPEOPLE_CONTAINER", None)
            with self.assertRaises(SystemExit) as caught:
                firstrun.ensure()
        self.assertEqual(caught.exception.code, 2)

    def test_explicit_flag_beats_the_container_sniff(self):
        with with_auth():
            with self.assertRaises(SystemExit):
                firstrun.ensure(allow_unauthenticated=False)

    def test_unauthenticated_state_is_published_with_instructions(self):
        with with_auth(), mock.patch.dict(os.environ, {"MYPEOPLE_CONTAINER": "1"}):
            firstrun.ensure()
        state = self._state()
        self.assertFalse(state["authenticated"])
        self.assertTrue(state["howto"], "a blocked user must be told the exact command to run")
        self.assertTrue(any("auth login" in line for line in state["howto"]))

    def test_login_clears_the_state_and_selects_the_backend(self):
        with with_auth("codex"), mock.patch.dict(os.environ, {"MYPEOPLE_CONTAINER": "1"}):
            _, _, backend = firstrun.ensure()
        self.assertEqual(backend, "codex")
        state = self._state()
        self.assertTrue(state["authenticated"])
        self.assertFalse(state["howto"], "no banner once the node can actually run agents")

    def test_a_later_login_is_picked_up_without_restarting(self):
        """up (no login) -> user logs in -> auth-check flips the node, no down/up in between."""
        from mypeople import cli
        with with_auth(), mock.patch.dict(os.environ, {"MYPEOPLE_CONTAINER": "1"}):
            firstrun.ensure()
        self.assertFalse(self._state()["authenticated"])
        with with_auth("claude"):
            rc = cli.cmd_auth_check(["--quiet"])
        self.assertEqual(rc, 0, "a completed login must unblock the node in place")
        self.assertTrue(self._state()["authenticated"])
        self.assertEqual(firstrun._read_env_val("DEFAULT_BACKEND"), "claude")

    def test_auth_check_honours_a_login_on_a_different_backend(self):
        """The user is not forced into `claude` just because it is the default config value."""
        from mypeople import cli
        with with_auth(), mock.patch.dict(os.environ, {"MYPEOPLE_CONTAINER": "1"}):
            firstrun.ensure()
        with with_auth("grok"):
            self.assertEqual(cli.cmd_auth_check(["--quiet"]), 0)
        self.assertEqual(firstrun._read_env_val("DEFAULT_BACKEND"), "grok")

    def test_auth_check_reports_still_blocked(self):
        from mypeople import cli
        with with_auth(), mock.patch.dict(os.environ, {"MYPEOPLE_CONTAINER": "1"}):
            firstrun.ensure()
        with with_auth():
            self.assertEqual(cli.cmd_auth_check(["--quiet"]), 1)


class LoginBannerTests(unittest.TestCase):
    """What the user actually sees on :9933 and :9900 while blocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = os.path.join(self.tmp.name, "install")
        firstrun.materialize(self.install)

    def _write_state(self, **kw):
        firstrun.write_auth_state(self.install, kw.pop("authenticated"), **kw)

    def test_every_shipped_page_shows_the_banner(self):
        self._write_state(authenticated=False, message="no login")
        C = load_mpcommon(self.install)
        for name in ("todos.html", "dashboard.html", "terminal-graph.html"):
            with self.subTest(page=name):
                src = open(os.path.join(self.install, "bin", name), encoding="utf-8").read()
                out = C.render_page(src)
                self.assertIn("mp-login-banner", out)
                self.assertIn("Login required", out)
                self.assertIn("auth login", out, "the banner must carry the actual command")

    def test_no_banner_once_authenticated(self):
        self._write_state(authenticated=True, backend="claude")
        C = load_mpcommon(self.install)
        out = C.render_page(open(os.path.join(self.install, "bin", "todos.html"),
                                 encoding="utf-8").read())
        self.assertNotIn("mp-login-banner", out)

    def test_no_banner_when_the_state_file_is_absent(self):
        """An install predating the file must not grow a scary banner it cannot justify."""
        C = load_mpcommon(self.install)
        out = C.render_page("<html><body><h1>hi</h1></body></html>")
        self.assertNotIn("mp-login-banner", out)

    def test_banner_is_not_injected_twice(self):
        self._write_state(authenticated=False)
        C = load_mpcommon(self.install)
        src = open(os.path.join(self.install, "bin", "todos.html"), encoding="utf-8").read()
        self.assertEqual(C.render_page(C.render_page(src)).count('id="mp-login-banner"'), 1)

    def test_banner_survives_a_page_with_no_body_tag(self):
        self._write_state(authenticated=False)
        C = load_mpcommon(self.install)
        self.assertIn("mp-login-banner", C.render_page("<h1>mypeople</h1>"))

    def test_version_badge_still_renders_alongside_it(self):
        self._write_state(authenticated=False)
        C = load_mpcommon(self.install)
        out = C.render_page(open(os.path.join(self.install, "bin", "todos.html"),
                                 encoding="utf-8").read())
        self.assertIn("mp-version-badge", out)
        self.assertNotIn("__MP_LOGIN_STEPS__", out, "no placeholder may reach the browser")


class BossSupervisorGateTests(unittest.TestCase):
    """The done-condition: after login the Boss appears with no down/up cycle.

    Runs the real boss-supervisor.sh against stub `mypeople`/`mp`/`tmux` binaries, so this pins
    behaviour rather than the text of the script.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = os.path.join(self.tmp.name, "install")
        os.makedirs(os.path.join(self.install, "logs"), exist_ok=True)
        # boss-supervisor.sh puts $HOME/.local/bin and $INSTALL_DIR/bin AHEAD of the inherited
        # PATH. A developer's real `mp` therefore shadows a stub placed anywhere else -- and that
        # real `mp` talks to the LIVE install. Stubs go in $INSTALL_DIR/bin, and HOME is
        # redirected into the sandbox, so this test cannot reach a real node.
        self.stub = os.path.join(self.install, "bin")
        os.makedirs(self.stub, exist_ok=True)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(self.home, ".local", "bin"), exist_ok=True)
        self.marker = os.path.join(self.tmp.name, "ensure-boss-called")

    def _stub(self, name, body):
        path = os.path.join(self.stub, name)
        with open(path, "w") as f:
            f.write("#!/usr/bin/env bash\n%s\n" % body)
        os.chmod(path, 0o755)

    def _run(self, authenticated, seconds=3):
        # `mypeople auth-check` is the gate; `mp ensure-boss` is what must not happen early.
        self._stub("mypeople", "exit %d" % (0 if authenticated else 1))
        self._stub("mp", 'if [ "$1" = ensure-boss ]; then touch %s; fi; exit 0' % self.marker)
        self._stub("tmux", "exit 1")          # no Boss window exists
        self._stub("hostname", "echo testnode")
        env = dict(os.environ, PATH=self.stub + os.pathsep + os.environ["PATH"],
                   HOME=self.home, INSTALL_DIR=self.install, HOST_ID="testnode",
                   MYPEOPLE_CONFIG_PATH=os.path.join(self.install, "config", "queue.env"))
        env.pop("TMUX", None)
        proc = subprocess.Popen(["bash", str(BIN / "boss-supervisor.sh")], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + seconds
            while time.time() < deadline:
                if os.path.exists(self.marker):
                    return True
                time.sleep(0.1)
            return os.path.exists(self.marker)
        finally:
            proc.kill()
            proc.wait()

    def test_no_boss_is_spawned_while_the_node_has_no_login(self):
        """Spawning against a backend that cannot answer is what makes a node look alive and lie."""
        self.assertFalse(self._run(authenticated=False),
                         "an unauthenticated node must not start a Boss")

    def test_boss_starts_as_soon_as_the_login_lands(self):
        self.assertTrue(self._run(authenticated=True),
                        "after login the Boss must come up without a container restart")


if __name__ == "__main__":
    unittest.main()
