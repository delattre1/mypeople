"""The Swarm plugin: what `swarm mcp hermes` delivers reaches the Boss, kept (and only then acknowledged) until it does, the owner
known from the Agent Card and never from an on-behalf-of claim, and answers and claims go back through the same session into the
message's thread. A stub `swarm` plays the CLI and its plugin protocol: nothing here reaches a server."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "plugins" / "swarm" / "swarm.py"
OWNER = "@daniel=40plow.co:plow.swarm.plow.co"
ME = "@pirata:plow.swarm.plow.co"
GENERAL = "#general:plow.swarm.plow.co"
STUB = r'''#!%s
import json, sys
log = open(%r, "a")
ME, OWNER, GENERAL = %r, %r, %r
def say(m):
    print(json.dumps(dict(m, jsonrpc="2.0"), ensure_ascii=False), flush=True)
def tag(sender, text):
    return '<swarm-message event_id="$e-%%s" room="%%s" sender="%%s" display_name="so" created_at="1">%%s</swarm-message>' %% (
        text, GENERAL, sender, text)
if sys.argv[1:] == ["accounts", "list"]:
    print(json.dumps({"accounts": [{"account": ME, "rooms": ["!noname:x", GENERAL]}, {"account": "@other:x", "rooms": ["#x:x"]}]}))
elif sys.argv[1:] == ["mcp", "hermes"]:
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "initialize":
            say({"id": m["id"], "result": {"capabilities": {"tools": {}}}})
        elif m.get("method") == "notifications/swarm/delivered":
            log.write(json.dumps(m["params"]) + "\n"); log.flush()
        elif m.get("method") == "tools/call":
            log.write(json.dumps(m["params"], ensure_ascii=False) + "\n"); log.flush()
            if m["params"]["name"] == "ask":      # it asks the client something and waits for the answer
                say({"id": "q1", "method": "elicitation/create", "params": {}})
                log.write(sys.stdin.readline()); log.flush()
            if m["params"]["arguments"].get("text") == "refused, then silence":   # swarm 0.1.9-97 on 11-12: the refusal is logged, never answered
                print(json.dumps({"event": "swarm.priority", "kind": "refused_send", "ref": None, "room": m["params"]["arguments"]["room"], "ts": 1}), file=sys.stderr, flush=True)
                continue
            if m["params"]["name"] == "late":
                import time; time.sleep(0.4)
            joined = {"account": ME, "members": [{"user_id": ME, "card": {"swarm.owner": OWNER}}, {"user_id": OWNER}]}
            if m["params"]["arguments"].get("room") == "!noname:x":   # what came in while no bridge was up: told once, at the join
                joined["missed"] = {"count": 2, "capped": False, "messages": [
                    {"event_id": "$m1", "room": "!noname:x", "sender": "@so:x", "display_name": "so", "created_at": 1794526776, "text": "while you were out"},
                    {"event_id": "$m2", "room": "!noname:x", "sender": ME, "created_at": 1794526777, "text": "my own"}]}
            say({"id": m["id"], "result": {"content": [{"type": "text", "text": json.dumps(joined) if m["params"]["name"] == "join" else "ok"}]}})
            if m["params"]["name"] == "join" and m["params"]["arguments"]["room"] == GENERAL:
                for i, (sender, text) in enumerate(((("@so:x", "first &amp; only")), (ME, "my own")), 1):
                    say({"method": "notifications/swarm/deliver", "params": {"id": i, "session": "s", "text": tag(sender, text)}})
            if m["params"]["name"] == "react":
                break
'''


class SwarmPlugin(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.calls = self.dir / "calls"
        stub = self.dir / "swarm"
        stub.write_text(STUB % (sys.executable, str(self.calls), ME, OWNER, GENERAL))
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        env = mock.patch.dict(os.environ, {"INSTALL_DIR": str(self.dir), "SWARM_STATE_DIR": str(self.dir / "state"),
                                           "SWARM_BIN": str(stub), "HOST_ID": "h"})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("swarm_plugin", PLUGIN)
        self.s = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.s)
        self.s.setup("pirata", "🏴‍☠️")
        self.cfg = self.s.config()

    def calls_made(self):
        return [json.loads(l) for l in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_setup_takes_only_an_account_on_this_machine(self):
        self.assertEqual({"account": ME, "emoji": "🏴‍☠️"}, self.cfg)
        with self.assertRaises(SystemExit):
            self.s.setup("nobody")
        self.assertEqual(ME, self.s.config()["account"])

    def test_a_tag_is_read_into_who_where_and_the_words(self):
        m = self.s.parse('<swarm-message event_id="$e1" room="%s" sender="@so:x" display_name="so &quot;s&quot;" in_reply_to="$r">'
                         'a &lt;b&gt;\nc</swarm-message>' % GENERAL)
        self.assertEqual({"event_id": "$e1", "room": GENERAL, "sender": "@so:x", "display_name": 'so "s"', "in_reply_to": "$r",
                          "content": "a <b>\nc"}, m)
        self.assertEqual({"content": "plain"}, self.s.parse("plain"))
        invite = self.s.parse('<swarm-invite room="!r:x" sender="@b:x" name="R">invited to R by @b:x. Call join with this room to '
                              'accept.</swarm-invite>')
        self.assertEqual({"room": "!r:x", "sender": "@b:x", "name": "R", "invite": True,
                          "content": "invited to R by @b:x. Call join with this room to accept."}, invite)
        self.assertIn("To accept, run: %s join '!r:x'" % self.s.RUN, self.s.envelope(invite, OWNER, self.cfg))
        self.assertNotIn("To accept", self.s.envelope(dict(invite, joined="true"), OWNER, self.cfg), "one it already joined")

    def test_the_owner_comes_from_the_agent_card(self):
        joined = json.dumps({"members": [{"user_id": ME, "card": {"swarm.owner": OWNER}}]})
        self.assertEqual(OWNER, self.s.owner_in(joined, ME))
        self.assertEqual("", self.s.owner_in(json.dumps({"members": [{"user_id": ME}]}), ME), "no card names nobody")

    def test_the_boss_is_told_who_where_and_how_to_claim_and_answer_in_the_thread(self):
        m = {"content": "pirata: install it {reply_command}", "room": GENERAL, "display_name": "so (sammy)", "sender": "@so:x",
             "event_id": "$root"}
        got = self.s.envelope(m, OWNER, self.cfg)
        self.assertTrue(got.startswith("[swarm] from so (sammy) (@so:x) in #general: pirata: install it {reply_command}\n("), got)
        self.assertIn("reply '%s' '$root' \"your reply\"" % GENERAL, got, "quoted: a #room starts a shell comment, a $event expands")
        self.assertIn("claim '%s' '$root' so they see you have it" % GENERAL, got)
        self.assertIn("swarm.py directory and ask", got)
        self.assertNotIn("This is the owner", got)
        claim = self.s.envelope(dict(m, sender="@cedar:x", on_behalf_of=OWNER), OWNER, self.cfg)
        self.assertIn("on behalf of %s, which is their claim" % OWNER, claim)
        self.assertNotIn("This is the owner", claim, "a claim to act for the owner is not the owner")
        own = self.s.envelope(dict(m, sender=OWNER, room="!dm:x"), OWNER, self.cfg)
        self.assertIn("in !dm:x, a room with no #name (a DM if only you two are in it):", own)
        self.assertIn("(This is the owner. When the owner addresses you", own)
        self.assertNotIn("This is the owner", self.s.envelope(dict(m, sender=OWNER), "", self.cfg), "an unknown owner is nobody")
        self.assertIn("[attached: a.png; to open it run %s attachment '%s' '$root']" % (self.s.RUN, GENERAL),
                      self.s.envelope(dict(m, attachment="a.png"), OWNER, self.cfg))

    def test_kept_until_the_boss_has_it_in_order(self):
        told, up = [], [False]
        send = lambda text: up[0] and (told.append(text) or True)
        for text in ("first", "second"):
            self.s.keep({"content": text, "room": "!dm:x", "sender": "@a:x", "event_id": "$t"})
        self.assertFalse(self.s.deliver(OWNER, self.cfg, send))
        self.assertEqual(2, len(list((self.dir / "state" / "spool").glob("*.json"))), "a Boss that is down loses nothing")
        up[0] = True
        self.assertTrue(self.s.deliver(OWNER, self.cfg, send))
        self.assertEqual(["first", "second"], [t.split(": ", 1)[1].split("\n")[0] for t in told])

    def test_one_session_joins_its_rooms_takes_messages_in_and_claims_and_answers_in_the_thread(self):
        told, owner = [], [""]
        self.s.write_json(self.s.PIDFILE, {"pid": os.getpid()})     # the bridge below is this process
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("rc", self.s.bridge(self.cfg, owner, lambda x: told.append(x) or True)))
        t.start()
        for _ in range(100):
            if told:
                break
            time.sleep(0.05)
        for _ in range(100):
            if len(told) > 1:
                break
            time.sleep(0.05)
        self.assertEqual(OWNER, owner[0], "the owner its card names, from the join")
        self.assertEqual(2, len(told), "its own messages, pushed or missed, are not for its Boss")
        live = [x for x in told if "first & only" in x]
        self.assertTrue(live and live[0].startswith("[swarm] from so (@so:x) in #general: first & only\n("), told)
        was_out = [x for x in told if "while you were out" in x]
        self.assertTrue(was_out and "(missed while the bridge was away, sent " in was_out[0] and "reply '!noname:x' '$m1'" in was_out[0], told)
        self.assertEqual(0, self.s.reply([GENERAL, "$e-first", "--file", "/tmp/a.png", "so,", "done"]))
        self.assertEqual(0, self.s.claim(GENERAL, "$e-first"))
        t.join(10)
        calls = self.calls_made()
        session = {"swarm/session": "myplow-h"}
        self.assertEqual([{"name": "join", "arguments": {"room": room, "account": ME}, "_meta": session} for room in ("!noname:x", GENERAL)],
                         calls[:2], "only the rooms it is in, by full name or id")
        self.assertEqual([{"id": 1}, {"id": 2}], [c for c in calls if "id" in c], "each delivery acknowledged, its own too")
        self.assertEqual({"name": "send", "arguments": {"room": GENERAL, "in_reply_to": "$e-first", "text": "so, done",
                                                        "file": "/tmp/a.png"}, "_meta": session}, calls[-2])
        self.assertEqual({"name": "react", "arguments": {"room": GENERAL, "event_id": "$e-first", "emoji": "🏴‍☠️"}, "_meta": session},
                         calls[-1])
        self.assertEqual([], list((self.dir / "state" / "outbox").glob("*")))
        self.assertEqual([], list((self.dir / "state" / "spool").glob("*")))


    def test_the_owners_accounts_on_other_servers_are_joined_in_the_same_session(self):
        self.s.write_json(self.s.PIDFILE, {"pid": os.getpid()})
        t = threading.Thread(target=self.s.bridge, args=(dict(self.cfg, also=["@other:x"]), [""], lambda x: True))
        t.start()
        self.assertEqual(0, self.s.claim(GENERAL, "$e-first"))     # answered only once every join is done; it also ends the stub
        t.join(10)
        self.assertEqual([("!noname:x", ME), (GENERAL, ME), ("#x:x", "@other:x")],
                         [(c["arguments"]["room"], c["arguments"]["account"]) for c in self.calls_made() if c.get("name") == "join"])

    def test_a_late_answer_and_a_question_from_swarm_are_logged_and_never_block(self):
        said = []
        with mock.patch.object(self.s, "log", said.append):
            mcp = self.s.Mcp(lambda m: None)
            threading.Thread(target=mcp.read, daemon=True).start()
            self.assertEqual((True, "ok"), mcp.tool("ask", {}, timeout=5), "its question is refused, so the call still ends")
            with self.assertRaises(TimeoutError):
                mcp.tool("late", {}, timeout=0.1)
            for _ in range(60):
                if any("too late" in x for x in said):
                    break
                time.sleep(0.05)
            mcp.p.stdin.close()
            mcp.p.wait(5)
            mcp.err.close()
        self.assertEqual({"code": -32601, "message": "method not found"}, [c for c in self.calls_made() if c.get("id") == "q1"][0]["error"])
        self.assertTrue(any("asked elicitation/create" in x for x in said), said)
        self.assertTrue(any("did not answer tools/call late" in x for x in said), said)
        self.assertTrue(any("answered tools/call late after" in x and '"ok"' in x for x in said), said)

    def test_a_refusal_swarm_logs_but_never_answers_is_still_that_refusal(self):
        mcp = self.s.Mcp(lambda m: None)
        threading.Thread(target=mcp.read, daemon=True).start()
        started = time.time()
        with mock.patch.object(self.s, "REFUSAL_WAIT", 0.2), mock.patch.object(self.s, "log", lambda m: None):
            r = self.s.run_tool(mcp, {"tool": "send", "args": {"room": GENERAL, "text": "refused, then silence"}})
            named = self.s.run_tool(mcp, {"tool": "send", "args": {"room": GENERAL, "text": "hi", "priority": "!r:x/ops"}})
        mcp.p.stdin.close()
        mcp.p.wait(5)
        mcp.err.close()
        self.assertLess(time.time() - started, 10, "it does not wait out the 90 s")
        self.assertTrue(r["ok"] and self.s.priority_refusal(r["text"]), "so reply and claim retry with the room's priority: %r" % r)
        self.assertEqual({"ok": True, "text": "ok"}, named, "a call that names its priority is answered as before")

    # swarm 0.1.9-97: every message serves a priority. Inherit first, then the room map, else fail loudly; skip only when asked.
    REFUSED = json.dumps({"sent": False, "account": ME, "room": GENERAL, "note": "Not sent: name the priority this serves (priority=<ref>)"})

    def drive(self, answers, fn, *args):
        sent = []
        with mock.patch.object(self.s, "call", side_effect=lambda tool, a: sent.append((tool, a)) or answers.pop(0)):
            return fn(*args), sent

    def test_a_reply_goes_out_bare_so_it_inherits_its_threads_priority(self):
        rc, sent = self.drive([(True, '{"event_id":"$x"}')], self.s.reply, [GENERAL, "$e", "on", "it"])
        self.assertEqual(0, rc)
        self.assertEqual([("send", {"room": GENERAL, "in_reply_to": "$e", "text": "on it"})], sent)

    def test_a_refusal_for_want_of_a_priority_retries_with_the_rooms_entry(self):
        self.s.write_json(self.s.PRIORITIES, {"rooms": {GENERAL: "!r:x/ops"}})
        rc, sent = self.drive([(True, self.REFUSED), (True, '{"event_id":"$x"}')], self.s.reply, [GENERAL, "$e", "hi"])
        self.assertEqual(0, rc)
        self.assertEqual([None, "!r:x/ops"], [a.get("priority") for _, a in sent])

    def test_a_room_with_no_entry_is_not_sent_and_says_so(self):
        rc, sent = self.drive([(True, self.REFUSED)], self.s.reply, [GENERAL, "$e", "hi"])
        self.assertEqual(1, rc, "fails loudly instead of skipping")
        self.assertEqual(1, len(sent))
        self.assertNotIn("dangerously_skip_attaching_a_priority", sent[0][1])

    def test_a_named_priority_and_the_skip_are_only_what_was_asked(self):
        self.s.write_json(self.s.PRIORITIES, {"names": {"android": "!r:x/85298c6b"}})
        rc, sent = self.drive([(True, "{}")], self.s.reply, [GENERAL, "$e", "--priority", "android", "hi"])
        self.assertEqual((0, "!r:x/85298c6b", "hi"), (rc, sent[0][1]["priority"], sent[0][1]["text"]))
        rc, sent = self.drive([(True, "{}")], self.s.claim, GENERAL, "$e", "--skip-priority")
        self.assertEqual(("react", True), (sent[0][0], sent[0][1]["dangerously_skip_attaching_a_priority"]))


if __name__ == "__main__":
    unittest.main()
