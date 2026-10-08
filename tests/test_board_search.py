"""Cmd/Ctrl+F filters the board's tasks as you type.

Runs the SHIPPED matches()/visible() from todos.html in node, and checks the page wires the
shortcut, so a refactor that drops either shows up here.
"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest

BIN = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"
HTML = (BIN / "todos.html").read_text()


def block(name):
    """`let query` through the end of visible(): the filter exactly as shipped."""
    start = HTML.index('let query="";')
    end = HTML.index("\n}\n", HTML.index("function %s(" % name)) + 3
    return HTML[start:end]


HARNESS = r"""
let lens = "", showDone = false, showCancelled = false;
%s
const tasks = [
  {id: "a1", text: "Make Grok 4.7 the default", assignee: "host/main:eng-855", state: "working"},
  {id: "b2", text: "Fix stale time labels", assignee: "host/main:eng-12", state: "done"},
  {id: "c3", text: "PR watcher plugin", assignee: "host/main:eng-855", state: "cancelled"},
  {id: "d4", text: "Weekly report", assignee: "", state: "recurring"},
];
const out = {};
for (const q of JSON.parse(process.env.QUERIES)) {
  query = q;
  out[q] = tasks.filter(visible).map(t => t.id);
}
// Board order as given (pins first), with finished cards interleaved.
out.__order = activeFirst([
  {id: "pin-done", state: "done"}, {id: "pin-live", state: "working"},
  {id: "n1-cancelled", state: "cancelled"}, {id: "n2-live", state: "working"},
  {id: "n3-done", state: "done"}, {id: "n4-live", state: "blocked"},
]).map(t => t.id);
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class BoardSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        queries = ["", "grok", "GROK 4.7".lower(), "eng-855", "labels", "c3", "grok labels", "zzz"]
        env = {**os.environ, "QUERIES": json.dumps(queries)}
        res = subprocess.run(["node", "-e", HARNESS % block("visible")], capture_output=True,
                             text=True, timeout=30, env=env)
        assert res.returncode == 0, res.stderr
        cls.out = json.loads(res.stdout)

    def test_no_query_is_the_normal_board(self):
        # done, cancelled and recurring stay hidden exactly as before
        self.assertEqual(["a1"], self.out[""])

    def test_matches_the_title(self):
        self.assertEqual(["a1"], self.out["grok"])

    def test_every_word_must_match(self):
        self.assertEqual(["a1"], self.out["grok 4.7"])
        self.assertEqual([], self.out["grok labels"])

    def test_matches_the_owner_across_states(self):
        # a search finds finished cards too: the one you are looking for is often done
        self.assertEqual(["a1", "c3"], self.out["eng-855"])

    def test_finds_a_done_card_the_board_hides(self):
        self.assertEqual(["b2"], self.out["labels"])

    def test_matches_the_card_id(self):
        self.assertEqual(["c3"], self.out["c3"])

    def test_active_cards_always_come_first_in_board_order(self):
        # every live card above every finished one; each group keeps its own (pins-first) order
        self.assertEqual(["pin-live", "n2-live", "n4-live", "pin-done", "n1-cancelled", "n3-done"],
                         self.out["__order"])

    def test_a_search_lists_active_matches_before_finished_ones(self):
        self.assertIn("activeFirst([...pins.filter(visible), ...normal.filter(visible)])",
                      HTML)

    def test_no_match_is_empty(self):
        self.assertEqual([], self.out["zzz"])


class ShortcutWiringTests(unittest.TestCase):
    def test_board_takes_cmd_or_ctrl_f(self):
        self.assertRegex(HTML, r'\(e\.metaKey\|\|e\.ctrlKey\)\s*&&\s*!e\.altKey\s*&&\s*e\.key\.toLowerCase\(\)==="f"')
        self.assertIn('id="search"', HTML)
        self.assertIn('$("#search").addEventListener("input"', HTML)

    def test_escape_clears_the_filter(self):
        self.assertRegex(HTML, r'e\.key==="Escape"\)\{[^}]*query=""')


if __name__ == "__main__":
    unittest.main()
