"""Relative times ("14m ago") keep up with the clock while a thread stays open.

They were computed once, when a comment was drawn, and the board only redraws when the data
changes, so a thread left open showed "14m ago" indefinitely until a new message or a reopen.
This runs the SHIPPED relTime()/refreshRelTimes() from todos.html in node with a fake clock.
"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest

HTML = (Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin" / "todos.html").read_text()


def fn(name):
    # relTime spans several lines; refreshRelTimes is one. Take from `function name(` to the
    # first line that closes it at column 0, or the end of a one-liner.
    start = HTML.index("function %s(" % name)
    line_end = HTML.index("\n", start)
    one = HTML[start:line_end]
    if one.count("{") and one.count("{") == one.count("}"):
        return one + "\n"
    return HTML[start:HTML.index("\n}\n", start) + 3]


HARNESS = r"""
let NOW = 0; Date.now = () => NOW;
const els = [];
global.document = {querySelectorAll: sel => sel === ".ev-ago[data-ts]" ? els : []};
%s
%s
const MIN = 60 * 1000, posted = 1_000_000_000;       // a comment posted at t=posted (seconds)
const label = {dataset: {ts: String(posted)}, textContent: ""};
els.push(label);
const seen = [];
for (const minutes of [0, 14, 24, 90, 60 * 30]) {
  NOW = posted * 1000 + minutes * MIN;
  refreshRelTimes();
  seen.push(label.textContent);
}
console.log(JSON.stringify(seen));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class RelativeTimesTickTests(unittest.TestCase):
    def test_labels_advance_with_the_clock(self):
        script = HARNESS % (fn("relTime"), fn("refreshRelTimes"))
        res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout),
                         ["just now", "14m ago", "24m ago", "2h ago", "1d ago"])

    def test_the_refresh_is_actually_scheduled(self):
        # The function alone does nothing if no timer calls it.
        self.assertRegex(HTML, r"setInterval\(refreshRelTimes,\s*\d+\)")

    def test_every_label_carries_its_timestamp(self):
        # refreshRelTimes can only update labels that remember when they were posted.
        self.assertIn('ago.className="ev-ago"; ago.dataset.ts=it.ts;', HTML)


if __name__ == "__main__":
    unittest.main()
