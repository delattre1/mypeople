"""A cancelled card reads as cancelled on the board.

The CEO (2026-10-05): cancelled tasks were "not very distinctive, not clear that they were
canceled". The row was a faint grey ring like a working one, grey text, and nothing else. Now it
says so three ways: a grey disc with an x (the twin of done's green disc with a check), a struck
title, and the word.
"""
from pathlib import Path
import re
import unittest

BOARD = (Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin" / "todos.html").read_text()


class CancelledRow(unittest.TestCase):
    def test_the_ring_is_a_disc_with_an_x(self):
        line = next(l for l in BOARD.splitlines() if l.strip().startswith('if(s==="cancelled")'))
        self.assertIn('class="si si-cancelled"', line)
        self.assertIn('d="M7.4 7.4l5.2 5.2M12.6 7.4l-5.2 5.2"', line)
        self.assertNotIn("ring(", line)   # not the open ring a working card wears

    def test_the_title_is_struck_and_the_row_says_cancelled(self):
        self.assertRegex(BOARD, r"li\.task\.cancelled \.task-text\{text-decoration:line-through")
        self.assertIn('badge.className="badge st-cancelled"; badge.textContent="Cancelled"', BOARD)


if __name__ == "__main__":
    unittest.main()
