"""The web-chat plugin end to end, offline: the relay (relay.py) in-process, the plugin polling it, a stub mp."""
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent / "mypeople/runtime/plugins/web-chat"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WebChatPlugin(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        relay = load("relay", PLUGIN / "relay.py")
        self.server = relay.serve(relay.Relay(self.dir / "relay", hashlib.sha256(b"agent-key").hexdigest()), port=0, host="127.0.0.1")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # A stub mp that records its argv, one JSON line per call; it fails while <dir>/fail exists.
        self.mp = self.dir / "mp"
        self.mp.write_text(f"#!{sys.executable}\nimport json, os, sys\nopen({str(self.dir / 'argv')!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                           f"sys.exit(1 if os.path.exists({str(self.dir / 'fail')!r}) else 0)\n")
        self.mp.chmod(self.mp.stat().st_mode | stat.S_IEXEC)
        os.environ.update(WEB_CHAT_STATE_DIR=str(self.dir / "state"), MP_BIN=str(self.mp), BOSS_AGENT="mac/main:Boss")
        (self.dir / "state").mkdir()
        (self.dir / "state/creds.json").write_text(json.dumps({"url": self.base, "key": "agent-key"}))
        self.plugin = load("web_chat", PLUGIN / "web-chat.py")

    def tearDown(self):
        self.server.shutdown()

    def http(self, method, path, body=None, token=None):
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, (json.loads(r.read()) if "json" in r.headers.get("Content-Type", "") else r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def sent(self):
        p = self.dir / "argv"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def test_visitor_to_boss_and_back(self):
        status, page = self.http("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<div class="live" id="chat">', page, "/ is the landing page, its phone holding the chat")
        status, chat = self.http("GET", "/chat")
        self.assertEqual(status, 200)
        self.assertIn(b'placeholder="iMessage"', chat)
        self.assertEqual(self.http("POST", "/messages", {"text": "hi", "page": "Bad Slug"})[0], 422)
        _, a = self.http("POST", "/messages", {"text": "I want an email agent", "page": "assistant"})
        _, b = self.http("POST", "/messages", {"text": "hello from B"})
        self.http("POST", "/messages", {"text": "and my calendar"}, a["token"])

        # The plugin hands each message to the Boss in order, the page tag on the first only, with the reply command.
        self.assertTrue(self.plugin.poll_once(wait=0))
        calls = self.sent()
        self.assertEqual([c[:2] for c in calls], [["send", "mac/main:Boss"]] * 3)
        va = calls[0][2].split()[4]
        self.assertTrue(calls[0][2].startswith(f"[web-chat] from web visitor {va} (came from page assistant): I want an email agent"))
        self.assertIn(f"reply {va} \"your reply\"", calls[0][2])
        self.assertIn("react", calls[0][2])
        self.assertNotIn("came from page", calls[2][2], "only the first message carries the page")
        self.assertNotIn("agent-key", "".join(c[2] for c in calls), "the agent key never reaches the Boss's text")
        self.assertTrue(self.plugin.poll_once(wait=0))
        self.assertEqual(len(self.sent()), 3, "nothing twice")

        # A failed hand-off keeps the mark: the message comes again on the next pass.
        (self.dir / "fail").touch()
        self.http("POST", "/messages", {"text": "are you there?"}, b["token"])
        self.assertFalse(self.plugin.poll_once(wait=0))
        (self.dir / "fail").unlink()
        self.assertTrue(self.plugin.poll_once(wait=0))
        self.assertEqual([c[2].split(": ", 1)[1].split("\n")[0] for c in self.sent()[3:]], ["are you there?"] * 2)

        # The Boss's reply reaches that visitor's page, live and on reload; the other visitor sees nothing of it.
        events = urllib.request.urlopen(f"{self.base}/events?token={a['token']}", timeout=10)
        self.assertEqual(events.readline(), b": open\n")
        events.readline()
        self.plugin.call("POST", "/agent/reply", {"visitor": va, "text": "On it!"})
        self.assertEqual(events.readline(), b"event: message\n")
        self.assertEqual(json.loads(events.readline().removeprefix(b"data: "))["text"], "On it!")
        mid = self.http("GET", "/messages", token=a["token"])[1]["messages"][0]["id"]
        self.plugin.call("POST", "/agent/react", {"visitor": va, "message": mid, "emoji": "👀"})
        thread = self.http("GET", "/messages", token=a["token"])[1]["messages"]
        self.assertEqual([(m["mine"], m["text"]) for m in thread][-1], (False, "On it!"))
        self.assertEqual(thread[0]["reactions"], ["👀"])
        self.assertNotIn("On it!", json.dumps(self.http("GET", "/messages", token=b["token"])[1]))
        # The agent reads one visitor's thread by id (Willow's handover), oldest first.
        history = self.plugin.call("GET", f"/agent/thread?visitor={va}")["messages"]
        self.assertEqual([(m["mine"], m["text"]) for m in history],
                         [(True, "I want an email agent"), (True, "and my calendar"), (False, "On it!")])
        self.assertEqual(self.http("GET", f"/agent/thread?visitor={va}", token=a["token"])[0], 404, "never with a visitor token")

        # Only the agent key opens /agent; a visitor token or a made-up one gets nothing.
        self.assertEqual(self.http("GET", "/agent/inbox?after=0", token=a["token"])[0], 404)
        self.assertEqual(self.http("POST", "/agent/reply", {"visitor": va, "text": "x"}, "wrong")[0], 404)
        self.assertEqual(self.http("GET", "/messages", token="made-up")[0], 401)


if __name__ == "__main__":
    unittest.main()
