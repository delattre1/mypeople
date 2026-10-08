"""The running version is visible on every UI page.

The runtime is copied out of the package into INSTALL_DIR and served by daemons running under a
bare interpreter, so `import mypeople` is not reachable from them. These tests pin the two links
in that chain: materialize() stamps the version into the install, and the page renderer puts that
stamp on every page it serves.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mypeople  # noqa: E402
from mypeople import firstrun  # noqa: E402

BIN = ROOT / "mypeople" / "runtime" / "bin"


def load_mpcommon(install):
    """Import mpcommon the way a daemon does: standalone, off INSTALL_DIR, not as part of the
    package. Re-imported per test because CFG/version are resolved at import time."""
    env_keys = ("INSTALL_DIR", "MYPEOPLE_CONFIG_PATH", "MYPEOPLE_VERSION")
    saved = {k: os.environ.get(k) for k in env_keys}
    os.environ["INSTALL_DIR"] = install
    # never let a developer's real ~/.config/mypeople/queue.env leak into the test
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


class VersionStampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = os.path.join(self.tmp.name, "install")

    def test_materialize_stamps_the_package_version(self):
        firstrun.materialize(self.install)
        stamp = os.path.join(self.install, "VERSION")
        self.assertTrue(os.path.exists(stamp), "an install must know which version it is")
        self.assertEqual(open(stamp).read().strip(), mypeople.__version__)

    def test_upgrade_refreshes_a_stale_stamp(self):
        """The failure the pages would show: upgraded code still reporting the old number."""
        firstrun.materialize(self.install)
        with open(os.path.join(self.install, "VERSION"), "w") as f:
            f.write("0.0.1-stale\n")
        firstrun.materialize(self.install)
        self.assertEqual(open(os.path.join(self.install, "VERSION")).read().strip(),
                         mypeople.__version__)

    def test_runtime_reads_the_stamp_of_the_install_it_serves(self):
        firstrun.materialize(self.install)
        with open(os.path.join(self.install, "VERSION"), "w") as f:
            f.write("9.9.9\n")
        C = load_mpcommon(self.install)
        self.assertEqual(C.version(), "9.9.9",
                         "the runtime must report the install serving it, not a compiled-in guess")

    def test_version_never_raises_without_a_stamp(self):
        """A missing stamp must degrade to a label, not 500 the page."""
        os.makedirs(self.install, exist_ok=True)
        C = load_mpcommon(self.install)
        self.assertTrue(C.version())  # resolves to the source tree's version, or "dev"


class PageRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.install = os.path.join(self.tmp.name, "install")
        firstrun.materialize(self.install)
        with open(os.path.join(self.install, "VERSION"), "w") as f:
            f.write("1.2.3\n")
        self.C = load_mpcommon(self.install)

    def test_every_shipped_ui_page_renders_the_version(self):
        pages = ("todos.html", "dashboard.html", "terminal-graph.html")
        for name in pages:
            with self.subTest(page=name):
                src = open(os.path.join(self.install, "bin", name), encoding="utf-8").read()
                out = self.C.render_page(src)
                self.assertIn("v1.2.3", out, "%s must show the running version" % name)
                self.assertIn("mp-version-badge", out)

    def test_badge_survives_a_page_with_no_body_tag(self):
        out = self.C.render_page("<h1>mypeople</h1>")
        self.assertIn("v1.2.3", out)

    def test_no_placeholder_leaks_to_the_browser(self):
        out = self.C.render_page(open(os.path.join(self.install, "bin", "todos.html"),
                                      encoding="utf-8").read())
        for placeholder in ("__MP_VERSION__", "__TTYD_PORT__", "__HOST_ID__"):
            self.assertNotIn(placeholder, out)

    def test_badge_is_not_injected_twice(self):
        once = self.C.render_page(open(os.path.join(self.install, "bin", "todos.html"),
                                       encoding="utf-8").read())
        twice = self.C.render_page(once)
        self.assertEqual(twice.count('id="mp-version-badge"'), 1)

    def test_every_shipped_ui_page_can_reach_every_other_one(self):
        """No first-class surface may be a dead end you can only leave by typing a URL. Injected at
        the render seam, so a page added later is linked without anyone remembering to do it."""
        pages = ("todos.html", "dashboard.html", "terminal-graph.html")
        for name in pages:
            with self.subTest(page=name):
                src = open(os.path.join(self.install, "bin", name), encoding="utf-8").read()
                out = self.C.render_page(src)
                self.assertIn('id="mp-nav"', out, "%s has no nav" % name)
                for href in ('href="/"', 'href="/terminal-graph"'):
                    self.assertIn(href, out, "%s cannot reach %s" % (name, href))

    def test_nav_offers_exactly_the_two_surfaces(self):
        """Board and Graph -- and nothing else. Terminals are reached through the Graph; the HUD
        and the wall are not on the bar."""
        out = self.C.render_page("<h1>mypeople</h1>")
        nav = out.split('id="mp-nav"', 1)[1].split("</nav>", 1)[0]
        self.assertEqual(nav.count("<a "), 2, "the nav is Board / Graph only")
        self.assertNotIn("/dashboard", nav)
        self.assertNotIn("/wall", out)
        self.assertNotIn("Terminals", out)

    def test_nav_is_not_injected_twice(self):
        once = self.C.render_page(open(os.path.join(self.install, "bin", "todos.html"),
                                       encoding="utf-8").read())
        twice = self.C.render_page(once)
        self.assertEqual(twice.count('id="mp-nav"'), 1)

    def test_env_override_wins(self):
        os.environ["MYPEOPLE_VERSION"] = "0.5.0-rc1"
        self.addCleanup(os.environ.pop, "MYPEOPLE_VERSION", None)
        self.assertEqual(self.C.version(), "0.5.0-rc1")


if __name__ == "__main__":
    unittest.main()
