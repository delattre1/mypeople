"""The 5.22 board: brand faces actually load, and a card's "[ Category ]" prefix is display only.

Runs the SHIPPED splitTitle() from todos.html in node. The stored title must survive untouched:
the row shows "growth" as a handle and the rest as the title, but editing edits the raw text.
"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest

BIN = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"
HTML = (BIN / "todos.html").read_text()


def fn(name):
    start = HTML.index("function %s(" % name)
    return HTML[start:HTML.index("\n}\n", start) + 3]


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SplitTitleTests(unittest.TestCase):
    CASES = {
        "bot+cat": "🤖 [ Growth ] Find apps spending on ads",
        "cat": "[ Plow Latch ] Full redesign",
        "whole": "[VIDA FINANCEIRA - Pluggy]",
        "plain": "Hire consultant engineers",
        "multi": "[ IG ] Carousel\nsecond line",
    }

    @classmethod
    def setUpClass(cls):
        script = fn("splitTitle") + "console.log(JSON.stringify(Object.fromEntries(Object.entries(%s).map(([k,v])=>[k,splitTitle(v)]))))" % json.dumps(cls.CASES)
        res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        cls.out = json.loads(res.stdout)

    def test_category_and_robot_become_marks(self):
        self.assertEqual(self.out["bot+cat"], {"bot": True, "cat": "Growth", "title": "Find apps spending on ads"})
        self.assertEqual(self.out["cat"], {"bot": False, "cat": "Plow Latch", "title": "Full redesign"})

    def test_a_title_that_is_only_brackets_stays_a_title(self):
        self.assertEqual(self.out["whole"], {"bot": False, "cat": "", "title": "[VIDA FINANCEIRA - Pluggy]"})

    def test_plain_and_multiline_titles(self):
        self.assertEqual(self.out["plain"]["title"], "Hire consultant engineers")
        self.assertEqual(self.out["multi"]["title"], "Carousel\nsecond line")

    def test_editing_starts_from_the_stored_title(self):
        # the editor must never save the display split back: it loads task.text, prefix and all
        self.assertIn('el.textContent=task.text||"";   // edit the stored title', HTML)


class BrandFontsTests(unittest.TestCase):
    def test_every_face_the_stylesheet_names_ships_with_its_license(self):
        css = (BIN / "fonts" / "fonts.css").read_text()
        files = re.findall(r"url\(/fonts/([^)]+)\)", css)
        self.assertEqual(len(files), 10)
        for f in files:
            self.assertTrue((BIN / "fonts" / f).is_file(), f)
        for fam in ("dmsans", "dmmono", "instrumentserif"):
            self.assertTrue((BIN / "fonts" / ("OFL-%s.txt" % fam)).is_file())

    def test_the_board_server_serves_them(self):
        src = (BIN / "todo-server.py").read_text()
        self.assertIn('if p.startswith("/fonts/"):', src)
        self.assertIn("name = os.path.basename(p)", src)   # never a path outside bin/fonts

    def test_every_page_links_the_stylesheet_once(self):
        import sys
        sys.path.insert(0, str(BIN))
        import mpcommon as C
        page = C.render_page(HTML)
        self.assertEqual(page.count('href="/fonts/fonts.css"'), 1)
        self.assertLess(page.index('href="/fonts/fonts.css"'), page.index("</head>"))
        self.assertEqual(C.render_page(page).count('href="/fonts/fonts.css"'), 1)


if __name__ == "__main__":
    unittest.main()
