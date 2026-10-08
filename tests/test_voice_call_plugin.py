"""Voice Call plugin (card 79584b97bd): the owner phones his Boss.

What must hold: only the owner with the PIN gets a voice stream, a stranger leaves no trace, and
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
import tempfile
import unittest
from unittest import mock
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
CFG = {"pin": "482913", "twilio_number": "+16505550100"}
ENV = {"VOICE_CALL_ALLOW": OWNER, "VOICE_CALL_PUBLIC_URL": "https://h.example"}


def sync_threads(vc):
    vc.threading.Thread = lambda target, args=(), daemon=None: mock.Mock(start=lambda: target(*args))


class TwilioSignatureTest(unittest.TestCase):
    def test_official_vector(self):
        # twilio-python tests/unit/test_request_validator.py
        vc = load(ENV)
        params = {"CallSid": "CA1234567890ABCDE", "Digits": "1234", "From": "+14158675309",
                  "To": "+18005551212", "Caller": "+14158675309"}
        self.assertEqual(vc.twilio_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params),
                         "RSOYDt4T1cUTdK1PDd93/VVr8B8=")


class WhoMayCallTest(unittest.TestCase):
    def setUp(self):
        self.vc = load(ENV)
        self.logs, self.texts = [], []
        self.vc.log = self.logs.append
        self.vc.text_owner = self.texts.append
        sync_threads(self.vc)

    def test_stranger_hears_one_sentence_and_leaves_no_trace(self):
        out = self.vc.on_twilio("/voice/twilio", {"CallSid": "CA1", "From": "+14155550000"}, CFG)
        self.assertIn("This line takes texts only", out)
        self.assertIn("<Hangup/>", out)
        self.assertNotIn("<Connect", out)
        self.assertEqual(self.logs, ["rejected a call"])  # no number, nothing else
        deleted = []
        self.vc.twilio = lambda cfg, method, path, form=None: deleted.append((method, path)) or (204, {})
        self.vc.on_twilio("/voice/twilio/status", {"CallSid": "CA1", "CallStatus": "completed"}, CFG)
        self.assertEqual(deleted, [("DELETE", "/Calls/CA1.json")])

    def test_owner_needs_the_pin(self):
        out = self.vc.on_twilio("/voice/twilio", {"CallSid": "CA2", "From": OWNER}, CFG)
        self.assertIn('numDigits="6"', out)
        self.assertIn('action="https://h.example/voice/twilio/pin"', out)
        self.assertNotIn("<Connect", out)
        for _ in (1, 2):
            out = self.vc.on_twilio("/voice/twilio/pin", {"CallSid": "CA2", "From": OWNER, "Digits": "111111"}, CFG)
            self.assertIn("<Gather", out)
        out = self.vc.on_twilio("/voice/twilio/pin", {"CallSid": "CA2", "From": OWNER, "Digits": ""}, CFG)
        self.assertEqual(out.count("<Hangup/>"), 1)
        self.assertNotIn("<Connect", out)
        self.assertEqual(len(self.texts), 1)  # he is told someone tried

    def test_right_pin_opens_a_stream_only_its_one_time_token_can_join(self):
        out = self.vc.on_twilio("/voice/twilio/pin", {"CallSid": "CA3", "From": OWNER, "Digits": "482913"}, CFG)
        self.assertIn('<Stream url="wss://h.example/voice/stream">', out)
        token = out.split('name="token" value="')[1].split('"')[0]
        self.assertIsNone(self.vc.take_grant("guess"))
        grant = self.vc.take_grant(token)
        self.assertEqual(grant[1:], ("CA3", False))  # his call: never recorded
        self.assertIsNone(self.vc.take_grant(token))  # once only

    def test_token_expires_after_a_minute(self):
        out = self.vc.on_twilio("/voice/twilio/pin", {"CallSid": "CA4", "From": OWNER, "Digits": "482913"}, CFG)
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
        pin = {"Digits": CFG["pin"]}
        with mock.patch.object(self.vc.time, "time", return_value=999):
            ours = self.vc.on_twilio("/voice/twilio/pin", dict(pin, CallSid="C4", From=self.ours), cfg)
            his = self.vc.on_twilio("/voice/twilio/pin", dict(pin, CallSid="C5", From=OWNER), cfg)
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
        sync_threads(self.vc)
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

    def test_keys_page_saves_only_well_formed_keys_and_never_shows_them(self):
        sid, tok = "AC" + "a" * 32, "b" * 32
        saved, bad = self.vc.save_keys({"twilio_sid": sid, "twilio_token": "not-a-token", "tts_key": "sk_" + "c" * 40})
        self.assertEqual(saved, ["Twilio Account SID", "ElevenLabs API key"])
        self.assertEqual(bad, ["Twilio Auth Token"])
        self.vc.save_keys({"twilio_token": tok})
        cfg = self.vc.load_config()
        self.assertEqual((cfg["twilio_sid"], cfg["twilio_token"]), (sid, tok))
        page = self.vc.keys_page()
        self.assertNotIn(sid, page)
        self.assertNotIn(tok, page)
        self.assertEqual(page.count("saved"), 3)
        self.assertEqual(oct(self.vc.CONFIG.stat().st_mode & 0o777), "0o600")

    def test_keys_page_answers_only_his_tailnet_login(self):
        vc = load({"VOICE_CALL_STATE_DIR": self.tmp.name, "VOICE_CALL_KEYS_LOGINS": "daniel@example.com"})
        ok = lambda headers, path="/voice/keys/tok", now=100: vc.keys_request_ok(path, headers, "tok", 200, now)
        self.assertTrue(ok({"Tailscale-User-Login": "daniel@example.com"}))
        self.assertFalse(ok({}))  # the public internet carries no tailnet identity
        self.assertFalse(ok({"Tailscale-User-Login": "someone@example.org"}))
        self.assertFalse(ok({"Tailscale-User-Login": "daniel@example.com"}, path="/voice/keys/guess"))
        self.assertFalse(ok({"Tailscale-User-Login": "daniel@example.com"}, now=300))  # expired
        self.assertNotEqual(vc.KEYS_PORT, vc.PORT)  # never the funneled port
        bad = json.dumps({"AllowFunnel": {"h:443": True}, "Web": {"h:443": {"Handlers": {
            "/voice": {"Proxy": "http://127.0.0.1:8796/voice"}, "/": {"Proxy": f"http://127.0.0.1:{vc.KEYS_PORT}"}}}}})
        good = json.dumps({"AllowFunnel": {"h:443": True}, "Web": {
            "h:443": {"Handlers": {"/voice": {"Proxy": "http://127.0.0.1:8796/voice"}}},
            "h:8799": {"Handlers": {"/voice/keys": {"Proxy": f"http://127.0.0.1:{vc.KEYS_PORT}/voice/keys"}}}}})
        self.assertTrue(vc.funneled(vc.KEYS_PORT, bad))
        self.assertFalse(vc.funneled(vc.KEYS_PORT, good))
        closed = load({"VOICE_CALL_STATE_DIR": self.tmp.name})
        with self.assertRaises(SystemExit):
            closed.cmd_keys_page()  # no allowed login: it refuses to start at all

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


if __name__ == "__main__":
    unittest.main()
