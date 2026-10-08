"""The UI patches that only ever lived on the live install, pinned so a release can't drop them.

Three fixes were applied straight to `~/.local/share/mypeople/bin/` and never committed:
the mobile keyboard-tracking modal (3a95ca396f), the phone type scale (302dae6fb9), and the
fixed graph tile size (21e07e4a13). Every one of them worked, and every one of them was
silently reverted the moment the install was upgraded to 5.0.6 — the shipped runtime simply
never had them. Nothing failed loudly; the CEO just picked up a phone and found the old bugs.

These assertions are cheap because the real requirement is cheap: the patches have to be in
the files we ship, not only in the files we run.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


class MobileModalTest(unittest.TestCase):
    """The task modal has to follow the soft keyboard instead of hiding behind it."""

    def setUp(self):
        self.todos = (BIN / "todos.html").read_text()

    def test_the_board_tracks_the_visual_viewport(self):
        # Without this listener --vvh is never written and the modal keeps its desktop height.
        # (The task modal is the board's alone: the graph shows terminals, not tasks.)
        self.assertIn("window.visualViewport", self.todos)
        self.assertIn('setProperty("--vvh"', self.todos)
        self.assertIn('setProperty("--vvtop"', self.todos)

    def test_the_board_sizes_the_mobile_modal_from_vvh(self):
        self.assertRegex(self.todos, r"@media\s*\(max-width:640px\)")
        self.assertIn("height:var(--vvh,100dvh)", self.todos)

    def test_todos_thread_can_shrink_under_the_keyboard(self):
        # .thread{min-height:360px} outside the media query blocks the shrink and shoves the
        # composer off-screen, which is the exact bug 3a95ca396f fixed.
        self.assertIn("min-height:0", self.todos)

    def test_mobile_inputs_stay_at_or_above_the_ios_zoom_floor(self):
        # iOS zooms the page on focus for anything under 16px, which strands the composer.
        rule = re.search(r"#composer\{([^}]*)\}", self.todos)
        self.assertIsNotNone(rule, "#composer rule is gone")
        composer_size = re.search(r"font-size:(\d+)px", rule.group(1))
        self.assertIsNotNone(composer_size, "#composer has no font-size to check")
        self.assertGreaterEqual(int(composer_size.group(1)), 16)

    def test_phone_type_scale_is_smaller_than_desktop(self):
        # 302dae6fb9: a long thread is unreadable at desktop sizes on a 390px screen.
        mobile = self._mobile_block(self.todos)
        for selector, size in ((".task-text", 15), (".m-title", 19), (".ev-text", 15)):
            with self.subTest(selector=selector):
                self.assertRegex(mobile, re.escape(selector) + r"\{font-size:%dpx" % size)

    def _mobile_block(self, html):
        start = re.search(r"@media\s*\(max-width:640px\)\{", html)
        self.assertIsNotNone(start, "mobile media query is gone")
        depth, i = 1, start.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(html[i], 0)
            i += 1
        return html[start.end():i]


class GraphTileFrameTest(unittest.TestCase):
    """Tile size must not be derived from the cols it changes."""

    def test_frame_is_fixed_not_derived_from_polled_cols(self):
        html = (BIN / "terminal-graph.html").read_text()
        self.assertIn("n.frameW=1000;n.frameH=600;", html)
        # The shrink loop: frame -> ttyd cols -> tmux resize -> smaller frame, down to 60x15.
        self.assertNotIn("n.frameW=Math.max(480,n.cols*6.25)", html)


if __name__ == "__main__":
    unittest.main()
