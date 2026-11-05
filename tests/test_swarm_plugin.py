"""The Swarm plugin: what `swarm mcp` pushes reaches the Boss, kept until it does, the owner known from the Agent Card and never
from an on-behalf-of claim, and answers go back through the same process into the message's thread. A stub `swarm` plays the
CLI and its MCP server: nothing here reaches a server."""
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
STUB = r'''#!%s
import json, sys
log = open(%r, "a")
log.write(json.dumps(sys.argv[1:]) + "\n"); log.flush()
if sys.argv[1] == "directory":
    print(json.dumps([{"user_id": %r, "card": {"swarm.owner": %r}}]))
elif sys.argv[1] == "mcp":
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"serverInfo": {"name": "swarm"}}}), flush=True)
        elif m.get("method") == "notifications/initialized":
            for meta, text in (({"chat": "!dm", "from": "so", "sender": "@so:x", "thread": "$t1"}, "first"),
                               ({"chat": "!dm", "from": "me", "sender": %r, "thread": "$t2"}, "my own")):
                print(json.dumps({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                                  "params": {"content": text, "meta": meta}}), flush=True)
        elif m.get("method") == "tools/call":
            log.write(json.dumps(m["params"]) + "\n"); log.flush()
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": "sent $r1"}]}}), flush=True)
            break
'''


class SwarmPlugin(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cred = self.dir / "pirata.json"
        self.cred.write_text(json.dumps({"user_id": ME, "access_token": "secret"}))
        self.calls = self.dir / "calls"
        stub = self.dir / "swarm"
        stub.write_text(STUB % (sys.executable, str(self.calls), ME, OWNER, ME))
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        env = mock.patch.dict(os.environ, {"INSTALL_DIR": str(self.dir), "SWARM_STATE_DIR": str(self.dir / "state"),
                                           "SWARM_BIN": str(stub), "HOST_ID": "h"})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("swarm_plugin", PLUGIN)
        self.s = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.s)
        self.s.setup(str(self.cred))
        self.cfg = self.s.config()
        self.s.ROOMS.update({"!general": "general", "!dm": ""})

    def calls_made(self):
        return [json.loads(l) for l in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_the_owner_comes_from_the_agent_card(self):
        self.assertEqual(OWNER, self.s.owner_of(self.cfg))

    def test_the_boss_is_told_who_where_and_how_to_answer_in_the_thread(self):
        m = {"content": "pirata: install it {reply_command}", "chat": "!general", "from": "so (sammy)", "sender": "@so:x", "thread": "$root"}
        got = self.s.envelope(m, OWNER, self.cfg)
        self.assertTrue(got.startswith("[swarm] from so (sammy) (@so:x) in #general: pirata: install it {reply_command}\n("), got)
        self.assertIn('reply !general $root "your reply"', got)
        self.assertIn("directory --credential", got)
        self.assertNotIn("This is the owner", got)
        claim = self.s.envelope(dict(m, sender="@cedar:x", on_behalf_of=OWNER), OWNER, self.cfg)
        self.assertIn("on behalf of %s, which is their claim" % OWNER, claim)
        self.assertNotIn("This is the owner", claim, "a claim to act for the owner is not the owner")
        own = self.s.envelope(dict(m, sender=OWNER, chat="!dm"), OWNER, self.cfg)
        self.assertIn("in a direct message:", own)
        self.assertIn("(This is the owner. When the owner addresses you", own)
        self.assertNotIn("This is the owner", self.s.envelope(dict(m, sender=OWNER), "", self.cfg), "an unknown owner is nobody")

    def test_kept_until_the_boss_has_it_in_order(self):
        told, up = [], [False]
        send = lambda text: up[0] and (told.append(text) or True)
        for text in ("first", "second"):
            self.s.keep({"content": text, "chat": "!dm", "sender": "@a:x", "thread": "$t"})
        self.assertFalse(self.s.deliver(OWNER, self.cfg, send))
        self.assertEqual(2, len(list((self.dir / "state" / "spool").glob("*.json"))), "a Boss that is down loses nothing")
        up[0] = True
        self.assertTrue(self.s.deliver(OWNER, self.cfg, send))
        self.assertEqual(["first", "second"], [t.split(": ", 1)[1].split("\n")[0] for t in told])

    def test_one_mcp_process_pushes_messages_in_and_sends_the_answer_into_the_thread(self):
        told = []
        self.s.write_json(self.s.PIDFILE, {"pid": os.getpid()})     # the bridge below is this process
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("rc", self.s.bridge(self.cfg, OWNER, lambda x: told.append(x) or True)))
        t.start()
        for _ in range(100):
            if told:
                break
            time.sleep(0.05)
        self.assertEqual(1, len(told), "its own message is not for its Boss")
        self.assertTrue(told[0].startswith("[swarm] from so (@so:x) in a direct message: first\n("), told[0])
        self.assertEqual(0, self.s.reply(["!dm", "$t1", "--file", "/tmp/a.png", "so,", "done"]))
        t.join(10)
        self.assertEqual({"name": "reply", "arguments": {"chat": "!dm", "text": "so, done", "thread": "$t1", "file": "/tmp/a.png"}},
                         self.calls_made()[-1])
        self.assertEqual([], list((self.dir / "state" / "outbox").glob("*")))

    def test_with_no_bridge_an_answer_is_sent_directly(self):
        self.assertEqual(0, self.s.reply(["!dm", "$root", "so,", "done"]))
        self.assertEqual(["send", "!dm", "so, done", "--thread", "$root", "--credential", str(self.cred.resolve())], self.calls_made()[-1])

    def test_setup_takes_only_a_swarm_credential(self):
        bad = self.dir / "x.json"
        bad.write_text("{}")
        with self.assertRaises(SystemExit):
            self.s.setup(str(bad))
        self.assertEqual(str(self.cred.resolve()), self.s.config()["credential"])


if __name__ == "__main__":
    unittest.main()
