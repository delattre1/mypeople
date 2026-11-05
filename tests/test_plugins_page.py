"""The Plugins tab the owner picked, 11-05: connect first, tabs with counts, names beside one plugin, the text open, in place.

Runs the SHIPPED page script in node against a small DOM and a fake board, so what it pins is what ships: every action reaches its real route.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from test_access_page import DOM

BIN = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"
HTML = (BIN / "plugins.html").read_text()
SCRIPT = (BIN / "textbox.js").read_text() + HTML[HTML.index("<script>\n") + 9:HTML.rindex("</script>")]
IDS = re.findall(r'id="([\w-]+)"', HTML[HTML.index("<body"):HTML.index("<script")])
ROWS = [{"name": "plow-chat", "state": "running", "desc": "Texts", "installed": False, "also": ["lab"], "rule": "", "custom": "", "asking": True},
        {"name": "voice", "state": "down", "desc": "Agents", "installed": False, "also": [], "rule": "", "custom": "", "asking": False},
        {"name": "github-prs", "state": "off", "desc": "PRs", "installed": False, "also": [], "rule": "", "custom": "", "asking": False},
        {"name": "web-chat", "state": "not set up", "desc": "Web", "installed": False, "also": [], "rule": "", "custom": "", "asking": False}]
PLOW = "[plowchat] from {sender} in {chat}: {their_message}\\nRun {reply_command}. {safety_line} {plow}"
SAID = {"plow-chat": {"kind": "template", "default": PLOW, "text": PLOW, "required": ["their_message", "sender", "reply_command"], "warned": ["safety_line"], "identity": ["sender"]},
        "github-prs": {"kind": "yes", "default": "[LOCKED] <their message>", "text": "[LOCKED] <their message>", "required": [], "warned": []}}
RUN = r"""
const posts=[];const FAKE=__ROWS__,SAID=__SAID__;
const fetch=async(u,o)=>{if(o&&o.method==="POST"){const b=JSON.parse(o.body);posts.push([u,b]);if(u.endsWith("/set")){const r=FAKE.find(x=>x.name===b.name);r.state=b.on?"running":"off"}return{ok:true,status:200,json:async()=>({plugins:FAKE})}}if(u.endsWith("/messages"))return{ok:true,status:200,json:async()=>({messages:SAID})};return{ok:true,status:200,json:async()=>({plugins:FAKE})}};
const navigator={};const setInterval=()=>0;
const all=()=>{const o=[];for(const i of __IDS__)ids[i]&&(o.push(ids[i]),ids[i].walk(c=>o.push(c)));return o};
const by=(tag,txt)=>all().find(n=>n.tagName===tag&&n.textContent.trim().startsWith(txt));
const tick=async()=>{for(let i=0;i<6;i++)await new Promise(r=>setImmediate(r))};const tabsNow=()=>ids.tabs.children.map(c=>c.textContent);
const on=()=>ids.tabs.children.find(c=>c.attrs["aria-current"]).textContent;
__SCRIPT__
(async()=>{const R={};await tick();
 R.tabs=tabsNow();R.on=on();R.names=ids.view.querySelectorAll("button").filter(b=>b.className==="nm").map(b=>b.children[0].textContent);
 R.plowBusy=(ids.view.querySelectorAll("div").find(d=>d.className==="ed busy")||{textContent:""}).textContent.includes("Your agent is rewriting this");R.folds=ids.view.querySelectorAll("details").length;R.also=ids.view.textContent.includes("Also running, not switched here: lab");
 const t=ids.view.querySelectorAll("textarea")[0],base=t.value;R.said=base;R.copy=by("BUTTON","Copy").textContent;R.yellow=ids.view.querySelectorAll("span").filter(n=>n.className==="v person").map(n=>n.textContent);R.grey=ids.view.querySelectorAll("span").filter(n=>n.className==="v").map(n=>n.textContent);
 t.value=base.replace("{reply_command}","");t.oninput();R.lostOff=by("BUTTON","Save").disabled;by("BUTTON","Put it back").onclick();R.lostBack=t.value.includes("{reply_command}");
 t.value=base.replace("{safety_line}","");t.oninput();R.warnOn=!by("BUTTON","Save").disabled;
 t.value=base+" Mine.";t.oninput();by("BUTTON","Save").onclick();await tick();
 const i=ids.view.querySelectorAll("input")[0];i.value="Be shorter";i.oninput();by("BUTTON","Ask").onclick();await tick();
 ids.view.querySelectorAll("button").find(b=>b.attrs.role==="switch").onclick();await tick();R.afterSwitch=on();R.tabsAfter=tabsNow();
 ids.view.querySelectorAll("button").find(b=>b.className==="nm"&&b.children[0].textContent==="github-prs").onclick();await tick();
 R.lockedFixed=ids.view.querySelectorAll("pre").find(n=>n.className==="fixed").textContent;R.lockedLegend=ids.view.textContent.includes("Yellow is");const lt=ids.view.querySelectorAll("textarea")[0];lt.value="My own text.";lt.oninput();by("BUTTON","Save").onclick();await tick();
 by("BUTTON","Running").onclick();await tick();ids.view.querySelectorAll("button").find(b=>b.className==="nm"&&b.children[0].textContent==="voice").onclick();await tick();
 R.down=ids.view.textContent.includes("down");R.names2=ids.view.querySelectorAll("button").filter(b=>b.className==="nm").map(b=>b.children[0].textContent);
 by("BUTTON","Not set up").onclick();await tick();R.noSwitch=!ids.view.querySelectorAll("button").find(b=>b.attrs.role==="switch");by("BUTTON","Ask your agent to connect it").onclick();await tick();
 ids.want.value="Connect my Slack workspace";ids.connect.onsubmit({preventDefault(){}});await tick();R.asked=ids.asked.textContent;R.wantCleared=ids.want.value;
 R.posts=posts;console.log(JSON.stringify(R));process.exit(0)})().catch(e=>{console.error(e);process.exit(1)});
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class PluginsTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        js = (DOM + RUN).replace("__IDS__", json.dumps(IDS)).replace("__ROWS__", json.dumps(ROWS)).replace("__SAID__", json.dumps(SAID)).replace("__SCRIPT__", SCRIPT)
        with tempfile.NamedTemporaryFile("w", suffix=".js") as f:
            f.write(js)
            f.flush()
            r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr[-800:]
        cls.r = json.loads(r.stdout)

    def test_connect_comes_first_then_tabs_with_counts(self):
        self.assertLess(HTML.index('id="connect"'), HTML.index('id="tabs"'))
        self.assertEqual(self.r["tabs"], ["Running 2", "Off 1", "Not set up 1"])
        self.assertEqual(self.r["on"], "Running 2")

    def test_names_beside_one_plugin_and_its_text_open_with_its_variables_marked(self):
        self.assertEqual(self.r["names"], ["plow-chat", "voice"])
        self.assertEqual(self.r["folds"], 0)
        self.assertEqual(self.r["said"], PLOW)
        self.assertEqual(self.r["copy"], "Copy")
        self.assertEqual(self.r["yellow"], ["{their_message}"])
        self.assertEqual(self.r["grey"], ["{sender}", "{chat}", "{reply_command}", "{safety_line}", "{plow}"])
        self.assertTrue(self.r["also"])

    def test_while_the_agent_rewrites_after_an_ask_the_box_says_so(self):
        self.assertTrue(self.r["plowBusy"])

    def test_a_reply_command_cannot_be_lost_and_a_safety_line_only_warns(self):
        self.assertTrue(self.r["lostOff"] and self.r["lostBack"] and self.r["warnOn"])

    def test_a2a_has_no_special_case_left_here(self):
        self.assertNotIn("a2a", SCRIPT)

    def test_a_locked_plugin_shows_its_lines_fixed_and_the_owners_text_beside_them(self):
        self.assertEqual(self.r["lockedFixed"], SAID["github-prs"]["text"])
        self.assertFalse(self.r["lockedLegend"], "no variables to explain")

    def test_a_plugin_that_should_run_and_does_not_says_down(self):
        self.assertTrue(self.r["down"])

    def test_every_action_reaches_its_real_route(self):
        posts = dict((u, b) for u, b in self.r["posts"] if not u.endswith("/connect"))
        self.assertEqual([b for u, b in self.r["posts"] if u.endswith("/wrap")],
                         [{"name": "plow-chat", "text": PLOW + " Mine."}, {"name": "github-prs", "text": "My own text."}])
        self.assertEqual(posts["/todo/plugins/wrap-ask"], {"name": "plow-chat", "text": "Be shorter"})
        self.assertEqual(posts["/todo/plugins/set"], {"name": "plow-chat", "on": False})
        connects = [b["text"] for u, b in self.r["posts"] if u.endswith("/connect")]
        self.assertEqual(connects, ["Connect the web-chat plugin that is already here", "Connect my Slack workspace"])
        self.assertIn("Boss card", self.r["asked"])
        self.assertEqual(self.r["wantCleared"], "")

    def test_the_switch_stays_and_follows_the_plugin_to_its_new_tab(self):
        self.assertEqual(self.r["afterSwitch"], "Off 2")
        self.assertEqual(self.r["tabsAfter"], ["Running 1", "Off 2", "Not set up 1"])
        self.assertTrue(self.r["noSwitch"], "a plugin that is not set up has nothing to switch")

    def test_nothing_from_a_plugin_goes_in_as_markup(self):
        self.assertNotIn("innerHTML", SCRIPT)


if __name__ == "__main__":
    unittest.main()
