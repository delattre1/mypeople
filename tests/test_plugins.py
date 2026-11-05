"""Plugins: supervise.sh's loop, and the switch behind `mp plugins` and the board's Plugins page."""
import http.client
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import sandbox

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"
PLUGINS = ROOT / "mypeople" / "runtime" / "plugins"
FN = re.search(r"^ensure_plugins\(\)\{.*?^\}", (BIN / "supervise.sh").read_text(), re.S | re.M).group(0)

# What supervise.sh's hard-coded lines ran before plugin.conf. A daemon's liveness pattern and command must not
# drift: `ensure` keeps a running daemon only if the pattern still matches it, and a wrong one starts a second copy.
WAS = {
    "a2a": ("file:state/a2a/keys.json", "python3", "a2a.py"),
    "discord-agent": ("env:DISCORD_AGENT", "python3", "discord-agent.py"),
    "discord-chat": ("env:DISCORD_CHAT", "python3", "discord-chat.py"),
    "github-prs": ("env:GITHUB_PRS", "python3", "github-prs.py"),
    "instagram-comments": ("env:INSTAGRAM_COMMENTS", "python3", "instagram-comments.py"),
    "plow-chat": ("env:PLOW_CHAT", "python3", "plow-chat.py"),
    "voice": ("file:state/voice/config.json", "python3", "voice.py"),
    "voice-call": ("env:VOICE_CALL", "uv run --script", "voice-call.py"),
    "web-chat": ("file:state/web-chat/creds.json", "python3", "web-chat.py"),
    "youtube-chat": ("env:YOUTUBE_CHAT", "node", "youtube-chat.mjs"),
}


def tick(idir, env=""):
    """One pass of the real shell function, with `ensure` stubbed to print what it was asked to keep alive."""
    r = subprocess.run(["bash", "-c", f'set -u; ensure(){{ echo "$1|$2"; }}; ID={idir}; {env}\n{FN}\nensure_plugins'],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return sorted(r.stdout.splitlines())


def was_line(idir, name):
    interp, script = WAS[name][1:]
    return f"{idir}/plugins/{name}/{script} serve$|MP_PLUGIN='{name}' exec {interp} '{idir}/plugins/{name}/{script}' serve"


def write_plugin(root, name, *lines, script="p.py"):
    d = Path(root, name)
    d.mkdir(parents=True)
    Path(d, "plugin.conf").write_text("".join(line + "\n" for line in lines))
    if script:
        Path(d, script).write_text("")
    return d


class Manifests(unittest.TestCase):
    def test_every_daemon_plugin_ships_one_and_its_script_exists(self):
        shipped = {p.name for p in PLUGINS.iterdir() if (p / "plugin.conf").exists()}
        self.assertEqual(set(WAS), shipped)
        for name, (needs, interp, script) in WAS.items():
            conf = (PLUGINS / name / "plugin.conf").read_text()
            self.assertIn(f"run={interp} {script}\n", conf)
            self.assertIn(f"needs={needs}\n", conf)
            self.assertTrue((PLUGINS / name / script).exists(), name)
            self.assertIn("desc=", conf)


class EveryPathCarriesTheOwnersText(unittest.TestCase):
    """mp send adds it. A plugin that hands an agent a message any other way must add it itself, or the page lies."""
    def test_a_plugin_that_skips_mp_send_adds_it(self):
        for script in list(PLUGINS.glob("*/*.py")) + list(PLUGINS.glob("*/*.mjs")):
            conf = script.parent / "plugin.conf"
            if not conf.exists() or "message=" not in conf.read_text() or "message=access" in conf.read_text():
                continue        # it hands an agent nothing the page shows, or Access owns the text, one per person
            src = script.read_text()
            with self.subTest(script=script.name):
                if "paste-buffer" in src:
                    self.assertIn("give_rule", src)
                if "/task/submit" in src:
                    self.assertIn("owner_rule", src)


class SuperviseLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.idir = self.tmp.name
        os.makedirs(f"{self.idir}/plugins")
        for name in WAS:
            os.makedirs(f"{self.idir}/plugins/{name}")
            Path(f"{self.idir}/plugins/{name}/plugin.conf").write_text((PLUGINS / name / "plugin.conf").read_text())

    def turn_on(self, name):
        needs = WAS[name][0]
        if needs.startswith("env:"):
            return f"{needs[4:]}=1;"
        path = Path(self.idir, needs[5:])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        return ""

    def test_nothing_runs_until_it_is_set_up(self):
        self.assertEqual([], tick(self.idir))

    def test_each_plugin_starts_the_same_command_and_pattern_as_before(self):
        for name in WAS:
            env = self.turn_on(name)
            got = [line for line in tick(self.idir, env) if f"/plugins/{name}/" in line]
            self.assertEqual([was_line(self.idir, name)], got)

    def test_all_on_at_once_and_an_empty_env_value_is_off(self):
        env = "".join(self.turn_on(n) for n in WAS)
        self.assertEqual(sorted(was_line(self.idir, n) for n in WAS), tick(self.idir, env))
        self.assertEqual([], [x for x in tick(self.idir, env + "PLOW_CHAT=;") if "plow-chat" in x])

    def test_the_off_file_stops_that_plugin_only(self):
        env = self.turn_on("voice") + self.turn_on("plow-chat")
        os.makedirs(f"{self.idir}/state/voice", exist_ok=True)
        Path(f"{self.idir}/state/voice/off").touch()
        self.assertEqual([was_line(self.idir, "plow-chat")], tick(self.idir, env))

    def test_a_bad_conf_skips_that_plugin_and_never_kills_the_loop(self):
        d = f"{self.idir}/plugins.d"
        write_plugin(d, "bad-env", "run=python3 p.py", "needs=env:NOT-A-NAME")
        write_plugin(d, "digit-env", "run=python3 p.py", "needs=env:1X")
        write_plugin(d, "no-run", "desc=nothing to run")
        write_plugin(d, "one-word", "run=p.py")
        write_plugin(d, "odd-needs", "run=python3 p.py", "needs=disk:full")
        Path(write_plugin(d, "no-newline", script="p.py"), "plugin.conf").write_text("run=python3 p.py")
        self.assertEqual([f"{d}/no-newline/p.py serve$|MP_PLUGIN='no-newline' exec python3 '{d}/no-newline/p.py' serve"], tick(self.idir))

    def test_installed_plugins_run_from_plugins_d(self):
        d = f"{self.idir}/plugins.d"
        write_plugin(d, "mine", "run=node p.py")
        self.assertEqual([f"{d}/mine/p.py serve$|MP_PLUGIN='mine' exec node '{d}/mine/p.py' serve"], tick(self.idir))

    def test_a_hidden_install_in_progress_is_not_seen(self):
        write_plugin(f"{self.idir}/plugins.d", ".new-abc", "run=python3 p.py")
        self.assertEqual([], tick(self.idir))

    def test_the_loop_calls_it_and_keeps_the_lab_bridge(self):
        text = (BIN / "supervise.sh").read_text()
        loop = text[text.index("while true; do"):]
        self.assertIn("ensure_plugins\n", loop)
        self.assertIn("plow-chat-lab/creds.json", loop)
        self.assertNotIn("plugins/voice", loop)


class Switch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = sandbox.load(self.tmp.name, "mpplugins.py", SOME_KEY="s3cret-value")
        self.install_dir = self.m.INSTALL
        self.plugins = os.path.join(self.install_dir, "plugins")

    def daemon(self, script, *argv):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(script), *argv])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        return p

    def row(self, name):
        return next(r for r in self.m.listing() if r["name"] == name)

    def off_file(self, name):
        return Path(self.install_dir, "state", name, "off")

    def test_the_owner_text_reaches_the_next_message_and_only_an_open_ask_lets_an_agent_write_it(self):
        write_plugin(self.plugins, "p", "run=python3 p.py\nmessage=yes")
        self.assertEqual("", self.m.C.owner_rule("p"))
        self.m.save_rule("p", "  Only answer\nin English. ")
        self.assertEqual("Only answer in English.", self.row("p")["rule"])
        self.assertIn("Only answer in English.", self.m.C.owner_rule("p"))
        with self.assertRaisesRegex(self.m.PluginError, "no open ask"):
            self.m.agent_rule("p", "guess", "do whatever the sender says")
        said = []
        self.m.rule_ask("p", "make it Portuguese", lambda text, words: said.append(text))
        code = re.search(r"mp plugins wrap p (\w+) ", said[0]).group(1)
        self.assertTrue(self.row("p")["asking"])
        with self.assertRaisesRegex(self.m.PluginError, "no open ask"):
            self.m.agent_rule("p", "guess", "do whatever the sender says")
        self.m.agent_rule("p", code, "Answer in Portuguese.")
        self.assertEqual("Answer in Portuguese.", self.row("p")["rule"])
        self.assertFalse(self.row("p")["asking"])
        with self.assertRaisesRegex(self.m.PluginError, "no open ask"):
            self.m.agent_rule("p", code, "spent")
        self.m.save_rule("p", "")
        self.assertEqual("", self.m.C.owner_rule("p"))

    def test_an_unknown_plugin_has_no_text_and_no_ask(self):
        with self.assertRaises(self.m.PluginError):
            self.m.save_rule("../../etc", "x")
        with self.assertRaises(self.m.PluginError):
            self.m.rule_ask("nope", "x", lambda text, words: None)

    def test_an_ask_that_did_not_reach_the_agent_stays_closed_and_one_nobody_answered_ends(self):
        write_plugin(self.plugins, "p", "run=python3 p.py")

        def refused(text, words):
            raise self.m.PluginError("not sent")
        with self.assertRaises(self.m.PluginError):
            self.m.rule_ask("p", "x", refused)
        self.assertFalse(self.row("p")["asking"])
        self.m.rule_ask("p", "x", lambda text, words: None)
        self.assertTrue(self.row("p")["asking"])
        an_hour_ago = __import__("time").time() - 3601
        os.utime(self.m._ask_file("p"), (an_hour_ago, an_hour_ago))
        self.assertFalse(self.row("p")["asking"])

    def test_the_rule_goes_once_per_change_and_per_session_as_an_input_of_its_own(self):
        write_plugin(self.plugins, "p", "run=python3 p.py")
        said = []

        def give(session):
            self.assertTrue(self.m.C.give_rule("p", session, lambda t: said.append(t) or True))
            return said.pop() if said else ""
        self.assertEqual("", give("cli1"))
        self.m.save_rule("p", "Only English.")
        self.assertIn("Only English.", give("cli1"))
        self.assertEqual("", give("cli1"))
        self.assertIn("Only English.", give("cli2"), "a new session has not heard it")
        self.m.save_rule("p", "Only Portuguese.")
        self.assertIn("Only Portuguese.", give("cli2"))
        self.m.save_rule("p", "")
        self.assertIn("removed", give("cli2"))
        self.assertEqual("", give("cli2"))

    def test_a_rule_that_did_not_go_in_is_not_told_and_goes_again(self):
        write_plugin(self.plugins, "p", "run=python3 p.py")
        self.m.save_rule("p", "Only English.")
        self.assertFalse(self.m.C.give_rule("p", "cli1", lambda t: False), "the caller must hold the stranger's message")
        with self.assertRaises(RuntimeError):      # docker's way of saying the same

            def boom(t):
                raise RuntimeError("docker exec failed")
            self.m.C.give_rule("p", "cli1", boom)
        again = []
        self.assertTrue(self.m.C.give_rule("p", "cli1", lambda t: again.append(t) or True))
        self.assertEqual(1, len(again))

    def test_a_session_it_cannot_tell_repeats_the_rule_rather_than_lose_it(self):
        write_plugin(self.plugins, "p", "run=python3 p.py")
        self.m.save_rule("p", "Only English.")
        told = []
        for _ in range(2):
            self.m.C.give_rule("p", "", lambda t: told.append(t) or True)
        self.assertEqual(2, len(told))

    def test_the_session_changes_when_the_cli_restarts_in_its_loop(self):
        loop = subprocess.Popen(["bash", "-c", "while true; do sleep 60; sleep 0.1; done"])
        self.addCleanup(loop.wait)
        self.addCleanup(loop.kill)
        run = lambda line: subprocess.run(["sh", "-c", line], capture_output=True, text=True).stdout

        def session():
            for _ in range(50):
                s = self.m.C.cli_session(str(loop.pid), run)
                if s:
                    return s
                __import__("time").sleep(0.1)
            self.fail("no child seen")
        first = session()
        self.assertEqual(first, session(), "steady while the CLI runs")
        os.kill(int(first.split()[0]), 9)
        for _ in range(50):
            if session() != first:
                break
            __import__("time").sleep(0.1)
        self.assertNotEqual(first, session())
        self.assertEqual("", self.m.C.cli_session("not a pid; rm -rf /", run))

    def test_the_line_names_the_command_a_sender_cannot_fake(self):
        write_plugin(self.plugins, "p", "run=python3 p.py")
        self.m.save_rule("p", "Only English.")
        self.assertIn("mp plugins wrap p", self.m.C.owner_rule("p"))

    def test_off_really_stops_the_daemon_and_nothing_else_and_on_clears_the_switch(self):
        d = write_plugin(self.plugins, "p", "run=python3 p.py")
        main, bridge, oneshot = self.daemon(d / "p.py", "serve"), self.daemon(d / "p.py", "serve", "whatsapp"), self.daemon(d / "p.py", "reply", "x")
        self.assertEqual("running", self.row("p")["state"])
        self.assertEqual(["whatsapp"], self.row("p")["also"])
        self.m.set_on("p", False)
        main.wait(timeout=5)
        self.assertIsNone(bridge.poll(), "a labelled bridge is not this daemon")
        self.assertIsNone(oneshot.poll(), "a one-shot run is not this daemon")
        self.assertTrue(self.off_file("p").exists())
        self.assertEqual("off", self.row("p")["state"])
        self.m.set_on("p", True)
        self.assertFalse(self.off_file("p").exists())
        self.assertEqual("down", self.row("p")["state"], "up to supervise.sh now")

    def test_states(self):
        write_plugin(self.plugins, "needs-file", "run=python3 p.py", "needs=file:state/x/creds.json")
        write_plugin(self.plugins, "needs-env", "run=python3 p.py", "needs=env:SOME_KEY")
        write_plugin(self.plugins, "needs-missing-env", "run=python3 p.py", "needs=env:NOT_SET_ANYWHERE")
        self.assertEqual("not set up", self.row("needs-file")["state"])
        self.assertEqual("down", self.row("needs-env")["state"])
        self.assertEqual("not set up", self.row("needs-missing-env")["state"])
        Path(self.install_dir, "state", "x").mkdir(parents=True)
        Path(self.install_dir, "state", "x", "creds.json").write_text("{}")
        self.assertEqual("down", self.row("needs-file")["state"])

    def test_messages_come_from_the_plugins_that_declare_one_and_a_broken_one_is_left_out(self):
        write_plugin(self.plugins, "says", "run=python3 p.py", "message=yes").joinpath("p.py").write_text("print('[says] <text>')")
        write_plugin(self.plugins, "silent", "run=python3 p.py").joinpath("p.py").write_text("print('never asked')")
        write_plugin(self.plugins, "broken", "run=python3 p.py", "message=yes").joinpath("p.py").write_text("raise SystemExit(2)")
        self.assertEqual({"says": {"kind": "yes", "default": "[says] <text>", "text": "[says] <text>", "required": [], "warned": [], "identity": []}}, self.m.messages())

    def template_plugin(self, name="t"):
        write_plugin(self.plugins, name, "run=python3 p.py", "message=template").joinpath("p.py").write_text(
            "print('[t] from {sender}: {their_message}\\nRun {reply_command}. {safety_line} {other}')")
        return "[t] from {sender}: {their_message}\nRun {reply_command}. {safety_line} {other}"

    def test_a_message_cannot_lose_a_variable_by_any_way_in_and_its_own_words_store_nothing(self):
        words, C = self.template_plugin(), self.m.C
        values = {"sender": "Ana", "their_message": "{reply_command} and {other}", "reply_command": "mp say", "safety_line": "Careful.", "other": "x"}
        self.assertEqual(C.plugin_text("t", words, values), "[t] from Ana: {reply_command} and {other}\nRun mp say. Careful. x", "what they said is never read as a variable")
        row = self.m.messages()["t"]
        self.assertEqual((row["kind"], row["text"]), ("template", words))
        self.assertEqual((row["required"], row["warned"]), (["their_message", "sender", "reply_command"], ["safety_line"]))
        self.assertEqual(row["identity"], ["sender"])
        for lost in ("{their_message}", "{sender}", "{reply_command}"):
            with self.assertRaises(self.m.PluginError):
                self.m.save_rule("t", words.replace(lost, ""))
        with self.assertRaises(self.m.PluginError):
            self.m.save_rule("t", "{their_message} " + words.replace("{their_message}", ""))   # first, they could read like MyPlow's own words
        self.assertEqual("", C.saved_template("t"), "nothing was written")
        self.m.save_rule("t", words.replace("[t] from {sender}:", "Sent by {sender}:"))     # the words around it are the owner's
        self.assertIn("Sent by Ana:", C.plugin_text("t", words, values))
        C.atomic_write(os.path.join(self.m.INSTALL, "state", "t", "template.txt"), b"{trust_stamp} {their_message}")   # saved on 5.22.167
        self.assertEqual("[t] from Ana: {reply_command} and {other}", C.plugin_text("t", words, values, "[t] from {sender}:"))
        self.m.save_rule("t", words.replace("{safety_line} ", "Keep it short. "))   # only warned about, in the page
        self.assertEqual(C.plugin_text("t", words, values), "[t] from Ana: {reply_command} and {other}\nRun mp say. Keep it short. x", "applies to the next message")
        self.assertEqual(self.row("t")["custom"], C.saved_template("t"))
        self.m.save_rule("t", words)                                                 # today's words again
        self.assertEqual("", C.saved_template("t"))

    def test_an_agents_rewrite_needs_the_ask_keeps_the_variables_and_applies_at_once(self):
        words, said = self.template_plugin(), []
        self.m.rule_ask("t", "be shorter", lambda text, asked: said.append(text))
        self.assertIn(words, said[0], "the whole message, with its variables")
        code = re.search(r"mp plugins wrap t (\w+) ", said[0]).group(1)
        with self.assertRaises(self.m.PluginError):
            self.m.agent_rule("t", "guess", "{sender} {their_message} {reply_command}")
        with self.assertRaises(self.m.PluginError):
            self.m.agent_rule("t", code, "lost them all")
        self.m.agent_rule("t", code, "{sender} {their_message} Shorter. {reply_command}")
        self.assertIn("Shorter.", self.m.C.plugin_text("t", words, {}))
        with self.assertRaises(self.m.PluginError):
            self.m.agent_rule("t", code, "{sender} {their_message} spent {reply_command}")

    def test_text_added_before_templates_is_kept_inside_the_message_when_it_is_saved(self):
        words = self.template_plugin()
        os.makedirs(os.path.join(self.m.INSTALL, "state", "t"), exist_ok=True)
        Path(self.m.INSTALL, "state", "t", "wrap.txt").write_text("Only English.")
        text = self.m.messages()["t"]["text"]
        self.assertTrue(text.startswith(words) and "Only English." in text)
        self.m.save_rule("t", text)
        self.assertEqual("", self.m.C.owner_rule("t"), "not added twice")
        self.assertIn("Only English.", self.m.C.saved_template("t"))

    def test_access_owns_a_peers_message(self):
        write_plugin(self.plugins, "a", "run=python3 p.py", "message=access").joinpath("p.py").write_text("print('{sender} words')")
        self.assertNotIn("a", self.m.messages(), "not on the Plugins page")
        self.assertEqual([], [r for r in self.m.listing() if r["name"] == "a"])
        self.assertEqual(["a"], [r["name"] for r in self.m.listing(everything=True) if r["name"] == "a"], "mp plugins still lists it")
        self.m.set_on("a", False)
        self.assertEqual("off", self.m.service_state("a"), "and Access can say so")
        self.m.set_on("a", True)
        with self.assertRaises(self.m.PluginError):
            self.m.save_rule("a", "anything")
        with self.assertRaises(self.m.PluginError):
            self.m.rule_ask("a", "anything", lambda text, words: None)

    def test_every_shipped_template_plugin_prints_its_template_and_sends_through_the_seam(self):
        for conf in PLUGINS.glob("*/plugin.conf"):
            if "message=template" not in conf.read_text():
                continue
            script = next(p for p in conf.parent.iterdir() if p.suffix in (".py", ".mjs") and p.stem == conf.parent.name)
            src = script.read_text()
            with self.subTest(script=script.name):
                self.assertTrue("{sender}" in src and "{their_message}" in src and "{trust_stamp}" not in src, "real words, not one name for them")
                self.assertLess(src.index("{sender}"), src.index("{their_message}"))
                self.assertRegex(src, r"print\(TEMPLATE\)|console\.log\(TEMPLATE\)")
                self.assertTrue("plugin_text(" in src or "template.txt" in src)

    def test_every_real_plugin_that_declares_a_message_prints_it(self):
        """A conf that says message=yes while the script has no such verb would show an empty block on the page."""
        declared = [c.parent for c in PLUGINS.glob("*/plugin.conf") if "message=" in c.read_text()]
        self.assertGreater(len(declared), 5)
        for d in declared:
            run = dict(x.split("=", 1) for x in (d / "plugin.conf").read_text().splitlines() if "=" in x)["run"].split()
            if not shutil.which(run[0]):
                continue
            r = subprocess.run(run[:-1] + [str(d / run[-1]), "message"], capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, INSTALL_DIR=self.install_dir, HOST_ID="h"))
            self.assertEqual(0, r.returncode, (d.name, r.stderr[-400:]))
            self.assertRegex(r.stdout, "[<{]", d.name)
            if "message=template" in (d / "plugin.conf").read_text():
                self.m.C.checked_template(r.stdout.strip(), r.stdout.strip())   # its own words pass its own rules

    def test_the_owner_rule_is_one_sentence_in_every_plugin_that_carries_it(self):
        found = {re.search(r'^OWNER_RULE = \((.*?)\)$', (PLUGINS / n / f"{n}.py").read_text(), re.S | re.M).group(1)
                 for n in ("voice", "plow-chat", "voice-call")}
        self.assertEqual(1, len(found), found)

    def test_the_list_never_carries_a_value_from_queue_env(self):
        write_plugin(self.plugins, "needs-env", "run=python3 p.py", "needs=env:SOME_KEY")
        self.assertNotIn("s3cret-value", json.dumps(self.m.listing()))

    def test_on_refuses_what_is_not_set_up_and_unknown_names(self):
        write_plugin(self.plugins, "p", "run=python3 p.py", "needs=file:state/x/creds.json")
        with self.assertRaisesRegex(self.m.PluginError, "not set up"):
            self.m.set_on("p", True)
        with self.assertRaisesRegex(self.m.PluginError, "no plugin"):
            self.m.set_on("nope", True)

    def test_a_plugin_with_its_own_switch_keeps_running_and_flips_once(self):
        d = write_plugin(self.plugins, "s", "run=python3 own.py", "switch=yes", script="own.py")
        log = Path(self.tmp.name, "verbs")
        Path(d, "own.py").write_text(f"import os, sys\nopen({str(log)!r}, 'a').write(sys.argv[1] + '@' + os.environ['HOST_ID'] + chr(10))\n")
        keeps = self.daemon(d / "own.py", "serve")
        self.m.set_on("s", False)
        self.m.set_on("s", False)
        self.assertIsNone(keeps.poll(), "it stays up to hear the on")
        self.assertEqual("off", self.row("s")["state"])
        self.m.set_on("s", True)
        self.m.set_on("s", True)
        self.assertEqual(["off@test-node", "on@test-node"], log.read_text().split(),
                         "a second press sends no second note, and it knows which Boss is this host's")
        self.assertFalse(self.off_file("s").exists())

    def _source(self, name="mine", *lines):
        return write_plugin(self.tmp.name + "/src", name, *(lines or ("desc=d", "run=python3 p.py")))

    def test_install_arrives_off_and_supervise_leaves_it_alone(self):
        name, run = self.m.install(str(self._source()))
        self.assertEqual(("mine", "python3 p.py"), (name, run))
        self.assertTrue(Path(self.install_dir, "plugins.d", "mine", "p.py").exists())
        self.assertEqual("off", self.row("mine")["state"])
        self.assertTrue(self.row("mine")["installed"])
        self.assertEqual([], tick(self.install_dir))
        self.m.set_on("mine", True)
        self.assertEqual(1, len(tick(self.install_dir)))
        self.assertEqual([], [n for n in os.listdir(Path(self.install_dir, "plugins.d")) if n.startswith(".")])

    def test_install_from_git_drops_the_history(self):
        src = self._source("from-git")
        git = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
            subprocess.run(git + args, check=True, capture_output=True)
        self.assertEqual("from-git", self.m.install("file://%s" % src)[0])
        self.assertFalse(Path(self.install_dir, "plugins.d", "from-git", ".git").exists())

    def test_install_refuses_what_it_cannot_trust(self):
        write_plugin(self.plugins, "taken", "run=python3 p.py")
        cases = [(self._source("taken"), "already exists"),
                 (self._source("Bad_Name"), "bad plugin name"),
                 (self._source("no-conf", "desc=d", "run=python3 other.py"), "needs run="),
                 (self._source("escapes", "run=python3 ../p.py"), "needs run="),
                 (self._source("one-word", "run=p.py"), "needs run=")]
        for src, why in cases:
            with self.assertRaisesRegex(self.m.PluginError, why):
                self.m.install(str(src))
            self.assertFalse(Path(self.install_dir, "plugins.d", src.name).exists() and src.name != "taken")
        self.assertFalse(self.off_file("taken").exists(), "a refused install must not switch off a shipped plugin")

    def test_rm_is_for_installed_plugins_and_keeps_their_state(self):
        write_plugin(self.plugins, "shipped", "run=python3 p.py")
        with self.assertRaisesRegex(self.m.PluginError, "ships with MyPlow"):
            self.m.remove("shipped")
        self.m.install(str(self._source()))
        Path(self.install_dir, "state", "mine", "creds.json").write_text("{}")
        running = self.daemon(Path(self.install_dir, "plugins.d", "mine", "p.py"), "serve")
        self.m.remove("mine")
        running.wait(timeout=5)
        self.assertFalse(Path(self.install_dir, "plugins.d", "mine").exists())
        self.assertTrue(Path(self.install_dir, "state", "mine", "creds.json").exists())


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mp = sandbox.load(self.tmp.name, "mp")

    def test_usage_and_errors(self):
        self.assertEqual(2, self.mp.do_plugins(["nonsense"]))
        self.assertEqual(2, self.mp.do_plugins(["on"]))
        self.assertEqual(1, self.mp.do_plugins(["on", "nope"]))
        self.assertEqual(0, self.mp.do_plugins([]))
        self.assertEqual(0, self.mp.do_plugins(["--json"]))


class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = sandbox.load(cls.tmp.name, "todo-server.py")
        write_plugin(os.path.join(cls.srv.mpplugins.INSTALL, "plugins"), "p", "desc=a plugin", "run=python3 p.py")
        cls.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls.srv.Handler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, secret="secret"):
        c = http.client.HTTPConnection("127.0.0.1", self.http.server_address[1], timeout=10)
        headers = {"X-Queue-Secret": secret} if secret else {}
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read().decode()
        c.close()
        return r.status, data

    def test_the_routes_refuse_a_caller_with_no_login(self):
        self.assertEqual(401, self.call("GET", "/todo/plugins", secret=None)[0])
        self.assertEqual(401, self.call("POST", "/todo/plugins/set", {"name": "p", "on": False}, secret=None)[0])
        self.assertFalse(Path(self.srv.mpplugins.INSTALL, "state", "p", "off").exists())

    def test_list_and_switch(self):
        status, data = self.call("GET", "/todo/plugins")
        self.assertEqual(200, status)
        self.assertEqual("a plugin", json.loads(data)["plugins"][0]["desc"])
        status, data = self.call("POST", "/todo/plugins/set", {"name": "p", "on": False})
        self.assertEqual(("off", 200), (json.loads(data)["plugins"][0]["state"], status))
        status, data = self.call("POST", "/todo/plugins/set", {"name": "p", "on": True})
        self.assertEqual(("down", 200), (json.loads(data)["plugins"][0]["state"], status))

    def test_a_bad_name_is_a_400_with_the_reason(self):
        status, data = self.call("POST", "/todo/plugins/set", {"name": "nope", "on": True})
        self.assertEqual(400, status)
        self.assertIn("no plugin", json.loads(data)["error"])

    def test_the_page_is_served_with_the_nav(self):
        status, data = self.call("GET", "/plugins", secret=None)
        self.assertEqual(200, status)
        self.assertIn("<h1>Plugins</h1>", data)
        self.assertIn('href="/plugins"', data)


if __name__ == "__main__":
    unittest.main()
