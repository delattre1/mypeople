import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from mypeople import firstrun


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"
HANDLER = ROOT / "mypeople" / "runtime" / "plugins" / "tmux-boss-hooks" / "hook-handler.py"


def load_mp(home):
    config = Path(home) / "queue.env"
    config.write_text(
        'export INSTALL_DIR="%s"\n'
        'export HOST_ID="test-node"\n'
        'export QUEUE_URL="http://127.0.0.1:9900"\n'
        'export QUEUE_SECRET="secret"\n'
        'export TTYD_PORT="7681"\n'
        'export DEFAULT_BACKEND="claude"\n'
        'export DEFAULT_ENG_MODEL="claude-opus-4-8"\n'
        'export DEFAULT_CLAUDE_MODEL="claude-opus-4-8"\n'
        'export DEFAULT_CODEX_MODEL=""\n'
        'export DEFAULT_GROK_MODEL=""\n' % (Path(home) / "state"),
        encoding="utf-8",
    )
    # mpcommon lets the ambient process env OVERRIDE the config file, so every mypeople key must be
    # pinned explicitly here (same reason as tests/test_roles.py). Without this the suite inherits a
    # developer's live HOST_ID/QUEUE_URL, decides these test agent_ids are REMOTE, and dispatches
    # real tasks at their running queue -- which has already left a `test-node/main:eng-1` fossil in
    # a live roster. QUEUE_URL points at a closed port so a regression can never reach a live daemon.
    with mock.patch.dict(os.environ, {
        "HOME": str(home),
        "MYPEOPLE_CONFIG_PATH": str(config),
        "MYPEOPLE_HOME": str(Path(home) / "state"),
        "INSTALL_DIR": str(Path(home) / "state"),
        "HOST_ID": "test-node",
        "QUEUE_URL": "http://127.0.0.1:1",
        "QUEUE_SECRET": "secret",
        "DEFAULT_BACKEND": "claude",
    }, clear=False):
        sys.modules.pop("mpcommon", None)
        sys.path.insert(0, str(BIN))
        try:
            name = "mypeople_test_mp_%s" % id(home)
            loader = importlib.machinery.SourceFileLoader(name, str(BIN / "mp"))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(BIN))


class GrokAuthTests(unittest.TestCase):
    """`grok models` exits 0 whether or not the node is logged in (probed against grok 0.2.101).

    So the returncode idiom that is correct for `codex login status` is a FALSE POSITIVE here:
    it would report a logged-out node as authenticated and let `up` spawn a Boss that then sits
    at a login prompt. These tests pin the stdout marker as the signal.
    """

    def _run(self, stdout, returncode=0):
        return mock.patch.object(
            firstrun.subprocess, "run",
            return_value=subprocess.CompletedProcess([], returncode, stdout, ""))

    def test_logged_out_grok_is_not_authenticated_despite_exit_zero(self):
        with mock.patch.object(firstrun.shutil, "which", return_value="/usr/bin/grok"), \
             self._run("You are not authenticated.\n\nDefault model: grok-build\n"):
            self.assertFalse(firstrun._grok_authenticated())

    def test_logged_in_grok_is_authenticated(self):
        with mock.patch.object(firstrun.shutil, "which", return_value="/usr/bin/grok"), \
             self._run("You are logged in with grok.com.\n\nDefault model: grok-4.5\n"):
            self.assertTrue(firstrun._grok_authenticated())

    def test_missing_grok_cli_is_not_authenticated(self):
        with mock.patch.object(firstrun.shutil, "which", return_value=None):
            self.assertFalse(firstrun._grok_authenticated())

    def test_resolve_auth_accepts_grok(self):
        with mock.patch.object(firstrun, "_grok_authenticated", return_value=True):
            ok, backend, _ = firstrun.resolve_auth("grok")
        self.assertTrue(ok)
        self.assertEqual(backend, "grok")

    def test_resolve_auth_rejects_logged_out_grok(self):
        with mock.patch.object(firstrun, "_grok_authenticated", return_value=False):
            ok, _, msg = firstrun.resolve_auth("grok")
        self.assertFalse(ok)
        self.assertIn("grok login", msg)


class HookConfigTests(unittest.TestCase):
    def test_config_upgrade_preserves_secret_and_explicit_models(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "queue.env"
            path.write_text(
                'export QUEUE_SECRET="keep-me"\n'
                'export DEFAULT_BACKEND="claude"\n'
                'export DEFAULT_CLAUDE_MODEL="claude-custom"\n'
                'export DEFAULT_CODEX_MODEL="codex-custom"\n',
                encoding="utf-8",
            )
            with mock.patch.object(firstrun, "CONFIG_PATH", str(path)), \
                 mock.patch.object(firstrun, "CONFIG_DIR", td), \
                 mock.patch.dict(os.environ, {}, clear=True):
                firstrun.write_queue_env(str(Path(td) / "state"), "codex")
            values = {}
            for line in path.read_text().splitlines():
                key, value = line.removeprefix("export ").split("=", 1)
                values[key] = value.strip('"')
            self.assertEqual("keep-me", values["QUEUE_SECRET"])
            self.assertEqual("codex", values["DEFAULT_BACKEND"])
            self.assertEqual("claude-custom", values["DEFAULT_CLAUDE_MODEL"])
            self.assertEqual("codex-custom", values["DEFAULT_CODEX_MODEL"])

    def test_retired_hooks_are_removed_and_unrelated_hooks_survive(self):
        old = "/old/plugins/tmux-boss-hooks/emit-event.sh"
        hooks = {
            "PreToolUse": [{"matcher": "AskUserQuestion", "hooks": [
                {"type": "command", "command": old + " PreToolUse"}
            ]}],
            "SessionEnd": [{"hooks": [
                {"type": "command", "command": old + " SessionEnd"}
            ]}],
            "Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/custom-stop"}]}],
        }
        current = "/new/plugins/tmux-boss-hooks/emit-event.sh"
        result = firstrun._replace_mypeople_hooks(hooks, current)

        self.assertNotIn("PreToolUse", result)
        self.assertNotIn("SessionEnd", result)
        self.assertEqual(2, len(result["Stop"]))
        self.assertEqual(
            {"SessionStart", "UserPromptSubmit", "Stop"},
            {event for event, groups in result.items()
             if any(firstrun._mypeople_hook_group(group) for group in groups)},
        )

    def test_a_second_install_leaves_a_running_installs_hooks_alone(self):
        """A smoke test or a stray app with its own INSTALL_DIR must not aim every live agent's
        hooks at itself (card 8f490e73e5). Once that install is down, the new one takes them."""
        live = "/live/plugins/tmux-boss-hooks/emit-event.sh"
        hooks = {"Stop": [{"hooks": [{"type": "command", "command": live + " Stop"}]}]}
        smoke = "/tmp/smoke/plugins/tmux-boss-hooks/emit-event.sh"
        with mock.patch.object(firstrun, "daemons_running", lambda i: i == "/live"):
            kept = firstrun._replace_mypeople_hooks(json.loads(json.dumps(hooks)), smoke)
            again = firstrun._replace_mypeople_hooks(json.loads(json.dumps(hooks)), live)
        self.assertEqual(hooks, kept)
        self.assertIn(live, json.dumps(again), "the live install still refreshes its own hooks")
        with mock.patch.object(firstrun, "daemons_running", lambda i: False):
            taken = firstrun._replace_mypeople_hooks(json.loads(json.dumps(hooks)), smoke)
        self.assertNotIn(live, json.dumps(taken))

    def test_claude_and_codex_receive_the_same_three_hooks(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"HOME": td}):
            home = Path(td)
            (home / ".claude").mkdir()
            stale = "/old/plugins/tmux-boss-hooks/emit-event.sh"
            (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
                "SessionEnd": [{"hooks": [{"type": "command", "command": stale + " SessionEnd"}]}]
            }}))
            (home / ".codex").mkdir()
            (home / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {
                "PreToolUse": [{"hooks": [{"type": "command", "command": stale + " PreToolUse"}]}]
            }}))

            firstrun.write_claude_config("/tmp/mypeople")
            firstrun.write_codex_config("/tmp/mypeople")

            claude = json.loads((home / ".claude" / "settings.json").read_text())["hooks"]
            codex = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]
            self.assertEqual(set(firstrun.LIFECYCLE_EVENTS), set(claude))
            self.assertEqual(set(firstrun.LIFECYCLE_EVENTS), set(codex))


class BackendCommandTests(unittest.TestCase):
    def test_builds_backend_specific_launch_and_resume_commands(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            claude = mp.build_launch("n/main:e", td, "n/main:Boss", False,
                                     "claude-model", "claude", "claude-session")
            codex = mp.build_launch("n/main:e", td, "n/main:Boss", False,
                                    "codex-model", "codex", "codex-session")

            self.assertIn("MYPEOPLE_BACKEND=claude", claude)
            self.assertIn("claude --dangerously-skip-permissions", claude)
            self.assertIn("--resume claude-session", claude)
            self.assertIn("MYPEOPLE_BACKEND=codex", codex)
            self.assertIn("codex --dangerously-bypass-approvals-and-sandbox", codex)
            self.assertIn("--dangerously-bypass-hook-trust", codex)
            self.assertIn("resume codex-session", codex)

    def test_finds_backend_specific_session_transcripts(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            home = Path(td)
            claude = home / ".claude" / "projects" / "p" / "claude-id.jsonl"
            codex = home / ".codex" / "sessions" / "2026" / "07" / "rollout-codex-id.jsonl"
            claude.parent.mkdir(parents=True)
            codex.parent.mkdir(parents=True)
            claude.touch()
            codex.touch()
            with mock.patch.dict(os.environ, {"HOME": td}):
                self.assertEqual([str(claude)], mp.session_files("claude", "claude-id"))
                self.assertEqual([str(codex)], mp.session_files("codex", "codex-id"))

    def test_send_routes_next_stop_to_the_calling_agent(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            target = "test-node/main:peer"
            parent = "test-node/main:parent"
            with mock.patch.dict(os.environ, {"AGENT_ID": parent}), \
                 mock.patch.object(mp.C, "tmux_send_message", return_value=True):
                self.assertEqual(0, mp.do_send([target, "do work"]))
            mp.C.open_notification_routes(target, "do work")  # the turn this message opens
            self.assertEqual([parent], mp.C.claim_notification_routes(target))
            self.assertEqual([], mp.C.claim_notification_routes(target))

    def test_failed_send_cancels_its_notification_route(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            target = "test-node/main:missing"
            with mock.patch.dict(os.environ, {"AGENT_ID": "test-node/main:parent"}), \
                 mock.patch.object(mp.C, "tmux_send_message", return_value=False):
                self.assertEqual(1, mp.do_send([target, "do work"]))
            mp.C.open_notification_routes(target, "do work")
            self.assertEqual([], mp.C.claim_notification_routes(target))

    def test_remote_send_carries_reply_to(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            with mock.patch.dict(os.environ, {"AGENT_ID": "test-node/main:parent"}), \
                 mock.patch.object(mp, "remote_task", return_value=(True, "sent")) as remote:
                self.assertEqual(0, mp.do_send(["other-node/main:peer", "do work"]))
            remote.assert_called_once_with(
                "send", "other-node/main:peer",
                {"message": "do work", "reply_to": "test-node/main:parent"}, timeout=30,
            )


class NotificationRouteTests(unittest.TestCase):
    """card 8f490e73e5: routes popped oldest-first on every Stop, so a turn nobody asked for
    (the Boss talking to the owner) reached whoever messaged it weeks ago."""

    def test_a_turn_answers_only_the_senders_whose_message_reached_it(self):
        with tempfile.TemporaryDirectory() as td:
            C = load_mp(td).C
            w = "test-node/main:worker"
            first = C.enqueue_notification_route(w, "test-node/main:first", "first ask")
            C.enqueue_notification_route(w, "test-node/main:second", "second ask")
            self.assertTrue(first)
            self.assertEqual([], C.claim_notification_routes(w), "a turn no message opened")
            C.open_notification_routes(w, "<pasted>\n  second   ask </pasted>")
            self.assertEqual(["test-node/main:second"], C.claim_notification_routes(w))
            self.assertTrue(C.cancel_notification_route(w, first))
            C.open_notification_routes(w, "first ask")
            self.assertEqual([], C.claim_notification_routes(w))

    def test_a_route_expires_and_one_queued_before_pairing_is_still_answered(self):
        with tempfile.TemporaryDirectory() as td:
            C = load_mp(td).C
            w = "test-node/main:worker"
            path, _ = C._notification_route_paths(w)
            now = time.time()
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(json.dumps([
                {"token": "a", "reply_to": "test-node/main:stale", "ts": now - C.ROUTE_TTL - 5},
                {"token": "b", "reply_to": "test-node/main:waiting", "ts": now - 60}]))
            self.assertEqual(["test-node/main:waiting"], C.claim_notification_routes(w))
            self.assertEqual([], C.claim_notification_routes(w))


class HookHandlerTests(unittest.TestCase):
    def test_codex_stop_uses_last_assistant_message(self):
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env.update({
                "INSTALL_DIR": td,
                "AGENT_ID": "node/main:eng",
                "MYPEOPLE_BACKEND": "codex",
                "BOSS_ID": "",
            })
            payload = {
                "hook_event_name": "Stop",
                "session_id": "codex-session",
                "last_assistant_message": "Finished from Codex",
            }
            subprocess.run(
                [sys.executable, str(HANDLER), "Stop"],
                input=json.dumps(payload), text=True, env=env, check=True,
            )
            status = json.loads((Path(td) / "status" / "mc-main" / "eng.json").read_text())
            self.assertEqual("codex", status["backend"])
            self.assertEqual("idle", status["status"])
            self.assertEqual("codex-session", status["session_id"])
            self.assertEqual("Finished from Codex", status["summary"])

    def test_a_long_completion_summary_is_not_truncated(self):
        """card 5676f76673. The summary was capped at 280 chars, so a completion notice
        arrived cut mid-sentence and the receiving agent had to ask for the rest -- eng-550
        got one ending "...would report their sibling", 271 chars against the cap."""
        with tempfile.TemporaryDirectory() as td:
            long_summary = ("Sentence %d of the completion summary. " % 1) * 40  # ~1500 chars
            env = dict(os.environ)
            env.update({"INSTALL_DIR": td, "AGENT_ID": "node/main:eng",
                        "MYPEOPLE_BACKEND": "codex", "BOSS_ID": ""})
            subprocess.run([sys.executable, str(HANDLER), "Stop"],
                           input=json.dumps({"hook_event_name": "Stop",
                                             "session_id": "s", 
                                             "last_assistant_message": long_summary}),
                           text=True, env=env, check=True)
            status = json.loads((Path(td) / "status" / "mc-main" / "eng.json").read_text())
            self.assertEqual(status["summary"], long_summary.strip())
            self.assertGreater(len(status["summary"]), 280)

    def test_a_summary_stays_on_one_line(self):
        """verify.sh greps "AGENT NOTIFICATION.*<agent>" on ONE captured line, so newlines
        must still collapse -- lifting the cap must not turn the notice multi-line."""
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env.update({"INSTALL_DIR": td, "AGENT_ID": "node/main:eng",
                        "MYPEOPLE_BACKEND": "codex", "BOSS_ID": ""})
            subprocess.run([sys.executable, str(HANDLER), "Stop"],
                           input=json.dumps({"hook_event_name": "Stop", "session_id": "s",
                                             "last_assistant_message": "para one\n\npara two"}),
                           text=True, env=env, check=True)
            status = json.loads((Path(td) / "status" / "mc-main" / "eng.json").read_text())
            self.assertNotIn("\n", status["summary"])
            self.assertEqual(status["summary"], "para one para two")

    def _notices(self, td, payload, transcript=None, claimed=None, opened=None):
        """What one Stop would send: the handler loaded in-process with the queue mocked."""
        import importlib.machinery, importlib.util
        if transcript is not None:
            tp = Path(td) / "t.jsonl"
            tp.write_text("".join(json.dumps(e) + "\n" for e in transcript))
            payload = dict(payload, transcript_path=str(tp))
        with mock.patch.dict(os.environ, {"INSTALL_DIR": td, "AGENT_ID": "node/main:eng",
                                          "BOSS_ID": "node/main:Boss", "MYPEOPLE_BACKEND": "claude",
                                          "QUEUE_URL": "http://queue", "QUEUE_SECRET": "s"}):
            loader = importlib.machinery.SourceFileLoader("hh_%d" % id(payload), str(HANDLER))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            hh = importlib.util.module_from_spec(spec)
            loader.exec_module(hh)
        sent = []
        opened = [] if opened is None else opened
        hh.C = mock.Mock(claim_notification_routes=lambda aid: claimed or [],
                         open_notification_routes=lambda aid, text: opened.append(text),
                         http_json=lambda *a, **k: sent.append((a[2]["target_agent"], a[2]["payload"]["message"])))
        hh.time.sleep = lambda s: None
        with mock.patch.object(sys, "stdin", __import__("io").StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "argv", ["hook-handler.py", "Stop"]):
            hh.main()
        return [m for _, m in sent] if claimed is None else sent

    def test_a_message_queued_mid_turn_is_answered_by_that_turn(self):
        """A message that lands while the agent works never opens a turn of its own; Claude
        records it as a queued_command, and the Stop that ends the turn must answer it."""
        turn = [{"type": "user", "message": {"content": "work on the card"}},
                {"type": "attachment", "attachment": {"type": "queued_command",
                                                      "prompt": "peer asks: status?"}}]
        opened = []
        with tempfile.TemporaryDirectory() as td:
            sent = self._notices(td, {"last_assistant_message": "done"}, turn,
                                 claimed=["node/main:peer"], opened=opened)
        self.assertIn("peer asks: status?", " ".join(opened))
        self.assertEqual([("node/main:peer", "[AGENT NOTIFICATION] node/main:eng finished: done")], sent)

    def test_an_empty_reply_notifies_nobody(self):
        """card 8f490e73e5: an empty reply fell back to the PREVIOUS turn's text and notified,
        and each such notice woke an idle agent whose empty reply notified again."""
        before = [{"type": "user", "message": {"content": "earlier ask"}},
                  {"type": "assistant", "message": {"content": [{"type": "text", "text": "old news"}]}},
                  {"type": "user", "message": {"content": "[AGENT NOTIFICATION] x finished: y"}}]
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual([], self._notices(td, {"last_assistant_message": ""}, before))
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual([], self._notices(td, {"last_assistant_message": "—"}, before))
        with tempfile.TemporaryDirectory() as td:  # no payload field: the transcript's own turn
            self.assertEqual([], self._notices(td, {}, before))
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(["[AGENT NOTIFICATION] node/main:eng finished: shipped it"],
                             self._notices(td, {"last_assistant_message": "shipped it"}, before))

    def _run(self, td, event, payload, backend="claude"):
        env = dict(os.environ)
        env.update({"INSTALL_DIR": td, "AGENT_ID": "node/main:eng",
                    "MYPEOPLE_BACKEND": backend, "BOSS_ID": "", "QUEUE_URL": "", "GROK_HOME": td})
        subprocess.run([sys.executable, str(HANDLER), event],
                       input=json.dumps(payload), text=True, env=env, check=True)
        return json.loads((Path(td) / "status" / "mc-main" / "eng.json").read_text())

    def test_session_start_does_not_clobber_a_working_agent(self):
        """Claude Code re-fires SessionStart after a turn. Forcing "starting" there made the
        whole fleet read as stuck until the next prompt (card 157dcb7c75)."""
        for live_state in ("working", "idle", "blocked"):
            with tempfile.TemporaryDirectory() as td:
                self._run(td, "UserPromptSubmit", {"session_id": "s1"})
                p = Path(td) / "status" / "mc-main" / "eng.json"
                cur = json.loads(p.read_text()); cur["status"] = live_state
                p.write_text(json.dumps(cur))
                status = self._run(td, "SessionStart", {"session_id": "s1"})
                self.assertEqual(live_state, status["status"], live_state)

    def test_session_start_still_starts_a_genuinely_new_agent(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual("starting", self._run(td, "SessionStart", {"session_id": "s1"})["status"])

    def test_session_start_records_the_session_id_either_way(self):
        """The id is why the hook exists: without it revive cannot find the transcript."""
        with tempfile.TemporaryDirectory() as td:
            self._run(td, "UserPromptSubmit", {"session_id": "s1"})
            p = Path(td) / "status" / "mc-main" / "eng.json"
            cur = json.loads(p.read_text()); cur["status"] = "working"
            p.write_text(json.dumps(cur))
            status = self._run(td, "SessionStart", {"session_id": "s2"})
            self.assertEqual("s2", status["session_id"])
            self.assertEqual("working", status["status"])

    def test_grok_camelcase_keys_are_understood(self):
        """Grok emits sessionId/transcriptPath; the handler reads snake_case."""
        with tempfile.TemporaryDirectory() as td:
            status = self._run(td, "SessionStart", {"sessionId": "grok-session"}, backend="grok")
            self.assertEqual("grok-session", status["session_id"])

    def test_grok_stop_reads_chat_history_beside_the_transcript_path(self):
        """transcriptPath points at updates.jsonl (chunked JSON-RPC); the readable log is
        chat_history.jsonl next to it, and grok puts reply text directly in .content."""
        with tempfile.TemporaryDirectory() as td:
            sess = Path(td) / "sessions" / "grok-session"
            sess.mkdir(parents=True)
            (sess / "updates.jsonl").write_text("{}\n")
            (sess / "chat_history.jsonl").write_text(
                json.dumps({"type": "assistant", "content": "Finished from Grok"}) + "\n")
            status = self._run(td, "Stop",
                               {"sessionId": "grok-session",
                                "transcriptPath": str(sess / "updates.jsonl")}, backend="grok")
            self.assertEqual("Finished from Grok", status["summary"])
            self.assertEqual("idle", status["status"])

    def test_grok_stop_finds_the_transcript_from_the_session_id_alone(self):
        with tempfile.TemporaryDirectory() as td:
            sess = Path(td) / "sessions" / "proj" / "grok-session"
            sess.mkdir(parents=True)
            (sess / "chat_history.jsonl").write_text(
                json.dumps({"type": "assistant",
                            "content": [{"type": "text", "text": "Block form too"}]}) + "\n")
            status = self._run(td, "Stop", {"sessionId": "grok-session"}, backend="grok")
            self.assertEqual("Block form too", status["summary"])


if __name__ == "__main__":
    unittest.main()
