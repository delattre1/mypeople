"""The Access tab: tabs with counts, names beside one person, Find and arrow keys, the owner's choice of 11-05.

Runs the SHIPPED page script in node against a small DOM and a fake board. It pins what he asked for: "Paired" and never "Verified", no
separate mark on his own machines, no permission message under You have access to, and Grant, Save, Ask and Revoke reaching the real routes.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

BIN = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "bin"
HTML = (BIN / "access.html").read_text()
BOX = (BIN / "textbox.js").read_text()
SCRIPT = BOX + HTML[HTML.index("<script>\n") + 9:HTML.rindex("</script>")]
IDS = re.findall(r'id="([\w-]+)"', HTML[HTML.index("<body"):HTML.index("<script")])

DOM = r"""
class N{constructor(t){this.tagName=t.toUpperCase();this.children=[];this.attrs={};this.dataset={};this.className="";this._text="";this.value="";this.parent=null;this.style={}}
 getContext(){return{fillRect(){},set fillStyle(v){}}}
 get textContent(){return this._text+this.children.map(c=>typeof c==="string"?c:c.textContent).join("")}
 set textContent(v){this._text=String(v);this.children=[]}
 append(...k){for(const c of k){if(c&&c.parent)c.parent=null;if(typeof c!=="string")c.parent=this;this.children.push(c)}}
 replaceChildren(...k){this.children=[];this._text="";this.append(...k)}
 setAttribute(k,v){this.attrs[k]=String(v)}getAttribute(k){return this.attrs[k]}
 contains(n){for(;n;n=n.parent)if(n===this)return true;return false}
 focus(){document.activeElement=this}
 walk(f){for(const c of this.children)if(typeof c!=="string"){f(c);c.walk(f)}}
 querySelectorAll(s){const o=[];const t=s.toUpperCase();this.walk(c=>{if(c.tagName===t)o.push(c)});return o}}
const ids={};const listeners={};
const window=globalThis;const document={createTextNode:t=>String(t),createElement:t=>new N(t),getElementById:i=>ids[i]||(ids[i]=new N("div")),activeElement:null,addEventListener:(t,f)=>{listeners[t]=f}};
for(const i of __IDS__){document.getElementById(i)}if(ids.find)ids.find.tagName="INPUT";
"""
HARNESS = DOM + r"""const {TextEncoder}=require("util"),crypto=require("crypto");const posts=[];const FAKE=__STATE__;
const fetch=async(u,o)=>{if(o&&o.method==="POST"){const b=JSON.parse(o.body),op=u.split("/").pop();posts.push([op,b]);return{ok:true,status:200,json:async()=>op==="line"?{line:"mp pair mpp-SECRET",minutes:5}:FAKE}}return{ok:true,status:200,json:async()=>FAKE}};
let copied="";const navigator={clipboard:{writeText:async t=>{copied=t}}};const setInterval=()=>0;
const all=()=>{const o=[];for(const i of __IDS__)ids[i]&&(o.push(ids[i]),ids[i].walk(c=>o.push(c)));return o};
const page=()=>all().map(n=>n._text).join(" ");
const by=(tag,txt)=>all().find(n=>n.tagName===tag&&n.textContent.trim().startsWith(txt));
const tick=()=>new Promise(r=>setImmediate(r));const tabsNow=()=>ids.tabs.children.map(c=>c.textContent);
const cur=()=>{const n=all().find(n=>n.className==="nm"&&n.attrs["aria-current"]);return n&&n.children[0].textContent};
__SCRIPT__
(async()=>{const R={};await tick();await tick();
 R.serviceSays=ids.service.textContent;R.firstTabs=tabsNow();R.firstOn=all().find(n=>n.className.startsWith("st")&&n.attrs["aria-current"]).textContent;R.hot=all().find(n=>n.textContent.startsWith("Waiting on you")&&n.className.startsWith("st")).className;R.request=page().includes("It does not prove who owns that MyPlow.");
 by("BUTTON","Has access to you").onclick();await tick();
 R.names=ids.view.querySelectorAll("button").filter(b=>b.className==="nm").map(b=>b.children[0].textContent);
 R.jordanText=ids.view.querySelectorAll("textarea")[0].value;const busyNow=()=>{const e=ids.view.querySelectorAll("div").find(d=>d.className==="ed busy");return e?e.querySelectorAll("span").filter(s=>s.className==="busytag").map(s=>s.textContent).join():""};R.jordanBusy=busyNow();
 document.activeElement=null;listeners.keydown({key:"ArrowDown",target:ids.find,preventDefault(){}});await tick();R.afterDown=cur();
 R.ownText=ids.view.querySelectorAll("textarea")[0].value;R.ownBusy=busyNow();
 listeners.keydown({key:"ArrowUp",target:ids.find,preventDefault(){}});await tick();R.afterUp=cur();
 const t=ids.view.querySelectorAll("textarea")[0],base=t.value;R.boxCopy=by("BUTTON","Copy").textContent;R.unchangedHidden=by("BUTTON","Save").parent.hidden;
 t.value=base.replace("{their_message}","");t.oninput();R.lostOff=by("BUTTON","Save").disabled;R.lostSays=ids.view.textContent.includes("{their_message} the agent gets none of what the person said, so this cannot be saved");by("BUTTON","Put it back").onclick();R.lostBack=t.value.includes("{their_message}");R.backOn=!by("BUTTON","Save").disabled;
 t.value=base.replace("{reply_command}","");t.oninput();R.replyOff=by("BUTTON","Save").disabled;
 t.value=base.replace("{sender}","");t.oninput();R.senderOff=by("BUTTON","Save").disabled;R.senderSays=ids.view.textContent.includes("cannot tell who sent this");by("BUTTON","Put it back").onclick();R.senderBack=t.value.startsWith("{sender} ");
 t.value=base.replace("[from {sender}, on another MyPlow]","Sent by {sender}:");t.oninput();R.wordsOn=!by("BUTTON","Save").disabled;
 t.value="{their_message} "+base.replace("{their_message}","");t.oninput();R.orderOff=by("BUTTON","Save").disabled;R.orderSays=ids.view.textContent.includes("could arrive first");by("BUTTON","Move it").onclick();R.orderFixed=t.value.indexOf("{sender}")<t.value.indexOf("{their_message}");R.orderOn=!by("BUTTON","Save").disabled;
 t.value=base.replace("{safety_line}","");t.oninput();R.warnOn=!by("BUTTON","Save").disabled;R.warnSays=ids.view.textContent.includes("You can still save it");by("BUTTON","Put it back").onclick();R.warnBack=t.value.includes("{safety_line}");
 t.value=base+" {nonsense}";t.oninput();R.oddSays=ids.view.textContent.includes("{nonsense} is not a variable here, so it goes out exactly as written");
 by("BUTTON","Discard").onclick();await tick();R.discarded=ids.view.querySelectorAll("textarea")[0].value===base;
 const t2=ids.view.querySelectorAll("textarea")[0];t2.value=base+" Edited.";t2.oninput();by("BUTTON","Save").onclick();await tick();await tick();
 const a=ids.view.querySelectorAll("input")[0];a.value="Let them ask";a.oninput();by("BUTTON","Ask").onclick();await tick();await tick();
 const rv=by("BUTTON","Revoke");rv.onclick();R.afterOneRevoke=posts.filter(p=>p[0]==="remove").length;R.armedText=rv.textContent;rv.onclick();await tick();await tick();
 by("BUTTON","You have access to").onclick();await tick();
 R.crossNote=ids.view.textContent.includes("They can reach you too");by("BUTTON","under Has access").onclick();await tick();R.crossOn=ids.tabs.children.find(c=>c.attrs["aria-current"]).textContent;by("BUTTON","You have access to").onclick();await tick();
 R.reachTextareas=ids.view.querySelectorAll("textarea").length;R.reachInputs=ids.view.querySelectorAll("input").length;R.reachText=ids.view.textContent;R.reachRevoke=!!ids.view.querySelectorAll("button").find(b=>b.textContent==="Revoke");
 ids.find.value="lin";ids.find.oninput();await tick();R.foundTabs=tabsNow();ids.find.value="";ids.find.oninput();await tick();
 by("BUTTON","Waiting on you").onclick();await tick();R.requestSays=ids.view.textContent;by("BUTTON","Only they reach me").onclick();await tick();await tick();by("BUTTON","Waiting on you").onclick();await tick();by("BUTTON","Grant access").onclick();await tick();await tick();R.afterGrantOn=all().find(n=>n.className.startsWith("st")&&n.attrs["aria-current"]).textContent;
 by("BUTTON","Ended").onclick();await tick();R.ended=ids.view.textContent;
 by("BUTTON","New line").onclick();await tick();R.newLine=!!ids.view.querySelectorAll("form").length;
 ids.view.querySelectorAll("form")[0].onsubmit({preventDefault(){}});await tick();await tick();await tick();
 R.lineNote=ids.view.textContent;R.lineBefore=ids.view.textContent.includes("mpp-SECRET");R.qrBefore=ids.view.querySelectorAll("canvas").length;by("BUTTON","Copy").onclick();await tick();R.copied=copied;by("BUTTON","Show").onclick();await tick();R.lineAfter=ids.view.textContent.includes("mpp-SECRET");R.qrAfter=ids.view.querySelectorAll("canvas").length;R.hideLabel=!!by("BUTTON","Hide");
 by("BUTTON","Ended").onclick();await tick();by("BUTTON","New line").onclick();await tick();R.qrAfterLeaving=ids.view.querySelectorAll("canvas").length;R.lineAfterLeaving=ids.view.textContent.includes("mpp-SECRET");
 const M=T=>{const m=qrMatrix(T);return{size:m.length,sha:crypto.createHash("sha256").update(m.map(r=>r.map(b=>b?1:0).join("")).join("")).digest("hex")}};
 R.qr={short:M("hello"),real:M(__REAL__)};R.tooLong=qrMatrix("x".repeat(1000));
 R.all=page();R.posts=posts;console.log(JSON.stringify(R));process.exit(0)})().catch(e=>{console.error(e);process.exit(1)});
"""

REAL = "mp pair mpp-" + "A" * 228 + "  # asks daniels-MacBook-Pro-2's owner to let this MyPlow message theirs; they grant or decline; undo with: mp pair rm daniels-MacBook-Pro-2"
JORDAN = "[from {sender}, on another MyPlow] {their_message}\n(Jordan's text. Answer with {reply_command}. {safety_line} Confirm: {confirm_command})"
STATE = {"host": "me",
         "requests": [{"host": "riley-mbp", "name": "Riley", "say": "I am Riley's agent.", "at": 1}],
         "granted": [{"host": "jordan-air", "name": "Jordan", "said": "", "waiting": False, "text": "Jordan's text.", "asking": False, "template": JORDAN, "defaults": JORDAN,
                      "required": ["their_message", "sender", "reply_command"], "warned": ["safety_line", "confirm_command"], "identity": ["sender"], "both": True},
                     {"host": "lin-1", "name": "lin-1", "said": "", "waiting": False, "text": "", "asking": True, "template": "[from {sender}, on another MyPlow] {their_message}",
                      "defaults": "[from {sender}, on another MyPlow] {their_message}", "required": ["their_message", "sender"], "warned": [], "identity": ["sender"], "both": False}],
         "reach": [{"host": "casey-mini", "name": "Casey", "said": "Asking about logs.", "waiting": False, "text": "Hidden text.", "asking": False, "both": True},
                   {"host": "morgan-box", "name": "Morgan", "said": "Hi.", "waiting": True, "text": "", "asking": False}],
         "ended": ["taylor-pc"], "service": "off"}


PINNED = ("99ccedcf0d82e92a63894c8b42e405a3ac50eafa33ee2ce68fa895813fdcb386", "c1028160c138d9bac909913f1262f9c1870ca52a9e38427d4d4ba192930c42b0")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class AccessTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        js = HARNESS.replace("__IDS__", json.dumps(IDS)).replace("__STATE__", json.dumps(STATE)).replace("__REAL__", json.dumps(REAL)).replace("__SCRIPT__", SCRIPT)
        with tempfile.NamedTemporaryFile("w", suffix=".js") as f:
            f.write(js)
            f.flush()
            r = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr[-800:]
        cls.r = json.loads(r.stdout)

    def test_tabs_with_counts_open_on_what_waits_for_him(self):
        self.assertEqual(self.r["firstTabs"], ["Waiting on you 1", "Has access to you 2", "You have access to 2", "Ended 1", "New line"])
        self.assertEqual(self.r["firstOn"], "Waiting on you 1")
        self.assertIn("hot", self.r["hot"])

    def test_it_says_paired_and_what_a_pairing_proves_never_verified(self):
        self.assertTrue(self.r["request"])
        self.assertNotIn("Verified", HTML)
        self.assertIn('"Paired"', HTML)

    def test_his_machines_get_no_mark_of_their_own(self):
        self.assertEqual(self.r["names"], ["Jordan", "lin-1"])
        self.assertEqual(self.r["ownText"], "[from {sender}, on another MyPlow] {their_message}")
        self.assertNotIn("own machine", HTML)
        self.assertNotIn("Your machines", HTML)

    def test_the_box_blocks_what_cannot_be_lost_warns_on_the_rest_and_restores_in_one_click(self):
        self.assertEqual(self.r["boxCopy"], "Copy")
        self.assertTrue(self.r["unchangedHidden"], "Save and Discard appear only when changed")
        self.assertTrue(self.r["lostOff"] and self.r["lostSays"] and self.r["lostBack"] and self.r["backOn"])
        self.assertTrue(self.r["replyOff"])
        self.assertTrue(self.r["senderOff"] and self.r["senderSays"] and self.r["senderBack"], "who sent it comes back first")
        self.assertTrue(self.r["wordsOn"], "the words around it are the owner's")
        self.assertTrue(self.r["orderOff"] and self.r["orderSays"] and self.r["orderFixed"] and self.r["orderOn"])
        self.assertTrue(self.r["warnOn"] and self.r["warnSays"] and self.r["warnBack"])
        self.assertTrue(self.r["oddSays"] and self.r["discarded"])

    def test_it_says_when_messages_between_myplows_are_off_as_no_other_page_has_them_now(self):
        self.assertIn("mp plugins on a2a", self.r["serviceSays"])

    def test_grant_goes_both_ways_by_default_and_one_click_says_only_they_reach_me(self):
        grants = [body for op, body in self.r["posts"] if op == "grant"]
        self.assertEqual(grants, [{"host": "riley-mbp", "both": False}, {"host": "riley-mbp"}])
        self.assertIn("goes both ways", self.r["requestSays"])

    def test_you_have_access_to_says_they_reach_you_too_and_links_to_the_box(self):
        self.assertTrue(self.r["crossNote"])
        self.assertTrue(self.r["crossOn"].startswith("Has access to you"))

    def test_while_the_agent_rewrites_after_an_ask_the_box_says_so_and_moves(self):
        self.assertEqual(self.r["ownBusy"], "Your agent is rewriting this")
        self.assertEqual(self.r["jordanBusy"], "", "no ask open, nothing moving")
        self.assertIn("@keyframes mp-rewrite", BOX)
        self.assertIn("prefers-reduced-motion", BOX, "still for whoever asked for less motion")

    def test_you_have_access_to_shows_no_permission_message(self):
        self.assertEqual((self.r["reachTextareas"], self.r["reachInputs"]), (0, 0))
        self.assertNotIn("Permission message", self.r["reachText"])
        self.assertNotIn("What it tells the agent", self.r["reachText"])
        self.assertNotIn("Hidden text", self.r["reachText"])
        self.assertNotIn("hidden package", self.r["reachText"])
        self.assertIn("You said:", self.r["reachText"])
        self.assertTrue(self.r["reachRevoke"])

    def test_find_narrows_every_count_and_arrows_move_between_people(self):
        self.assertEqual(self.r["foundTabs"], ["Waiting on you 0", "Has access to you 1", "You have access to 0", "Ended 0", "New line"])
        self.assertEqual((self.r["afterDown"], self.r["afterUp"]), ("lin-1", "Jordan"))

    def test_every_action_reaches_its_route_and_revoke_takes_a_second_click(self):
        posts = {op: body for op, body in self.r["posts"]}
        self.assertEqual(posts["save"], {"host": "jordan-air", "text": JORDAN + " Edited."})
        self.assertEqual(posts["ask"], {"host": "jordan-air", "text": "Let them ask"})
        self.assertEqual(posts["remove"], {"host": "jordan-air"})
        self.assertEqual(posts["grant"], {"host": "riley-mbp"})
        self.assertEqual(self.r["afterOneRevoke"], 0, "one stray click must not end access")
        self.assertIn("Click again", self.r["armedText"])

    def test_after_a_grant_it_moves_on_to_their_text_and_the_other_tabs_work(self):
        self.assertTrue(self.r["afterGrantOn"].startswith("Has access to you"))
        self.assertIn("taylor-pc", self.r["ended"])
        self.assertTrue(self.r["newLine"])

    def test_the_line_and_its_qr_stay_hidden_until_asked_and_again_after_leaving(self):
        self.assertFalse(self.r["lineBefore"], "a one-use secret on a screen that may be streaming")
        self.assertRegex(self.r["lineNote"], r"Good for 5 minutes, until \S.*, and one use\.")
        self.assertNotIn("24 h", HTML)
        self.assertEqual((self.r["qrBefore"], self.r["qrAfter"], self.r["qrAfterLeaving"]), (0, 1, 0))
        self.assertTrue(self.r["lineAfter"])
        self.assertFalse(self.r["lineAfterLeaving"])
        self.assertTrue(self.r["hideLabel"])

    def test_copy_works_while_the_line_is_hidden(self):
        self.assertEqual(self.r["copied"], "mp pair mpp-SECRET")

    def test_the_qr_is_made_here_and_decodes(self):
        self.assertEqual(re.findall(r'<script src="([^"]+)"', HTML), ["/textbox.js"], "our own box and nothing else")
        for outside in ("<img", "https://", "http://"):
            self.assertNotIn(outside, HTML.replace("http://www.w3.org", ""), "a one-use secret must not leave the page")
        self.assertIsNone(self.r["tooLong"])
        # Pinned after zbar read these exact matrices back: any change to the encoder fails here.
        self.assertEqual(self.r["qr"]["short"]["size"], 21)
        self.assertEqual(self.r["qr"]["real"]["size"], 77, "a real line is about 380 bytes: version 15")
        self.assertEqual((self.r["qr"]["short"]["sha"], self.r["qr"]["real"]["sha"]), PINNED)


if __name__ == "__main__":
    unittest.main()
