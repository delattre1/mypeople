"""The board and the HUD fill the window: no page column is capped at a fixed pixel width (CEO, 10-05).

A capped column left most of a wide window empty. Reads the shipped stylesheets: a px max-width on a
page column or on the thread/composer columns brings the fixed width back.
"""
from pathlib import Path
import re
import unittest

BIN = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"
COLUMNS = {"todos.html": (".wrap", ".thread-col", ".composer-col"), "dashboard.html": (".wrap",)}


class FluidWidthTests(unittest.TestCase):
    def test_no_page_column_has_a_fixed_pixel_width(self):
        for page, selectors in COLUMNS.items():
            css = (BIN / page).read_text()
            for selector in selectors:
                rules = re.findall(r"(?<![\w-])%s\{([^}]*)\}" % re.escape(selector), css)
                self.assertTrue(rules, f"{page} has no {selector} rule")
                for rule in rules:
                    self.assertNotRegex(rule, r"max-width:\s*\d+px", f"{page} {selector} is capped: {rule}")


if __name__ == "__main__":
    unittest.main()
