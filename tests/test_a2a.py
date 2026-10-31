"""A2A: plain mp send to an agent on another, paired MyPlow, through the relay, and its answer back."""
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
import sandbox

PLUGIN = Path(__file__).resolve().parents[1] / "mypeople/runtime/plugins/a2a/a2a.py"


class FakeQueue:
    """The local queue-server, as the bridge sees it: tasks by host, results, submits."""

    def __init__(self):
        self.tasks = {}

    def submit(self, typ, target, payload=None):
        tid = "t%d" % (len(self.tasks) + 1)
        self.tasks[tid] = {"task_id": tid, "type": typ, "target_agent": target, "payload": payload or {},
                           "host": target.split("/")[0] if "/" in target else "", "delivered": False,
                           "ok": None, "result": None}
        return tid

    def sends(self):
        return [t for t in self.tasks.values() if t["type"] == "send" and not t["delivered"]]

    def __call__(self, method, path, body=None):
        if path.startswith("/task/poll?hostname="):
            host, out = path.split("=", 1)[1], []
            for t in self.tasks.values():
                if not t["delivered"] and t["host"] in ("", host):
                    t["delivered"] = True
                    out.append({k: t[k] for k in ("task_id", "type", "target_agent", "payload")})
            return 200, out
        if path == "/task/submit":
            return 200, {"task_id": self.submit(body["type"], body["target_agent"], body.get("payload"))}
        if path == "/task/result":
            self.tasks[body["task_id"]].update(ok=body["ok"], result=body["result"])
            return 200, {"ok": True}
        t = self.tasks.get(path.rsplit("/", 1)[1])
        return (200, {"ok": t["ok"], "result": t["result"]}) if t else (404, None)


class A2ATest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"INSTALL_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.a2a = sandbox.load_file(PLUGIN, "a2a")
        self.a2a.log = lambda msg: None
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        threading.Thread(target=self.a2a.relay, args=(port, os.path.join(self.tmp.name, "relay.db")),
                         daemon=True).start()
        self.relay = "http://127.0.0.1:%d/a2a" % port
        for _ in range(50):
            try:
                urllib.request.urlopen(self.relay, timeout=1)
                break
            except OSError:
                time.sleep(0.05)
        self.alpha, self.qa = self.bridge("alpha")
        self.beta, self.qb = self.bridge("beta")

    def bridge(self, host):
        b = self.a2a.Bridge(host=host, state_dir=os.path.join(self.tmp.name, host), queue_url="unused",
                            secret="", relay_url=self.relay)
        b.queue = FakeQueue()
        return b, b.queue

    def pair(self):
        code = self.alpha.invite()
        self.assertIn("paired with alpha", self.beta.accept(code))
        self.alpha.step()                       # reads beta's hello: the pairing completes on both sides
        return code

    def test_send_reply_and_receipt(self):
        self.pair()
        tid = self.qa.submit("send", "beta/main:Boss", {"message": "hi", "reply_to": "alpha/main:eng-1"})
        self.alpha.step()
        self.beta.step()
        [got] = self.qb.sends()
        self.assertEqual(got["target_agent"], "beta/main:Boss")
        self.assertEqual(got["payload"], {"message": "[from alpha/main:eng-1, on another MyPlow] hi",
                                          "reply_to": "alpha/main:eng-1"})
        # beta's queue delivers it; its result travels back as alpha's mp send result
        got.update(delivered=True, ok=True, result="sent")
        self.beta.step()
        self.alpha.step()
        self.assertEqual((self.qa.tasks[tid]["ok"], self.qa.tasks[tid]["result"]), (True, "sent"))
        # the answer is plain mp send to the id it came from
        self.qb.submit("send", "alpha/main:eng-1", {"message": "pong", "reply_to": "beta/main:Boss"})
        self.beta.step()
        self.alpha.step()
        [back] = self.qa.sends()
        self.assertEqual(back["target_agent"], "alpha/main:eng-1")
        self.assertEqual(back["payload"]["message"], "[from beta/main:Boss, on another MyPlow] pong")
        back["delivered"] = True
        # the Stop hook's notice has no sender agent and no reply_to: it crosses from the host, and is not
        # answered in turn (a reply_to on it would ping-pong notices)
        self.qb.submit("send", "alpha/main:eng-1", {"message": "[AGENT NOTIFICATION] beta/main:Boss finished: ok"})
        self.beta.step()
        self.alpha.step()
        [notice] = self.qa.sends()
        self.assertEqual(notice["payload"], {"message": "[from beta, on another MyPlow] [AGENT NOTIFICATION] "
                                                        "beta/main:Boss finished: ok"})

    def test_a_failure_after_queued_reaches_the_sender(self):
        """mp send printed "queued" (the peer was slow); the peer then could not deliver: the sender hears it."""
        self.pair()
        tid = self.qa.submit("send", "beta/main:eng-9", {"message": "GO", "reply_to": "alpha/main:eng-1"})
        with mock.patch.object(self.a2a, "RECEIPT_WAIT", -1):
            self.alpha.step()
        self.assertTrue(self.qa.tasks[tid]["ok"])
        self.assertIn("queued", self.qa.tasks[tid]["result"])
        self.beta.step()
        [got] = self.qb.sends()
        got.update(delivered=True, ok=False, result="not sent: beta/main:eng-9 was retired 4 h ago (killed)")
        self.beta.step()
        self.alpha.step()
        [notice] = self.qa.sends()
        self.assertEqual(notice["target_agent"], "alpha/main:eng-1")
        self.assertIn("beta/main:eng-9 was retired 4 h ago", notice["payload"]["message"])

    def test_only_send_crosses_and_forgeries_drop(self):
        self.pair()
        kill = self.qa.submit("kill", "beta/main:Boss")
        self.alpha.step()
        self.assertFalse(self.qa.tasks[kill]["ok"])
        self.assertIn("refused", self.qa.tasks[kill]["result"])
        # signed with the wrong key, into the right box: the relay takes it, beta drops it
        key = self.beta.read("keys.json", {})["peers"]["alpha"]["key"]
        body = {"kind": "send", "from": "alpha/main:Boss", "to": "beta/main:Boss", "text": "x", "reply": False,
                "ts": time.time(), "n": "1"}
        self.a2a.post(self.relay + "/put", {"box": self.a2a.box(key, "alpha", "beta"),
                                            "msg": self.a2a.Bridge.sign(None, "00" * 32, body)})
        self.beta.step()
        self.assertEqual(self.qb.sends(), [])

    def test_code_is_one_use_and_a_taken_name_is_refused(self):
        code = self.pair()
        gamma, _ = self.bridge("gamma")
        gamma.accept(code)                      # the spent code: alpha never pairs with gamma
        self.alpha.step()
        self.assertEqual(sorted(self.alpha.read("keys.json", {})["peers"]), ["beta"])
        # a MyPlow naming itself after a paired peer would get that peer's messages: refused
        impostor, _ = self.bridge("beta")
        impostor.dir = Path(self.tmp.name, "impostor")
        impostor.accept(self.alpha.invite())
        before = self.alpha.read("keys.json", {})["peers"]["beta"]
        self.alpha.step()
        self.assertEqual(self.alpha.read("keys.json", {})["peers"]["beta"], before)

    def test_no_loop_guard(self):
        """Two Bosses can keep talking: no cap on messages in a row, either way (the 20-in-30-minutes guard is gone)."""
        self.pair()
        n = 45
        for _ in range(n):
            tid = self.qa.submit("send", "beta/main:Boss", {"message": "ping", "reply_to": "alpha/main:Boss"})
            self.alpha.step()
            self.assertIsNot(self.qa.tasks[tid]["ok"], False, self.qa.tasks[tid])
        self.beta.step()
        self.assertEqual(len(self.qb.sends()), n)
        # and back the other way, past where the old guard dropped incoming messages
        for _ in range(n):
            self.qb.submit("send", "alpha/main:Boss", {"message": "pong", "reply_to": "beta/main:Boss"})
            self.beta.step()
        self.alpha.step()
        self.assertEqual(len(self.qa.sends()), n)
        self.assertNotIn("conv", self.alpha.read("state.json", {}))

    def test_only_an_explicit_serve_starts_the_bridge(self):
        """A bare discord-chat.py ran a second serve beside supervise.sh's (Mac 2, 10-30); no plugin defaults to serve."""
        self.a2a.Bridge = mock.Mock()
        with mock.patch.object(socket, "getaddrinfo", socket.getaddrinfo), self.assertRaises(SystemExit):
            self.a2a.main(["a2a.py"])       # main swaps getaddrinfo process-wide: the patch puts it back
        self.a2a.Bridge.assert_not_called()

    def test_a_ts_net_name_the_system_refuses_resolves_publicly(self):
        """Tailscale's MagicDNS refuses another tailnet's ts.net names: the relay's must still resolve."""
        def system(host, *a, **k):     # refuses every name; answers an address
            if not host.replace(".", "").isdigit():
                raise socket.gaierror(8, "refused")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, a[0]))]
        with mock.patch.object(self.a2a, "_getaddrinfo", system), \
                mock.patch.object(self.a2a, "public_ips", lambda host: ["203.0.113.7"]):
            self.assertEqual(self.a2a.resolve("relay.example.ts.net", 8443)[0][4], ("203.0.113.7", 8443))
            with self.assertRaises(socket.gaierror):
                self.a2a.resolve("other.example.com", 443)      # any other name: the system's answer stands


if __name__ == "__main__":
    unittest.main()
