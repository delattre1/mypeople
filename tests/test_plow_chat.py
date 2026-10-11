"""Plow Chat plugin (card f864c568e6): the owner's iMessages reach the Boss exactly once.

The bridge polls, so what must hold: turning it on never replays history at the Boss, each
new message is delivered once, and a failed delivery is retried instead of skipped.
"""
import importlib.machinery
import importlib.util
from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

PLUGINS = Path(__file__).resolve().parents[1] / "mypeople" / "runtime" / "plugins"


def load(name, rel, env):
    with mock.patch.dict(os.environ, env):
        loader = importlib.machinery.SourceFileLoader(name, str(PLUGINS / rel))
        spec = importlib.util.spec_from_loader(name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    return mod


class PlowChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pc = load("plow_chat", "plow-chat/plow-chat.py",
                       {"PLOW_CHAT_STATE_DIR": self.tmp.name, "HOST_ID": "h"})
        self.msgs = [{"uid": "m1", "direction": "inbound", "body": "old", "chat_uid": "cht_a",
                      "created_at": "1"}]
        self.pc.api = lambda method, path, body=None, token=None: (200, {"data": self.msgs})
        self.sent = []
        self.pc.send_to_boss = lambda m: self.sent.append(m) or True
        self.creds = {"token": "t", "chat_uid": "cht_a"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_seeds_then_routes_once(self):
        self.assertEqual(self.pc.BOSS_AGENT, "h/main:Boss")
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertEqual(self.sent, [], "history must not replay at the Boss")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "hi boss",
                          "chat_uid": "cht_a", "created_at": "2",
                          "sender": {"display_name": "Dan"}})
        self.msgs.append({"uid": "m3", "direction": "outbound", "body": "reply",
                          "chat_uid": "cht_a", "created_at": "3"})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("[plowchat] from Dan in cht_a: hi boss", self.sent[0])
        self.assertIn("reply cht_a", self.sent[0])
        # Plain text: the mark-it-seen line, the message, the instructions. No reply, mention or
        # tapback lines.
        lines = self.sent[0].split("\n")
        self.assertIn("react cht_a m2 👀", lines[0])
        self.assertTrue(lines[2].startswith("(This is the owner's"))
        self.assertEqual(len(lines), 3)

    def test_a_reply_carries_what_it_answers_and_who_it_mentions(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({
            "uid": "msg_2", "direction": "inbound", "body": "yes that one", "chat_uid": "cht_a",
            "created_at": "2", "sender": {"type": "member", "display_name": "Dan"},
            "mentions": [{"handle": "+15550001", "is_me": True}, {"handle": "+15550002", "is_me": False}],
            "reply_to": {"part_index": 0, "message": {
                "uid": "msg_1", "body": "Deploy\n now?", "sender": {"type": "agent", "relationship": "self"}}}})
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertIn('\n(in reply to your message msg_1: "Deploy now?")', self.sent[0])
        self.assertIn("\n(mentions: you, +15550002)", self.sent[0])
        self.assertIn("react cht_a msg_2 like", self.sent[0])

    def test_a_tapback_never_wakes_the_boss_and_rides_with_the_next_text_once(self):
        dan = {"type": "member", "display_name": "Dan"}
        self.msgs[0]["reactions"] = [{"uid": "rx_old", "type": "like", "actor": dan}]
        self.pc.poll_chat(self.creds, "cht_a")  # seeding: the old tapback is history
        self.msgs.append({"uid": "msg_b", "direction": "outbound", "body": "Deploy now?",
                          "chat_uid": "cht_a", "created_at": "2",
                          "sender": {"type": "agent", "relationship": "self"},
                          "reactions": [{"uid": "rx_1", "type": "love", "actor": dan},
                                        {"uid": "rx_2", "type": "like",
                                         "actor": {"type": "agent", "relationship": "self"}}]})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertEqual(self.sent, [], "a tapback is not a turn")
        self.msgs.append({"uid": "msg_c", "direction": "inbound", "body": "and?",
                          "chat_uid": "cht_a", "created_at": "3", "sender": dan})
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertIn('\n(earlier tapback in cht_a, separate from the new text above: Dan loved your message '
                      'msg_b: "Deploy now?")', self.sent[0])
        # The tapback rides with a real text, so it must never read as a verdict on it (10-06: "8*5" was skipped).
        self.assertTrue(self.sent[0].split("\n")[1].startswith("[plowchat] from Dan in cht_a: and?"))
        self.assertNotIn("not a new message", self.sent[0])
        self.assertNotIn("liked", self.sent[0], "history and the Boss's own tapback stay out")
        self.msgs.append({"uid": "msg_d", "direction": "inbound", "body": "?",
                          "chat_uid": "cht_a", "created_at": "4", "sender": dan})
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertNotIn("earlier tapback", self.sent[1])

    def test_the_agent_is_told_to_mark_his_message_seen_before_anything_else(self):
        """The owner wanted feedback the moment a text is seen, not after the ~30s turn: the line
        right under his message tapbacks HIS message (its uid, not ours) with the eyes."""
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "msg_his", "direction": "inbound", "body": "deploy it?",
                          "chat_uid": "cht_a", "created_at": "2", "sender": {"display_name": "Dan"}})
        self.pc.poll_chat(self.creds, "cht_a")
        # The very first line, so it is the first command an agent runs, before it starts work.
        first, second = self.sent[0].split("\n")[:2]
        self.assertTrue(first.startswith("FIRST, before reading on: python3 "))
        self.assertIn(" react cht_a msg_his 👀", first)
        self.assertEqual("[plowchat] from Dan in cht_a: deploy it?", second)

    def test_react_sends_a_tapback_and_refuses_a_misspelt_one(self):
        calls = []
        self.pc.api = lambda method, path, body=None, token=None: calls.append((path, body)) or (200, {})
        for kind in ("love", "👀", "remove"):
            self.pc.react("cht_a", "msg_1", kind, self.creds)
        with self.assertRaises(SystemExit):
            self.pc.react("cht_a", "msg_1", "thumbsup", self.creds)
        self.assertEqual(calls, [("/v1/chats/cht_a/messages/msg_1/reactions", b) for b in (
            {"operation": "add", "type": "love"},
            {"operation": "add", "type": "custom", "custom_emoji": "👀"},
            {"operation": "remove"})])

    def test_help_and_unknown_options_never_reach_the_chat(self):
        """Two engineers' `reply --help` landed in the owner's chat as the text '--help' (10-05).
        Help prints usage and sends nothing; any other leading option is refused, on every path."""
        sent = []
        self.pc.send_message = lambda text, chat_uid="", creds=None, files=(): sent.append(text) or {}
        self.pc.react = lambda *a, **k: sent.append(a) or {}
        self.pc.bridge_loop = lambda *a: sent.append("second bridge started")
        out = []
        def run(*argv):
            with mock.patch.object(self.pc.sys, "argv", ["plow-chat.py", *argv]), \
                    mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
                try:
                    self.pc.main()
                    return "ok"
                except SystemExit as e:
                    return str(e)
        for argv in (["reply", "--help"], ["reply", "-h"], ["reply", "cht_a", "--help"],
                     ["reply", "cht_a", "--file", "a.png", "--help"], ["react", "--help"],
                     ["react", "cht_a", "msg_1", "-h"], ["serve", "--help"], ["--help"]):
            self.assertEqual("ok", run(*argv), argv)
        self.assertTrue(out and all("Usage:" in o for o in out))
        for argv in (["reply", "--verbose", "hi"], ["reply", "cht_a", "--dry-run", "hi"],
                     ["reply", "cht_a", "--file"], ["react", "cht_a", "msg_1", "--like"]):
            self.assertIn("nothing sent", run(*argv), argv)
        self.assertEqual([], sent, "no help or unknown option may send anything")
        self.assertEqual("ok", run("reply", "cht_a", "- a list line"))  # a dash-led message is text
        self.assertEqual(["- a list line"], sent)

    def test_chat_that_appears_while_running_is_routed_not_swallowed(self):
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a", seed=False), 1)
        self.assertIn("[plowchat]", self.sent[0])

    def test_photo_without_text_reaches_the_boss_with_its_path(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "", "chat_uid": "cht_a",
                          "created_at": "2", "attachments": [
                              {"uid": "att_1", "filename": "menu.jpeg", "url": "/v1/x?exp=1"}]})
        with mock.patch.object(self.pc, "download", return_value="/saved/att_1-menu.jpeg") as dl:
            self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)
        dl.assert_called_once()
        self.assertIn("[attached, open it: /saved/att_1-menu.jpeg]", self.sent[0])

    def test_reply_uploads_files_then_sends_their_uids(self):
        calls = []

        def api(method, path, body=None, token=None):
            calls.append((method, path, body))
            if path.endswith("/attachments"):
                return 201, {"uid": "att_out", "upload_url": "https://up/x", "upload_headers": {"h": "v"}}
            return 200, {"uid": "msg"}
        self.pc.api = api
        with tempfile.NamedTemporaryFile(suffix=".png") as f, \
                mock.patch.object(self.pc.urllib.request, "urlopen") as put:
            f.write(b"png")
            f.flush()
            self.pc.send_message("look", "cht_a", self.creds, files=[f.name])
        self.assertEqual(calls[0][2]["content_type"], "image/png")
        self.assertEqual(calls[0][2]["size_bytes"], 3)
        self.assertEqual(put.call_args[0][0].get_method(), "PUT")
        self.assertEqual(calls[1], ("POST", "/v1/chats/cht_a/messages",
                                    {"body": "look", "attachment_uids": ["att_out"]}))

    def test_failed_delivery_is_retried(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "hi",
                          "chat_uid": "cht_a", "created_at": "2"})
        self.pc.send_to_boss = lambda m: False
        self.pc.STARTED -= 300  # a bridge that has been up a while
        with mock.patch.object(self.pc, "send_message") as warn:
            self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        warn.assert_called_once()  # the sender hears it did not land
        self.pc.send_to_boss = lambda m: self.sent.append(m) or True
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)

    def test_a_text_that_lands_while_the_boss_comes_back_is_not_warned_about(self):
        """An in-place update restarts the bridge before the Boss: the text waits, nobody is told."""
        self.pc.poll_chat(self.creds, "cht_a")
        now = self.pc.datetime.now(self.pc.timezone.utc)
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "codename?",
                          "chat_uid": "cht_a", "created_at": now.isoformat()})
        self.pc.send_to_boss = lambda m: False
        with mock.patch.object(self.pc, "send_message") as warn:
            self.pc.poll_chat(self.creds, "cht_a")
            self.pc.poll_chat(self.creds, "cht_a")
            warn.assert_not_called()
            self.msgs[-1]["created_at"] = (now - timedelta(seconds=300)).isoformat()
            self.pc.poll_chat(self.creds, "cht_a")
            warn.assert_not_called()  # an overdue text, but the bridge itself just came back up
            self.pc.STARTED -= 300
            self.pc.poll_chat(self.creds, "cht_a")
            self.pc.poll_chat(self.creds, "cht_a")
        warn.assert_called_once()  # overdue: told once, not on every pass
        self.pc.send_to_boss = lambda m: self.sent.append(m) or True
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)



class CloudTest(unittest.TestCase):
    """A 1-click Plow VM: no saved login, PLOW_API_BASE + a proxied token."""

    def test_cloud_agent_reads_chats_from_me_and_says_hello_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            pc = load("plow_chat_cloud", "plow-chat/plow-chat.py",
                      {"PLOW_CHAT_STATE_DIR": tmp, "HOST_ID": "h",
                       "PLOW_API_BASE": "https://plow-x.int.exe.xyz/"})
            with mock.patch.dict(os.environ, {"PLOW_API_BASE": "https://plow-x.int.exe.xyz/"}):
                os.environ.pop("PLOW_AGENT_TOKEN", None)
                creds = pc.load_creds()
            self.assertEqual(pc.BASE, "https://plow-x.int.exe.xyz")
            self.assertEqual(creds["token"], "proxied")
            calls = []

            def api(method, path, body=None, token=None):
                calls.append((method, path, body, token))
                if path == "/v1/agents/cloud/me":
                    return 200, {"chats": [{"uid": "cht_c"}]}
                return 200, {"data": [{"uid": "m1", "direction": "inbound",
                                       "body": "Set this up for me", "chat_uid": "cht_c"}]}
            pc.api = api
            self.assertEqual(pc.list_chats(creds), ["cht_c"])
            pc.poll_chat(creds, "cht_c")
            pc.poll_chat(creds, "cht_c")
            hellos = [c for c in calls if c[0] == "POST"]
            self.assertEqual(hellos, [("POST", "/v1/chats/cht_c/messages",
                                       {"body": pc.WELCOME}, "proxied")])


class WiringTest(unittest.TestCase):
    def test_supervise_starts_it_only_when_turned_on_and_down_stops_it(self):
        root = PLUGINS.parents[2]
        sup = (root / "mypeople" / "runtime" / "bin" / "supervise.sh").read_text()
        self.assertIn('[ -n "${PLOW_CHAT:-}" ] && ensure "$ID/plugins/plow-chat/plow-chat.py"', sup)
        self.assertIn('os.path.join(install, "plugins") + os.sep', (root / "mypeople" / "cli.py").read_text())


if __name__ == "__main__":
    unittest.main()
