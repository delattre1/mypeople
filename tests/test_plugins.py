"""Plugins: supervise.sh's loop, and the switch behind `mp plugins` and the board's Plugins page."""
import http.client
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

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
    return f"{idir}/plugins/{name}/{script} serve$|exec {interp} '{idir}/plugins/{name}/{script}' serve"


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
        self.assertEqual([f"{d}/no-newline/p.py serve$|exec python3 '{d}/no-newline/p.py' serve"], tick(self.idir))

    def test_installed_plugins_run_from_plugins_d(self):
        d = f"{self.idir}/plugins.d"
        write_plugin(d, "mine", "run=node p.py")
        self.assertEqual([f"{d}/mine/p.py serve$|exec node '{d}/mine/p.py' serve"], tick(self.idir))

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
