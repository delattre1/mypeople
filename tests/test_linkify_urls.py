"""Bare URLs in board text render as clickable links.

The CEO pastes PR links into comments as plain text ("https://github.com/.../pull/8") and they
rendered as dead text; only [label](url) markdown became a link. This runs the SHIPPED
markdownInline() from todos.html in node against a minimal DOM stub, so it tests the real code
rather than a copy of the regex.
"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import unittest

BIN = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"
HTML = (BIN / "todos.html").read_text()

# The minimal DOM markdownInline touches: text nodes, a/strong/em/code elements, appendChild.
HARNESS = r"""
function node(tag){return {tag, children:[], appendChild(c){this.children.push(c)}}}
global.location = {href: "http://localhost:9933/"};
global.document = {
  createTextNode: t => ({tag: "#text", text: t}),
  createElement: tag => node(tag),
};
%s
%s
const flat = n => n.tag === "#text" ? {text: n.text}
  : {tag: n.tag, href: n.href, text: n.textContent, target: n.target};
const out = {};
for (const [k, v] of Object.entries(JSON.parse(process.env.LINKIFY_CASES))) {
  const p = node("p"); RENDER(p, v); out[k] = p.children.map(flat);
}
console.log(JSON.stringify(out));
"""


def fn(name, html=HTML):
    m = re.search(r"function %s\(.*?\n" % name, html)
    assert m, name + " not found"
    return m.group(0)


def run(script, cases):
    env = {**os.environ, "LINKIFY_CASES": json.dumps(cases)}
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         timeout=30, env=env)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class LinkifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {
            "bare": "see https://github.com/delattre1/mypeople/pull/8 please",
            "period": "merged: https://github.com/delattre1/mypeople/pull/8.",
            "paren": "(https://github.com/delattre1/mypeople/pull/8)",
            "wiki": "https://en.wikipedia.org/wiki/Tauri_(software)",
            "markdown": "[the PR](https://github.com/delattre1/mypeople/pull/8)",
            "code": "`https://not-a-link.example`",
            "js": "javascript:alert(1) and http://ok.example",
            "two": "a https://a.example b https://b.example",
        }
        board = HARNESS % (fn("safeMarkdownHref"), fn("markdownInline"))
        cls.out = run(board.replace("RENDER", "markdownInline"), cls.cases)

    def links(self, key, out=None):
        return [c for c in (out or self.out)[key] if c.get("tag") == "a"]

    def test_a_bare_url_becomes_a_link(self):
        (a,) = self.links("bare")
        self.assertEqual(a["href"], "https://github.com/delattre1/mypeople/pull/8")
        self.assertEqual(a["text"], a["href"])
        self.assertEqual(a["target"], "_blank")
        # the surrounding prose is still there as text
        texts = "".join(c.get("text", "") for c in self.out["bare"] if "tag" not in c)
        self.assertEqual(texts, "see  please")

    def test_sentence_punctuation_stays_outside_the_link(self):
        (a,) = self.links("period")
        self.assertEqual(a["href"], "https://github.com/delattre1/mypeople/pull/8")
        after = self.out["period"][self.out["period"].index(a) + 1]
        self.assertEqual(after, {"text": "."}, "the full stop is prose, rendered right after")

    def test_a_wrapping_parenthesis_stays_outside_the_link(self):
        (a,) = self.links("paren")
        self.assertEqual(a["href"], "https://github.com/delattre1/mypeople/pull/8")

    def test_a_parenthesis_that_belongs_to_the_url_is_kept(self):
        (a,) = self.links("wiki")
        self.assertEqual(a["href"], "https://en.wikipedia.org/wiki/Tauri_(software)")

    def test_markdown_links_still_use_their_label(self):
        (a,) = self.links("markdown")
        self.assertEqual(a["text"], "the PR")
        self.assertEqual(a["href"], "https://github.com/delattre1/mypeople/pull/8")

    def test_a_url_inside_code_is_not_linked(self):
        self.assertEqual(self.links("code"), [])
        (code,) = [c for c in self.out["code"] if c.get("tag") == "code"]
        self.assertEqual(code["text"], "https://not-a-link.example")

    def test_only_http_and_https_are_ever_linked(self):
        hrefs = [a["href"] for a in self.links("js")]
        self.assertEqual(hrefs, ["http://ok.example"])

    def test_several_urls_in_one_line(self):
        self.assertEqual([a["href"] for a in self.links("two")],
                         ["https://a.example", "https://b.example"])


if __name__ == "__main__":
    unittest.main()
