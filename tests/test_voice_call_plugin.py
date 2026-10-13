"""Voice Call plugin (card 79584b97bd): the owner phones his Boss.

What must hold: only the owner with the PIN gets a voice stream, and only for a call Twilio itself
vouches for; a webhook without our secret is refused and `say` is never on the public port; a stranger leaves no trace, and
our own test number gets in only while a self-expiring window is open; only those synthetic calls
are recorded. On the call every finished turn reaches the Boss, the Boss's answer is spoken only in
a pause, never over him, talking over the voice stops it, a slow Boss gets one "one sec", one
"still on it" and one offer to text, and the latency check measures the gap it claims to.
"""
import array
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request
import wave

PLUGINS = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "plugins"


def load(env):
    with mock.patch.dict(os.environ, env):
        loader = importlib.machinery.SourceFileLoader("voice_call", str(PLUGINS / "voice-call/voice-call.py"))
        spec = importlib.util.spec_from_loader("voice_call", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    return mod


OWNER = "+15555550123"  # synthetic: this repo is public
CFG = {"pin": "482913", "twilio_number": "+16505550100", "twilio_sid": "AC" + "0" * 32, "hook_secret": "s3cret",
       "twilio_key": "SK" + "0" * 32, "twilio_secret": "x", "say_token": "say"}
ENV = {"VOICE_CALL_ALLOW": OWNER, "VOICE_CALL_PUBLIC_URL": "https://h.example"}


def sync_threads(test, vc):
    # vc.threading is the process-wide module: patch it for this test only, or every later test's servers run inline
    patch = mock.patch.object(vc.threading, "Thread", lambda target, args=(), daemon=None: mock.Mock(start=lambda: target(*args)))
    patch.start()
    test.addCleanup(patch.stop)


def vouching(vc, **over):
    """Twilio's API answering GET /Calls/<sid>: a live call to our number from whoever the webhook says."""
    def twilio(cfg, method, path, form=None):
        sid = path.split("/Calls/")[1].split(".")[0]
        return over.get("code", 200), dict({"account_sid": CFG["twilio_sid"], "to": CFG["twilio_number"],
                                            "from": vc._from[sid], "status": "in-progress"}, **over.get("call", {}))
    vc._from = {}
    vc.twilio = twilio


def pin(vc, sid, caller, digits=CFG["pin"], cfg=CFG):
    vc._from[sid] = caller
    return vc.on_twilio("/voice/twilio/pin", {"CallSid": sid, "From": caller, "Digits": digits}, cfg)


class WebhookAuthTest(unittest.TestCase):
    """No Auth Token, so no signature: the webhook secret gates the door, Twilio's API vouches for the call."""

    def setUp(self):
        self.vc = load(ENV)
        self.vc.log = lambda m: None
        vouching(self.vc)

    def test_only_our_secret_gets_in(self):
        good = "Basic " + __import__("base64").b64encode(b"twilio:s3cret").decode()
        self.assertTrue(self.vc.hook_auth_ok(good, CFG))
        for bad in (None, "", "Basic " + __import__("base64").b64encode(b"twilio:guess").decode()):
            self.assertFalse(self.vc.hook_auth_ok(bad, CFG))
        self.assertFalse(self.vc.hook_auth_ok("Basic " + __import__("base64").b64encode(b"twilio:").decode(),
                                              dict(CFG, hook_secret="")))  # unset secret never matches

    def test_right_pin_needs_twilio_to_vouch_for_the_call(self):
        self.assertIn("<Connect>", pin(self.vc, "CA1", OWNER))
        for over in ({"code": 404}, {"call": {"status": "completed"}}, {"call": {"to": "+16505550199"}},
                     {"call": {"account_sid": "AC" + "1" * 32}}, {"call": {"from": "+14155550000"}}):
            vouching(self.vc, **over)
            out = pin(self.vc, "CA2", OWNER)
            self.assertNotIn("<Connect", out, over)
            self.assertIn("<Hangup/>", out)

    def test_a_forged_pin_webhook_from_a_stranger_gets_nothing(self):
        self.assertNotIn("<Connect", pin(self.vc, "CA3", "+14155550000"))  # even with the right PIN and a real call


class WhoMayCallTest(unittest.TestCase):
    def setUp(self):
        self.vc = load(ENV)
        self.logs, self.texts = [], []
        self.vc.log = self.logs.append
        self.vc.text_owner = self.texts.append
        sync_threads(self, self.vc)
        vouching(self.vc)

    def test_stranger_hears_one_sentence_and_leaves_no_trace(self):
        out = self.vc.on_twilio("/voice/twilio", {"CallSid": "CA1", "From": "+14155550000"}, CFG)
        self.assertIn("This line takes texts only", out)
        self.assertIn("<Hangup/>", out)
        self.assertNotIn("<Connect", out)
        self.assertEqual(self.logs, ["rejected a call"])  # no number, nothing else, not even Twilio's API
        deleted = []
        self.vc.twilio = lambda cfg, method, path, form=None: deleted.append((method, path)) or (204, {})
        self.vc.on_twilio("/voice/twilio/status", {"CallSid": "CA1", "CallStatus": "completed"}, CFG)
        self.assertEqual(deleted, [("DELETE", "/Calls/CA1.json")])

    def test_owner_needs_the_pin(self):
        out = self.vc.on_twilio("/voice/twilio", {"CallSid": "CA2", "From": OWNER}, CFG)
        self.assertIn('numDigits="6"', out)
        self.assertIn('action="https://twilio:s3cret@h.example/voice/twilio/pin"', out)
        self.assertNotIn("<Connect", out)
        for _ in (1, 2):
            out = self.vc.on_twilio("/voice/twilio/pin", {"CallSid": "CA2", "From": OWNER, "Digits": "111111"}, CFG)
            self.assertIn("<Gather", out)
        out = self.vc.on_twilio("/voice/twilio/pin", {"CallSid": "CA2", "From": OWNER, "Digits": ""}, CFG)
        self.assertEqual(out.count("<Hangup/>"), 1)
        self.assertNotIn("<Connect", out)
        self.assertEqual(len(self.texts), 1)  # he is told someone tried

    def test_right_pin_opens_a_stream_only_its_one_time_token_can_join(self):
        out = pin(self.vc, "CA3", OWNER)
        self.assertIn('<Stream url="wss://h.example/voice/stream">', out)
        token = out.split('name="token" value="')[1].split('"')[0]
        self.assertIsNone(self.vc.take_grant("guess"))
        grant = self.vc.take_grant(token)
        self.assertEqual(grant[1:], ("CA3", False))  # his call: never recorded
        self.assertIsNone(self.vc.take_grant(token))  # once only

    def test_token_expires_after_a_minute(self):
        out = pin(self.vc, "CA4", OWNER)
        token = out.split('name="token" value="')[1].split('"')[0]
        self.assertIsNone(self.vc.take_grant(token, now=self.vc.time.time() + 61))


class TestWindowTest(unittest.TestCase):
    """Our own Twilio number may call in only while a test window is open, and only its calls are recorded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vc = load(dict(ENV, VOICE_CALL_STATE_DIR=self.tmp.name))
        self.vc.log = lambda m: None
        self.ours = CFG["twilio_number"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_lets_only_our_number_in_and_expires(self):
        cfg = dict(CFG, test_until=1000)
        with mock.patch.object(self.vc.time, "time", return_value=999):
            self.assertIn("<Gather", self.vc.on_twilio("/voice/twilio", {"CallSid": "C1", "From": self.ours}, cfg))
            out = self.vc.on_twilio("/voice/twilio", {"CallSid": "C2", "From": "+14155550000"}, cfg)
            self.assertIn("texts only", out)  # the window never opens the door for anyone else
        with mock.patch.object(self.vc.time, "time", return_value=1001):
            self.assertIn("texts only", self.vc.on_twilio("/voice/twilio", {"CallSid": "C3", "From": self.ours}, cfg))

    def test_only_synthetic_calls_are_recorded(self):
        cfg = dict(CFG, test_until=1000)
        vouching(self.vc)
        with mock.patch.object(self.vc.time, "time", return_value=999):
            ours = pin(self.vc, "C4", self.ours, cfg=cfg)
            his = pin(self.vc, "C5", OWNER, cfg=cfg)
            grant = lambda out: self.vc.take_grant(out.split('value="')[1].split('"')[0])
            self.assertTrue(grant(ours)[2])
            self.assertFalse(grant(his)[2])  # even with a window open

    def test_window_is_capped_and_closes(self):
        self.vc.save_config(dict(CFG))
        with mock.patch("sys.stdout", io.StringIO()):
            self.vc.cmd_test_window(10_000)
            self.assertLessEqual(self.vc.load_config()["test_until"], self.vc.time.time() + 60 * 60 + 1)
            self.vc.cmd_test_window(0)
        self.assertEqual(self.vc.load_config()["test_until"], 0)


class CallTest(unittest.TestCase):
    def setUp(self):
        self.vc = load(ENV)
        self.vc.log = lambda m: None
        sync_threads(self, self.vc)
        self.t, self.played, self.cleared, self.boss = 0.0, [], [], []
        self.call = self.vc.Call(play=lambda kind, what, lang, ticket: self.played.append((kind, what, lang)),
                                 clear=lambda: self.cleared.append(1), boss=self.boss.append, now=lambda: self.t)

    def step(self, until, every=0.5):
        """Advance the clock, letting each line finish playing before the next pump."""
        while self.t < until:
            self.call.pump()
            if self.call.speaking:
                self.call.played()
            self.t += every
        out, self.played[:] = list(self.played), []
        return out

    def test_greets_then_every_turn_reaches_the_boss_with_an_ack(self):
        self.assertEqual(self.step(1), [("clip", "hello", "pt")])
        self.call.user_started()
        self.call.user_ended("What's the status of the deploy?", "en")
        self.assertEqual(len(self.boss), 1)
        self.assertIn('live on the phone (en): "What\'s the status of the deploy?"', self.boss[0])
        ticket = self.boss[0].split("]")[0].split()[-1]
        self.assertIn(f"say {ticket} ", self.boss[0])
        self.assertEqual(self.step(2), [("clip", "ack", "en")])  # he hears he was heard, in his language

    def test_the_boss_is_spoken_only_in_a_pause(self):
        self.step(1)
        self.call.user_started()
        self.call.user_ended("e o deploy?", "pt")
        ticket = self.boss[0].split("]")[0].split()[-1]
        self.step(2)
        self.call.user_started()  # he starts talking again
        self.call.say_later(ticket, "Terminou às 10:42, tudo verde.")
        self.assertEqual(self.step(4), [])  # held while he talks
        self.call.user_ended("", "pt")
        self.assertEqual(self.step(5), [("text", "Terminou às 10:42, tudo verde.", "pt")])

    def test_talking_over_the_voice_stops_it_and_tells_the_boss(self):
        self.step(1)
        self.call.user_ended("me conta tudo", "pt")
        ticket = self.boss[0].split("]")[0].split()[-1]
        self.step(2)
        self.call.say_later(ticket, "Uma resposta bem longa...")
        self.call.pump()
        self.assertTrue(self.call.speaking)
        self.call.user_started()
        self.assertEqual(self.cleared, [1])
        self.assertFalse(self.call.speaking)
        self.call.user_ended("para, outra coisa", "pt")
        self.assertIn("cut off your last reply", self.boss[-1])

    def test_a_slow_boss_gets_one_sec_then_still_on_it_then_an_offer_to_text_once_each(self):
        self.step(1)
        self.call.user_ended("faz o deploy", "pt")
        heard = [what for kind, what, _ in self.step(200) if kind == "clip"]
        self.assertEqual(heard, ["ack", "one_sec", "still_on_it", "offer_text"])

    def test_no_nudges_once_the_boss_has_answered(self):
        self.step(1)
        self.call.user_ended("oi", "pt")
        ticket = self.boss[0].split("]")[0].split()[-1]
        self.call.say_later(ticket, "Oi, Daniel.")
        heard = self.step(200)
        self.assertEqual(heard, [("text", "Oi, Daniel.", "pt")])  # the answer replaces the "Hm", no nudges

    def test_silence_is_not_a_turn(self):
        self.step(1)
        self.call.user_started()
        self.call.user_ended("   ", "pt")
        self.assertEqual(self.boss, [])
        self.assertEqual(self.step(200), [])


class SayAndLatencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vc = load({"VOICE_CALL_STATE_DIR": self.tmp.name, "VOICE_CALL_PORT": "1"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_say_falls_back_to_imessage_when_no_call_is_live(self):
        texts = []
        self.vc.text_owner = lambda t: texts.append(t) or True
        with mock.patch("sys.stdout", io.StringIO()):
            self.vc.cmd_say("t1", "done")  # nothing listens on port 1
        self.assertEqual(texts, ["done"])

    def test_latency_measures_the_reply_gap(self):
        rate, path = 8000, os.path.join(self.tmp.name, "rec.wav")
        # caller talks 0-1s and 3-4s; agent answers 0.8s and 1.2s after each
        spans = {0: [(0, 1.0), (3.0, 4.0)], 1: [(1.8, 2.5), (5.2, 6.0)]}
        pcm = array.array("h", [0] * (rate * 7 * 2))
        for ch, ranges in spans.items():
            for a, b in ranges:
                for i in range(int(a * rate), int(b * rate)):
                    pcm[2 * i + ch] = 8000 if (i // 10) % 2 else -8000
        with wave.open(path, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.tobytes())
        gaps = self.vc.latency(path)
        self.assertEqual(len(gaps), 2)
        self.assertAlmostEqual(gaps[0], 0.8, delta=0.04)
        self.assertAlmostEqual(gaps[1], 1.2, delta=0.04)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerTest(unittest.TestCase):
    """The real handler on real sockets: Twilio's port wants our secret, `say` answers only on its own port."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        port = free_port()
        self.vc = load(dict(ENV, VOICE_CALL_STATE_DIR=self.tmp.name, VOICE_CALL_PORT=str(port)))
        self.vc.log = lambda m: None
        self.vc.save_config(dict(CFG))
        self.servers = [self.vc.ThreadingHTTPServer(("127.0.0.1", p), self.vc.Handler) for p in (port, port + 2)]
        for srv in self.servers:
            self.vc.threading.Thread(target=srv.serve_forever, daemon=True).start()

    def tearDown(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()
        self.tmp.cleanup()

    def post(self, port, path, data=b"", headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, ""

    def test_twilio_port_challenges_then_answers_with_our_secret(self):
        body = f"CallSid=CA1&From={OWNER.replace('+', '%2B')}".encode()
        code, headers, _ = self.post(self.vc.PORT, "/voice/twilio", body)
        self.assertEqual(code, 401)  # Twilio sends the URL's credentials after this challenge
        self.assertIn("Basic", headers["WWW-Authenticate"])
        auth = "Basic " + __import__("base64").b64encode(b"twilio:s3cret").decode()
        code, _, out = self.post(self.vc.PORT, "/voice/twilio", body, {"Authorization": auth})
        self.assertEqual(code, 200)
        self.assertIn("<Gather", out)

    def test_say_is_never_on_the_twilio_port(self):
        req = json.dumps({"ticket": "t1", "text": "hi"}).encode()
        self.assertEqual(self.post(self.vc.PORT, "/voice/say", req, {"X-Say-Token": "say"})[0], 404)
        self.assertEqual(self.post(self.vc.SAY_PORT, "/voice/say", req, {"X-Say-Token": "say"})[0], 200)
        self.assertEqual(self.post(self.vc.SAY_PORT, "/voice/say", req, {"X-Say-Token": "guess"})[0], 404)
        self.assertEqual(self.post(self.vc.SAY_PORT, "/voice/twilio", b"", {})[0], 404)

    def test_a_late_answer_to_a_synthetic_call_never_texts_him(self):
        texts = []
        self.vc.text_owner = lambda t: texts.append(t) or True
        for ticket, test in (("t1", True), ("t2", False)):
            call = self.vc.Call(play=None, clear=None, boss=lambda m: None, test=test)
            call.live = False
            self.vc.TICKETS[ticket] = call
            with mock.patch("sys.stdout", io.StringIO()):
                self.vc.cmd_say(ticket, "late")
        self.assertEqual(texts, ["late"])  # only his own call's answer goes on by iMessage
        self.assertIn("SYNTHETIC TEST CALL", self.vc.envelope("t1", "pt", "oi", False, test=True))
        self.assertNotIn("SYNTHETIC", self.vc.envelope("t2", "pt", "oi", False))


class FluxTurnsTest(unittest.TestCase):
    def test_start_barges_in_and_end_of_turn_carries_text_and_language(self):
        vc = load(ENV)
        stream = vc.Stream(ws=None, cfg=CFG)
        stream.call = mock.Mock(live=True)
        msgs = [{"type": "Connected"}, {"type": "TurnInfo", "event": "Update", "transcript": "e o"},
                {"type": "TurnInfo", "event": "StartOfTurn", "transcript": "e o"},
                {"type": "TurnInfo", "event": "EndOfTurn", "transcript": "e o deploy?", "languages": ["pt"]},
                {"type": "TurnInfo", "event": "TurnResumed"},
                {"type": "TurnInfo", "event": "EndOfTurn", "transcript": "ok", "languages": []}]
        stream.listen([json.dumps(m) for m in msgs])
        self.assertEqual(stream.call.user_started.call_count, 2)
        self.assertEqual(stream.call.user_ended.call_args_list, [mock.call("e o deploy?", "pt"), mock.call("ok", "")])


if __name__ == "__main__":
    unittest.main()
