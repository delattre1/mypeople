"""Live-parity fixes that only existed on the CEO's install until 0.4.0.

tmux_send_message was mocked by every other test, so its submit behaviour was never exercised
here -- which is how an unconditional second Enter survived. These tests drive the real function
with tmux stubbed at subprocess level.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


def load_mpcommon(home):
    config = Path(home) / "queue.env"
    config.write_text(
        'export INSTALL_DIR="%s"\n'
        'export HOST_ID="test-node"\n'
        'export QUEUE_URL="http://127.0.0.1:9900"\n' % (Path(home) / "state"),
        encoding="utf-8",
    )
    env = {"HOME": str(home), "MYPEOPLE_CONFIG_PATH": str(config),
           "MYPEOPLE_HOME": str(Path(home) / "state"), "HOST_ID": "test-node",
           "QUEUE_URL": "http://127.0.0.1:9900"}
    with mock.patch.dict(os.environ, env, clear=False):
        sys.modules.pop("mpcommon", None)
        sys.path.insert(0, str(BIN))
        try:
            loader = importlib.machinery.SourceFileLoader("mpcommon", str(BIN / "mpcommon.py"))
            spec = importlib.util.spec_from_loader("mpcommon", loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(BIN))


class FakeTmux:
    """Records tmux argv. has-session succeeds; capture-pane returns whatever the test stages."""

    def __init__(self, pane_text=""):
        self.calls = []
        self.stdin = []
        self.pane_text = pane_text

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.stdin.append(kw.get("input"))
        rc, out = 0, ""
        if "capture-pane" in argv:
            out = self.pane_text
        return mock.Mock(returncode=rc, stdout=out, stderr="")

    def sent(self):
        return [c for c in self.calls if c[:2] == ["tmux", "send-keys"]]

    def enters(self):
        return [c for c in self.sent() if c[-1] == "Enter"]

    def literals(self):
        return [c[-1] for c in self.sent() if "-l" in c]

    def buffered(self):
        """Message bodies handed to `load-buffer` on stdin -- the delivery path."""
        return [self.stdin[i].decode("utf-8")
                for i, c in enumerate(self.calls)
                if c[:2] == ["tmux", "load-buffer"] and self.stdin[i] is not None]

    def pastes(self):
        return [c for c in self.calls if c[:2] == ["tmux", "paste-buffer"]]


class TmuxSendMessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.C = load_mpcommon(self.tmp.name)

    def send(self, message, pane_text=""):
        fake = FakeTmux(pane_text)
        with mock.patch.object(self.C.subprocess, "run", fake), \
             mock.patch.object(self.C.time, "sleep"):
            ok = self.C.tmux_send_message("mc-main:eng-1", message)
        return ok, fake

    def test_empty_message_sends_nothing(self):
        """A blank message must never reach the pane: a bare Enter submits whatever the agent
        had half-typed in its composer."""
        for blank in ("", "   ", "\n", None):
            ok, fake = self.send(blank)
            self.assertFalse(ok, "blank %r should report failure" % (blank,))
            self.assertEqual(fake.sent(), [], "blank %r sent keys" % (blank,))

    def test_single_line_submits_exactly_once(self):
        """The regression: an unconditional second Enter submits the composer twice, so the
        agent receives a spurious empty prompt after every single-line message."""
        ok, fake = self.send("status please")
        self.assertTrue(ok)
        self.assertEqual(fake.buffered(), ["status please"])
        self.assertEqual(len(fake.enters()), 1)

    def test_the_message_never_rides_in_as_a_send_keys_argument(self):
        """card 5676f76673. `send-keys -l` carried no bracketed-paste framing, so a composer
        read every newline as Enter and submitted one message as fragments -- the agent kept
        only the tail. It also passed the body as a command ARGUMENT, which tmux refuses past
        ~17KB ("command too long"), dropping the message whole. Delivery must go through the
        buffer, and the paste must be bracketed (-p)."""
        msg = "para one\n\npara two\n\npara three"
        ok, fake = self.send(msg)
        self.assertTrue(ok)
        self.assertEqual(fake.buffered(), [msg])
        self.assertEqual(fake.literals(), [])
        self.assertEqual(len(fake.pastes()), 1)
        self.assertIn("-p", fake.pastes()[0])

    def test_single_line_ignores_a_stale_paste_marker(self):
        """capture-pane reads 30 lines of scrollback, so a marker from an earlier multi-line
        paste is still visible. A single-line send must not inspect the pane at all."""
        ok, fake = self.send("status please", pane_text="> [Pasted text +9 lines]")
        self.assertTrue(ok)
        self.assertEqual(len(fake.enters()), 1)
        self.assertEqual([c for c in fake.calls if "capture-pane" in c], [])

    def test_multiline_retries_only_when_paste_marker_remains(self):
        ok, fake = self.send("line one\nline two", pane_text="> [Pasted text +2 lines]")
        self.assertTrue(ok)
        self.assertEqual(len(fake.enters()), 2)

    def test_multiline_does_not_retry_when_composer_cleared(self):
        ok, fake = self.send("line one\nline two", pane_text="> ready")
        self.assertTrue(ok)
        self.assertEqual(len(fake.enters()), 1)

    def test_never_targets_the_calling_pane(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,123,4"}, clear=False):
            fake = FakeTmux()
            with mock.patch.object(self.C.subprocess, "run", fake), \
                 mock.patch.object(self.C.time, "sleep"):
                self.C.tmux_send_message("mc-main:eng-1", "hi")
        self.assertTrue(fake.sent())


class SupervisorScriptTests(unittest.TestCase):
    """These two lines are the difference between a subsystem that runs and one that only exists."""

    def test_boss_supervisor_calls_reconcile(self):
        """mp reconcile is the only periodic caller of the reaper/reconcile subsystem; without it
        the fleet is only ever repaired by hand."""
        text = (BIN / "boss-supervisor.sh").read_text()
        self.assertIn("mp reconcile", text)
        self.assertIn("|| true", text.split("mp reconcile", 1)[1].split("\n", 1)[0],
                      "a failing reconcile must not kill the supervisor loop")

    def test_supervise_raises_descriptor_limit_before_starting_daemons(self):
        text = (BIN / "supervise.sh").read_text()
        self.assertIn("ulimit -n", text)
        self.assertLess(text.index("ulimit -n"), text.index("ensure "),
                        "ulimit must be raised before any daemon is spawned")


class TtydRecycleTests(unittest.TestCase):
    """ttyd 1.7.7 leaks a pty master fd per child it fails to reap. macOS caps ptys at 511, so a
    viewer left up for days locks every terminal on the machine out. The recycle guard has to fire
    on unreaped children ONLY -- keying it on total children would kill a healthy viewer as soon as
    the fleet outgrew the threshold."""

    FN = re.search(r"^recycle_leaked_ttyd\(\)\{.*?^\}",
                   (BIN / "supervise.sh").read_text(), re.S | re.M)

    def run_guard(self, ps_output, threshold=20):
        """Run the real shell function against a fabricated process table. ps/pgrep are PATH stubs;
        kill is shadowed by a bash function, because a PATH stub would lose to the builtin and the
        test would signal a real pid 4242 on the machine running it."""
        self.assertIsNotNone(self.FN, "recycle_leaked_ttyd missing from supervise.sh")
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp)
            (stub / "pgrep").write_text("#!/bin/sh\necho 4242\n")
            (stub / "ps").write_text("#!/bin/sh\ncat %s\n" % (stub / "ps.out"))
            (stub / "ps.out").write_text(ps_output)
            for name in ("pgrep", "ps"):
                (stub / name).chmod(0o755)
            script = 'LOG=%s\nTTYD_MAX_DEAD_CHILDREN=%d\nkill(){ echo "$@" >> %s; }\n%s\nrecycle_leaked_ttyd "ttyd -a -p 7691"\n' % (
                tmp, threshold, stub / "killed", self.FN.group(0))
            subprocess.run(["bash", "-c", script], check=True,
                           env={**os.environ, "PATH": "%s:%s" % (stub, os.environ["PATH"])})
            killed = stub / "killed"
            return killed.read_text().split() if killed.exists() else []

    def test_recycles_a_ttyd_drowning_in_unreaped_children(self):
        dead = "4242 ?Es\n" * 30
        self.assertEqual(self.run_guard(dead), ["4242"])

    def test_spares_a_busy_viewer_whose_children_are_all_live(self):
        """40 live tiles is a big fleet, not a leak. Killing here would flap every terminal tile."""
        live = "4242 Ss+\n" * 40 + "4242 S+\n" * 40
        self.assertEqual(self.run_guard(live), [])

    def test_ignores_dead_children_of_other_processes(self):
        self.assertEqual(self.run_guard("9999 ?Es\n" * 30), [])

    def test_the_supervisor_loop_actually_calls_the_guard(self):
        """A guard that is defined but never called is the bug shipping again."""
        text = (BIN / "supervise.sh").read_text()
        loop = text[text.index("while true; do"):]
        self.assertIn('recycle_leaked_ttyd "ttyd -W -a -p $TTYD_PORT"', loop)
        self.assertIn('recycle_leaked_ttyd "ttyd -a -p $TTYD_RO_PORT"', loop)


class BoardUiTests(unittest.TestCase):
    HTML = (BIN / "todos.html").read_text()

    def test_comments_render_markdown(self):
        for fn in ("renderMarkdown", "markdownInline", "markdownTable", "safeMarkdownHref"):
            self.assertIn(fn, self.HTML)

    def test_markdown_links_are_sanitised(self):
        self.assertIn('rel="noopener noreferrer"', self.HTML.replace("'", '"'))

    def test_board_fetch_is_guarded(self):
        """The board is ~12 MB: overlapping polls stack up, and a late response can clobber a
        just-posted comment."""
        self.assertIn("boardFetchInFlight", self.HTML)
        self.assertIn("boardReq", self.HTML)

    def test_owner_history_is_rendered(self):
        self.assertIn("ownerHistory", self.HTML)

    def test_terminal_graph_is_reachable_from_the_board(self):
        """Reachability moved from a hand-placed anchor in todos.html to the shared render seam,
        so every surface links to every other one instead of each page carrying its own partial
        list. The rendered-page assertions live in test_version_badge.PageRenderTests; here we
        only pin that the seam still owns the link."""
        nav = (BIN / "mpcommon.py").read_text()
        self.assertIn('href="/terminal-graph"', nav)
        self.assertIn('id="mp-nav"', nav)

    def test_assignee_is_not_a_free_text_field(self):
        """/todo/owner owns assignment: op=set refuses a supplied assignee, so an editable input
        would silently do nothing."""
        self.assertNotIn('patchTask(task.id,{assignee:', self.HTML.replace(" ", ""))
        self.assertIn("appendAgentLink", self.HTML)


if __name__ == "__main__":
    unittest.main()
