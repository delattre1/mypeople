"""The Plugins tab the owner picked, 11-05: connect first, tabs with counts, names beside one plugin, what it tells the agent folded.

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

HTML = (Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin" / "plugins.html").read_text()
SCRIPT = HTML[HTML.index("<script>") + 8:HTML.rindex("</script>")]
IDS = re.findall(r'id="([\w-]+)"', HTML[HTML.index("<body"):HTML.index("<script>")])
ROWS = [{"name": "plow-chat", "state": "running", "desc": "Texts", "installed": False, "also": ["lab"], "rule": "", "asking": False},
        {"name": "a2a", "state": "down", "desc": "Agents", "installed": False, "also": [], "rule": "Be brief.", "asking": False},
        {"name": "github-prs", "state": "off", "desc": "PRs", "installed": False, "also": [], "rule": "", "asking": False},
        {"name": "web-chat", "state": "not set up", "desc": "Web", "installed": False, "also": [], "rule": "", "asking": False}]
SAID = {"plow-chat": "[plowchat] <their message>", "a2a": "[from <agent>] <their message>", "web-chat": "[web-chat] <their message>"}
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
 const det=()=>ids.view.querySelectorAll("details")[0];R.foldedByDefault=!det().open;R.said=ids.view.querySelectorAll("pre")[0].textContent;R.also=ids.view.textContent.includes("Also running, not switched here: lab");
 const t=ids.view.querySelectorAll("textarea")[0];t.value="My text.";t.oninput();by("BUTTON","Save").onclick();await tick();
 const i=ids.view.querySelectorAll("input")[0];i.value="Be shorter";i.oninput();by("BUTTON","Ask").onclick();await tick();
 ids.view.querySelectorAll("button").find(b=>b.attrs.role==="switch").onclick();await tick();R.afterSwitch=on();R.tabsAfter=tabsNow();
 by("BUTTON","Running").onclick();await tick();ids.view.querySelectorAll("button").find(b=>b.className==="nm"&&b.children[0].textContent==="a2a").onclick();await tick();
 R.down=ids.view.textContent.includes("down");R.accessLink=by("A","Access")!==undefined;
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

    def test_names_beside_one_plugin_and_its_text_folded(self):
        self.assertEqual(self.r["names"], ["plow-chat", "a2a"])
        self.assertTrue(self.r["foldedByDefault"])
        self.assertEqual(self.r["said"], SAID["plow-chat"])
        self.assertTrue(self.r["also"])

    def test_a_plugin_that_should_run_and_does_not_says_down(self):
        self.assertTrue(self.r["down"])
        self.assertTrue(self.r["accessLink"])

    def test_every_action_reaches_its_real_route(self):
        posts = dict((u, b) for u, b in self.r["posts"] if not u.endswith("/connect"))
        self.assertEqual(posts["/todo/plugins/wrap"], {"name": "plow-chat", "text": "My text."})
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
