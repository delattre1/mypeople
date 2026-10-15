"""Plow Chat plugin (card f864c568e6): the owner's iMessages reach the Boss exactly once.

What must hold: turning it on never replays history at the Boss, each new message is
delivered once, and a failed delivery is retried instead of skipped. The bridge holds Plow's
websocket (card 5b125624f8), so the same holds across a dropped socket.
"""
import base64
import hashlib
import importlib.machinery
import importlib.util
from datetime import timedelta
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
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
        self.assertTrue(lines[2].startswith("(You hold this line's Plow login"))
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
        self.assertTrue(first.startswith("FIRST, before reading on: PLOW_CHAT_STATE_DIR="))
        self.assertIn(" react cht_a msg_his 👀", first)
        self.assertEqual("[plowchat] from Dan in cht_a: deploy it?", second)

    def test_a_second_bridge_hands_the_boss_commands_for_its_own_account(self):
        """A second Plow account (the owner's WhatsApp number as its own identity) runs its own
        bridge with its own state dir. The reply it hands the Boss must carry that dir, or the
        reply goes out under the first account's login and never reaches the chat."""
        told = self.pc.envelope("Dan", "hi", "cht_a", mid="m9")
        self.assertIn(f"PLOW_CHAT_STATE_DIR={shlex.quote(self.tmp.name)} python3 {self.pc.SELF} reply cht_a", told)
        with mock.patch.dict(os.environ):
            os.environ.pop("PLOW_CHAT_STATE_DIR", None)
            first = load("plow_chat_first", "plow-chat/plow-chat.py", {"HOST_ID": "h"})
        self.assertIn(f"run: python3 {first.SELF} reply cht_a", first.envelope("Dan", "hi", "cht_a"))
        # A bridge on another server (the WhatsApp QR number) must hand over that server too.
        other = load("plow_chat_other", "plow-chat/plow-chat.py",
                     {"PLOW_CHAT_STATE_DIR": self.tmp.name, "PLOW_CHAT_BASE_URL": "http://127.0.0.1:18790", "HOST_ID": "h"})
        self.assertIn(f"PLOW_CHAT_STATE_DIR={shlex.quote(self.tmp.name)} PLOW_CHAT_BASE_URL=http://127.0.0.1:18790 "
                      f"python3 {other.SELF} reply cht_a", other.envelope("Dan", "hi", "cht_a"))

    def test_every_text_tells_the_agent_it_holds_the_whole_plow_api(self):
        """Willow on Mac 2 told the owner "I can't create threads" (10-15): the instructions named
        only reply and react. They must say the agent holds this line's login and the whole API."""
        told = self.pc.envelope("Dan", "start a group with Mary", "cht_a", mid="m9")
        self.assertIn("https://api.plow.co/openapi.json", told)
        self.assertIn(f"{self.pc.RUN} api POST /v1/chats", told)
        self.assertIn("never answer that you can't", told)
        self.assertLess(told.index("whole Plow API"), told.index(" reply cht_a"))   # said first

    def test_api_makes_any_call_with_this_lines_login(self):
        calls = []
        self.pc.load_creds = lambda: {"token": "t"}
        self.pc.api = lambda method, path, body=None, token=None: (
            calls.append((method, path, body, token)) or (201, {"uid": "cht_new"}))
        with mock.patch.object(sys, "argv", ["plow-chat.py", "api", "post", "/v1/chats",
                                              '{"members": ["+15550100"]}']), \
                mock.patch("builtins.print") as out:
            self.pc.main()
        self.assertEqual(calls, [("POST", "/v1/chats", {"members": ["+15550100"]}, "t")])
        self.assertIn("cht_new", out.call_args[0][0])
        self.pc.api = lambda *a, **k: (403, {"error": "no"})
        for argv in (["api", "GET", "/v1/lines"], ["api", "GET", "lines"]):   # refused / not a /v1 path
            with mock.patch.object(sys, "argv", ["plow-chat.py"] + argv), mock.patch("builtins.print"), \
                    self.assertRaises(SystemExit):
                self.pc.main()

    def test_a_mac_on_a_plow_line_gets_the_owners_latch_for_every_agent(self):
        """Willow on Mac 2 could not look up a contact on the owner's Mac (10-15): only cloud
        MyPlow registered Plow Latch. The line's credential reaches the relay URL Plow names for it."""
        home = tempfile.mkdtemp()
        cj = Path(home) / ".claude.json"
        cj.write_text(json.dumps({"projects": {"/x": {}}}))
        creds = {"token": "tok", "cloud": True}
        url = "https://api.plow.co/v1/relay/devices/u1/mcp"
        self.pc.api = lambda method, path, body=None, token=None: (200, {"mcp_url": url})
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(self.pc.connect_latch(creds), "connected")
            cfg = json.loads(cj.read_text())
            self.assertEqual(cfg["mcpServers"]["plow-latch"], {
                "type": "http", "url": url, "headers": {"Authorization": "Bearer tok"}})
            self.assertEqual(cfg["projects"], {"/x": {}})                 # the rest is kept
            self.assertEqual(oct(cj.stat().st_mode & 0o777), "0o600")     # it holds a credential
            self.assertEqual(self.pc.connect_latch(creds), "already connected")
            # The Mac that runs Latch already reaches it through its own connector: no second one.
            cj.write_text(json.dumps({"mcpServers": {"mine": {"type": "http", "url": url}}}))
            self.assertTrue(self.pc.connect_latch(creds).startswith("skip"))
            self.assertNotIn("plow-latch", json.loads(cj.read_text())["mcpServers"])
            # No Latch for this line, or not a line credential: nothing is written.
            self.pc.api = lambda *a, **k: (200, {"mcp_url": None})
            cj.write_text("{}")
            self.assertTrue(self.pc.connect_latch(creds).startswith("skip"))
            self.assertTrue(self.pc.connect_latch({"token": "acct", "chat_uid": "cht_a"}).startswith("skip"))
            self.assertEqual(json.loads(cj.read_text()), {})

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

    def test_a_send_first_chats_first_reply_reaches_the_boss(self):
        """A chat we started (Plow's send-first POST /v1/chats) first shows only our own send.
        It got no mark, so the next pass took it for a chat from before marks and swallowed
        the reply (whatsapp lab line 2, 10-14)."""
        self.msgs[:] = [{"uid": "m0", "direction": "outbound", "body": "hello", "chat_uid": "cht_a",
                         "created_at": "1"}]
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a", seed=False), 0)
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "the reply", "chat_uid": "cht_a",
                          "created_at": "2"})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a", seed=False), 1)
        self.assertIn("the reply", self.sent[0])

    def test_a_chat_empty_at_startup_routes_its_first_text(self):
        self.msgs[:] = []
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "first text", "chat_uid": "cht_a",
                          "created_at": "2"})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a", seed=False), 1)

    def test_photo_without_text_reaches_the_boss_with_its_path(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "", "chat_uid": "cht_a",
                          "created_at": "2", "attachments": [
                              {"uid": "att_1", "filename": "menu.jpeg", "url": "/v1/x?exp=1"}]})
        with mock.patch.object(self.pc, "download", return_value="/saved/att_1-menu.jpeg") as dl:
            self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)
        dl.assert_called_once()
        self.assertIn("[attached, open it: /saved/att_1-menu.jpeg]", self.sent[0])

    def test_a_failed_file_becomes_a_note_and_its_text_still_arrives(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "see the video", "chat_uid": "cht_a",
                          "created_at": "2", "attachments": [
                              {"uid": "att_1", "filename": "trip.mov", "url": None, "status": "failed"}]})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)
        self.assertIn("see the video", self.sent[0])
        self.assertIn("\n(could not download trip.mov: file unavailable)", self.sent[0])
        self.assertNotIn("open it", self.sent[0], "nothing to open: the file never arrived")

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

    def _two_chats(self, quiet, busy):
        self.pc.api = lambda method, path, body=None, token=None: (
            200, {"data": quiet if "/cht_a/" in path else busy})

    def test_a_quiet_chats_old_messages_never_route_again(self):
        """#40: 1000+ messages elsewhere pushed a quiet chat's ids out of one shared window, and
        its unchanged page was routed to the Boss again as new."""
        busy = [{"uid": "b%d" % i, "direction": "inbound", "body": "x", "chat_uid": "cht_b",
                 "created_at": "2026-10-14T10:00:%02d.%06dZ" % (i // 1000000, i)} for i in range(1001)]
        self._two_chats(self.msgs, busy)
        self.pc.poll_chat(self.creds, "cht_a")             # seeded: m1 is history
        self.pc.poll_chat(self.creds, "cht_b", seed=False)  # a busy chat routes 1001 messages
        self.sent.clear()
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)
        self.assertEqual([], self.sent)

    def test_upgrading_from_the_id_window_neither_replays_nor_drops(self):
        old = {"uid": "m0", "direction": "inbound", "body": "routed long ago", "chat_uid": "cht_a",
               "created_at": "0"}
        new = {"uid": "m2", "direction": "inbound", "body": "landed during the restart",
               "chat_uid": "cht_a", "created_at": "2"}
        self.msgs[:] = [old, self.msgs[0], new]
        self.pc.write_json(self.pc.STATE, {"seen_ids": ["m1"], "seeded_chats": ["cht_a"]})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 1)   # m0 aged out: not replayed
        self.assertIn("landed during the restart", self.sent[0])
        self.pc.write_json(self.pc.STATE, {"seen_ids": ["gone"], "seeded_chats": ["cht_a"]})
        self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), 0)   # all aged out: nothing

    def test_a_quiet_chats_old_tapbacks_never_come_back(self):
        like = {"uid": "r1", "type": "like", "actor": {"type": "member", "name": "Sam"}}
        self.msgs[0].update(direction="outbound", reactions=[like])  # Sam liked the Boss's text
        busy = [{"uid": "b%d" % i, "direction": "outbound", "body": "x", "chat_uid": "cht_b",
                 "created_at": "1", "reactions": [dict(like, uid="rb%d" % i)]} for i in range(1001)]
        self._two_chats(self.msgs, busy)
        self.pc.poll_chat(self.creds, "cht_a")
        self.pc.poll_chat(self.creds, "cht_b")
        st = self.pc.read_json(self.pc.STATE, {})
        st["pending_tapbacks"] = []
        self.pc.write_json(self.pc.STATE, st)
        self.pc.poll_chat(self.creds, "cht_a")
        self.assertEqual([], self.pc.read_json(self.pc.STATE, {}).get("pending_tapbacks"))

    def test_times_from_different_servers_order_by_instant(self):
        o = lambda ts: self.pc.order({"created_at": ts, "uid": "u"})
        self.assertLess(o("2026-10-14T13:09:05Z"), o("2026-10-14T13:09:05.056Z"))
        self.assertLess(o("2026-10-14T13:09:05.056Z"), o("2026-10-14T13:09:05.056001+00:00"))

    def test_failed_delivery_is_retried(self):
        self.pc.poll_chat(self.creds, "cht_a")
        self.msgs.append({"uid": "m2", "direction": "inbound", "body": "hi",
                          "chat_uid": "cht_a", "created_at": "2"})
        self.pc.send_to_boss = lambda m: False
        self.pc.STARTED -= 300  # a bridge that has been up a while
        with mock.patch.object(self.pc, "send_message") as warn:
            self.assertEqual(self.pc.poll_chat(self.creds, "cht_a"), -1)  # still waiting
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


    def test_the_socket_routes_at_once_and_a_reconnect_drops_and_repeats_nothing(self):
        """A ping is answered and a frame routes its chat at once; a text sent while the socket was
        down arrives by the catch-up on reconnect; a fragmented frame is joined; nothing twice."""
        srv = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(srv.close)
        self.pc.BASE = f"http://127.0.0.1:{srv.getsockname()[1]}"
        self.pc.api = lambda m, p, body=None, token=None: (
            (201, {"ticket": "t"}) if p == "/v1/ws/ticket"
            else (200, {"data": [{"uid": "cht_a"}]}) if p == "/v1/chats" else (200, {"data": self.msgs}))
        self.pc.poll_chat(self.creds, "cht_a")  # m1 is history
        msg = lambda uid: {"uid": uid, "direction": "inbound", "body": f"text {uid}",
                           "chat_uid": "cht_a", "created_at": uid}
        frame = lambda op, data, fin=0x80: bytes([fin | op]) + (  # an event needs the 16-bit length
            bytes([len(data)]) if len(data) < 126 else bytes([126]) + len(data).to_bytes(2, "big")) + data
        event = lambda uid: json.dumps({"event_type": "message_received", "chat_id": "cht_a",
                                        "data": {"message": msg(uid)}}).encode()
        pong, caught_up = [], []

        def accept():
            c, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            key = re.search(rb"Sec-WebSocket-Key: (\S+)", req).group(1)
            ok = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade"
                      b"\r\nSec-WebSocket-Accept: " + ok + b"\r\n\r\n" + frame(0x1, b'{"type": "connected"}'))
            return c

        def server():
            c = accept()
            c.sendall(frame(0x9, b"hb"))
            pong.append(c.recv(64))       # answered: the bridge is past its catch-up, listening
            self.msgs.append(msg("m2"))
            c.sendall(frame(0x1, event("m2")))
            time.sleep(0.3)
            c.close()                     # the socket drops,
            self.msgs.append(msg("m3"))   # a text lands meanwhile and its frame is never sent;
            c = accept()
            for _ in range(50):           # the catch-up on reconnect is the only way it can arrive
                if len(self.sent) >= 2:
                    break
                time.sleep(0.1)
            caught_up.append(len(self.sent))
            self.msgs.append(msg("m4"))   # a new text's frame comes in two fragments, then m2's again
            c.sendall(frame(0x1, event("m4")[:9], 0) + frame(0x0, event("m4")[9:]) + frame(0x1, event("m2")))
            time.sleep(0.3)
            c.close()

        class Stop(Exception):
            pass
        opened, real = [], self.pc.open_socket

        def open_socket(creds):
            opened.append(1)
            if len(opened) > 2:
                raise Stop
            return real(creds)
        self.pc.open_socket = open_socket
        threading.Thread(target=server, daemon=True).start()
        bridge = threading.Thread(target=lambda: self.assertRaises(Stop, self.pc.bridge_loop, self.creds),
                                  daemon=True)
        bridge.start()
        bridge.join(20)
        self.assertFalse(bridge.is_alive())
        self.assertEqual([2], caught_up)  # m3 came by the catch-up, before any frame after it
        self.assertEqual(["text m2", "text m3", "text m4"], [re.search(r"text m\d", m).group() for m in self.sent])
        head, mask, data = pong[0][:2], pong[0][2:6], pong[0][6:]
        self.assertEqual((b"\x8a\x82", b"hb"), (head, bytes(c ^ mask[i % 4] for i, c in enumerate(data))))


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


class LabBridgeTest(unittest.TestCase):
    def test_a_saved_login_names_its_own_server(self):
        """A cloud VM learns where the owner's WhatsApp lab is from its lab login alone."""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "creds.json").write_text(json.dumps({"token": "k", "base_url": "https://lab.example/"}))
            pc = load("plow_chat_lab", "plow-chat/plow-chat.py",
                      {"PLOW_CHAT_STATE_DIR": d, "PLOW_API_BASE": "https://api.plow.co", "HOST_ID": "h"})
            self.assertEqual(pc.BASE, "https://lab.example")
            self.assertIn(f"PLOW_CHAT_STATE_DIR={d} ", pc.RUN, "the Boss's replies must use the same login")

    def test_supervise_runs_the_lab_bridge_only_while_its_login_exists(self):
        sup = (PLUGINS.parent / "bin" / "supervise.sh").read_text()
        line = next(x for x in sup.splitlines() if "plow-chat-lab/creds.json" in x)
        with tempfile.TemporaryDirectory() as idir:
            run = lambda: subprocess.run(["bash", "-c", f'ensure(){{ echo "$1|$2"; }}; ID={idir}; {line}'],
                                         capture_output=True, text=True).stdout
            self.assertEqual("", run())
            Path(idir, "state", "plow-chat-lab").mkdir(parents=True)
            Path(idir, "state", "plow-chat-lab", "creds.json").write_text("{}")
            pat, cmd = run().strip().split("|")
            self.assertTrue(pat.endswith("plow-chat.py serve lab$"))
            self.assertIn(f"PLOW_CHAT_STATE_DIR='{idir}/state/plow-chat-lab' exec python3", cmd)


class WiringTest(unittest.TestCase):
    def test_supervise_starts_it_only_when_turned_on_and_down_stops_it(self):
        root = PLUGINS.parents[2]
        sup = (root / "mypeople" / "runtime" / "bin" / "supervise.sh").read_text()
        self.assertIn('[ -n "${PLOW_CHAT:-}" ] && ensure "$ID/plugins/plow-chat/plow-chat.py serve$"', sup)
        self.assertIn('os.path.join(install, "plugins") + os.sep', (root / "mypeople" / "cli.py").read_text())

    def test_supervise_sees_only_the_main_bridge_as_the_main_bridge(self):
        """10-13: a second bridge (the owner's WhatsApp number as its own Plow account) ran the same
        script, supervise's liveness check matched it, and the dead main bridge was never restarted:
        the owner's texts went unanswered for 51 minutes. A labelled second bridge or a one-shot
        `reply` must not count as the main bridge; the main bridge itself still must."""
        sup = (PLUGINS.parent / "bin" / "supervise.sh").read_text()
        pat = re.search(r'\[ -n "\$\{PLOW_CHAT:-\}" \] && ensure "([^"]+)"', sup).group(1)
        with tempfile.TemporaryDirectory() as idir:
            script = f"{idir}/plugins/plow-chat/plow-chat.py"
            pat = pat.replace("$ID", idir)

            def running(*argv):
                return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", script, *argv])

            def seen():
                return subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True).stdout.split()

            others = [running("serve", "whatsapp"), running("reply", "cht_x", "hi")]
            try:
                self.assertEqual([], seen())
                main = running("serve")
                try:
                    self.assertEqual([str(main.pid)], seen())
                finally:
                    main.kill()
            finally:
                for p in others:
                    p.kill()


if __name__ == "__main__":
    unittest.main()
