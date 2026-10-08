"""Anyone else's cloud MyPlow runs on ITS OWN owner's Claude, never on Daniel's.

claude-login.sh fetch, with curl and claude stubbed on PATH: Daniel's login server answers 200 (his
agents) or 401 (anyone else's). On 401 the install uses the login saved on its own VM, or asks its
owner once by text (own-login.py, stubbed here) and saves the answer 0600.
"""
import importlib.util
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOOD = "sk-ant-oat01-" + "g" * 40


class ClaudeLoginFetch(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.td)
        rel = self.td / "rel"
        rel.mkdir()
        shutil.copy(ROOT / "cloud" / "claude-login.sh", rel)
        (rel / "own-login.py").write_text(
            "import os, sys\nopen(os.environ['ASKED'], 'w').write('asked')\nsys.stdout.write(%r)\n" % GOOD)
        bin_ = self.td / "bin"
        bin_.mkdir()
        # curl: identity -> an assertion; POST /token -> "<body>\n<status>" from $BANK_STATUS; beacons -> nothing
        self.stub(bin_ / "curl", """#!/bin/bash
case "$*" in
  *index-identity*) echo '{"assertion": "a"}' ;;
  */token*) printf '{"token": "sk-ant-oat01-daniels-%040d"}\\n%s' 0 "$BANK_STATUS" ;;
esac""")
        # claude -p works only for a real-looking login
        self.stub(bin_ / "claude", '#!/bin/bash\n[[ "$CLAUDE_CODE_OAUTH_TOKEN" == sk-ant-oat01-* ]]')
        self.env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", MYPEOPLE_HOME=str(self.td / "data"),
                        PLOW_API_BASE="http://plow", MYPLOW_CLAUDE_BANK="http://bank",
                        ASKED=str(self.td / "asked"))
        self.own = self.td / "data" / "state" / "claude-login" / "own-token"
        self.script = rel / "claude-login.sh"

    def stub(self, path, body):
        path.write_text(body + "\n")
        path.chmod(0o755)

    def fetch(self, bank):
        return subprocess.run(["bash", str(self.script), "fetch"], capture_output=True, text=True,
                              env=dict(self.env, BANK_STATUS=str(bank)), timeout=60)

    def test_the_login_server_can_come_from_the_install_s_own_file(self):
        """No built-in login server (the repo is public): an install names its own, by env or by a file on its
        data disk that in-place updates keep. Daniel's cloud MyPlow carries the file."""
        self.env.pop("MYPLOW_CLAUDE_BANK")
        (self.td / "data" / "state").mkdir(parents=True, exist_ok=True)
        (self.td / "data" / "state" / "claude-bank-url").write_text("http://bank\n")
        r = self.fetch(200)
        self.assertEqual(0, r.returncode)
        self.assertTrue(r.stdout.startswith("sk-ant-oat01-daniels-"))

    def test_without_a_login_server_an_install_uses_its_own_owner_s_login(self):
        self.env.pop("MYPLOW_CLAUDE_BANK")
        r = self.fetch(200)   # a /token answer would be Daniel's: it must never be asked for
        self.assertEqual(0, r.returncode)
        self.assertFalse(r.stdout.startswith("sk-ant-oat01-daniels-"))
        self.assertTrue(Path(self.env["ASKED"]).exists())

    def test_no_cloud_script_carries_a_login_server_default(self):
        for f in (ROOT / "cloud").iterdir():
            if f.is_file():
                text = f.read_text(errors="replace")
                self.assertNotRegex(text, r"MYPLOW_CLAUDE_BANK:-https?://", f.name)
                self.assertNotIn(".ts.net", text, f.name)

    def test_daniels_agents_keep_his_login_and_never_ask(self):
        r = self.fetch(200)
        self.assertEqual(0, r.returncode)
        self.assertTrue(r.stdout.startswith("sk-ant-oat01-daniels-"))
        self.assertFalse(self.own.exists())
        self.assertFalse(Path(self.env["ASKED"]).exists())

    def test_someone_elses_install_asks_once_and_saves_its_own_login(self):
        r = self.fetch(401)
        self.assertEqual((0, GOOD), (r.returncode, r.stdout))
        self.assertEqual(GOOD, self.own.read_text())
        self.assertEqual(0o600, stat.S_IMODE(self.own.stat().st_mode))
        self.assertNotIn(GOOD, r.stderr, "the login is never logged")

    def test_a_saved_login_is_used_without_asking_again(self):
        self.own.parent.mkdir(parents=True)
        self.own.write_text(GOOD)
        r = self.fetch(401)
        self.assertEqual((0, GOOD), (r.returncode, r.stdout))
        self.assertFalse(Path(self.env["ASKED"]).exists(), "a restart must not text the owner again")


class OwnLoginParsing(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("own_login", ROOT / "cloud" / "own-login.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_link_comes_whole_from_the_terminal_hyperlink(self):
        url = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=abc"
        out = (b"Browser didn't open? Use the url below:\r\n\x1b]8;;" + url.encode() + b"\x07"
               + url[:40].encode() + b"\r\n" + url[40:].encode() + b"\x1b]8;;\x07\r\nPaste code here if prompted>")
        self.assertEqual(url, self.m.link_of(out))

    def test_the_login_is_read_whole_even_when_the_terminal_wrapped_it(self):
        tok = "sk-ant-oat01-" + "A" * 66 + "e_iI4D2GpwsSFVy82CXw-_rwA_QAA"
        out = (b"\x1b[32m\xe2\x9c\x93 Long-lived authentication token created successfully!\r\n"
               b" Your OAuth token (valid for 1 year):\r\n \x1b[1m" + tok[:79].encode() + b"\r\n "
               + tok[79:].encode() + b"\x1b[0m\r\n Store this token securely.")
        self.assertEqual(tok, self.m.login_of(out))
        # what a 500-column pty really shows: spaces drawn as cursor moves, the login on one line
        wide = (b"\x1b[32m\xe2\x9c\x93\x1b[1CLong-lived\x1b[1Cauthentication token created successfully!\r\n"
                b"Your\x1b[1COAuth\x1b[1Ctoken\x1b[1C(valid\x1b[1Cfor\x1b[1C1\x1b[1Cyear):\r\n" + tok.encode()
                + b"\r\nStore\x1b[1Cthis token securely. You won't be able to see it again.")
        self.assertEqual(tok, self.m.login_of(wide))
        self.assertIsNone(self.m.login_of(b"OAuth error: Request failed with status code 400"))

    def test_the_code_is_the_owners_next_text_not_history_or_our_own(self):
        msgs = [{"uid": "m1", "direction": "inbound", "body": "set this up", "created_at": "1"},
                {"uid": "m2", "direction": "outbound", "body": "tap this: https://...", "created_at": "2"},
                {"uid": "m3", "direction": "inbound", "body": "here: abc123#state9", "created_at": "3"}]
        self.m.messages = lambda chat: msgs
        self.m.time.sleep = lambda s: None
        code, _ = self.m.wait_for_code("cht_x", {"m1"})
        self.assertEqual("abc123#state9", code)


if __name__ == "__main__":
    unittest.main()
