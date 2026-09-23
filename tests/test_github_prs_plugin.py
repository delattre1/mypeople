"""The GitHub PR watcher plugin delivers each new review to the agent that owns the PR.

Without it an engineer opens a PR, asks for a review, and never hears the verdict. These tests
drive the plugin's logic with GitHub, the board and `mp send` stubbed, so nothing leaves the box.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "mypeople" / "runtime" / "plugins" / "github-prs" / "github-prs.py"
PR = "https://github.com/acme/app/pull/8"


def load(state_dir):
    with mock.patch.dict(os.environ, {"GITHUB_PRS_STATE_DIR": state_dir, "HOST_ID": "node",
                                      "GITHUB_PRS_AUTHOR": "fleet"}):
        loader = importlib.machinery.SourceFileLoader("github_prs_%d" % id(state_dir), str(PLUGIN))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod


def ev(key, body="looks good", kind="comment", user="reviewer", state=""):
    return {"key": key, "kind": kind, "user": user, "body": body, "state": state,
            "url": PR + "#" + key}


def card(owner, text="", state="working", updated=1, comments=()):
    return {"assignee": owner, "text": text, "state": state, "updated": updated,
            "comments": [{"body": c} for c in comments], "proofs": []}


class FreshEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_first_sighting_of_a_pr_is_silent(self):
        state = {}
        self.assertEqual([], self.m.fresh_events("acme/app#8", [ev("comment:1")], state))
        # ...and what it saw then is never replayed later
        self.assertEqual([], self.m.fresh_events("acme/app#8", [ev("comment:1")], state))

    def test_only_new_events_come_through_once(self):
        state = {}
        self.m.fresh_events("acme/app#8", [ev("comment:1")], state)
        new = self.m.fresh_events("acme/app#8", [ev("comment:1"), ev("review:2", kind="review")], state)
        self.assertEqual(["review:2"], [e["key"] for e in new])
        self.assertEqual([], self.m.fresh_events("acme/app#8", [ev("review:2", kind="review")], state))

    def test_a_bare_slash_command_is_not_echoed_back(self):
        state = {}
        self.m.fresh_events("acme/app#8", [], state)
        new = self.m.fresh_events("acme/app#8", [ev("comment:5", "/srosro-review"),
                                                 ev("comment:6", "please fix /tmp handling")], state)
        self.assertEqual(["comment:6"], [e["key"] for e in new])


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)

    def test_the_card_that_links_the_pr_owns_it(self):
        board = {"tasks": {"a": card("node/main:eng-1", comments=["PR up: " + PR]),
                           "b": card("node/main:eng-2", "unrelated")}}
        self.assertEqual("node/main:eng-1", self.m.owner_for_pr(board, PR))

    def test_pr_8_is_not_pr_80(self):
        board = {"tasks": {"a": card("node/main:eng-1", comments=[PR + "0"])}}
        self.assertEqual("", self.m.owner_for_pr(board, PR))

    def test_a_live_card_beats_a_newer_finished_one(self):
        board = {"tasks": {"old": card("node/main:eng-1", PR, state="working", updated=1),
                           "done": card("node/main:eng-9", PR, state="done", updated=99)}}
        self.assertEqual("node/main:eng-1", self.m.owner_for_pr(board, PR))

    def test_the_most_recent_live_card_wins(self):
        board = {"tasks": {"a": card("node/main:eng-1", PR, updated=1),
                           "b": card("node/main:eng-2", PR, updated=5)}}
        self.assertEqual("node/main:eng-2", self.m.owner_for_pr(board, PR))

    def test_no_card_means_no_owner(self):
        self.assertEqual("", self.m.owner_for_pr({"tasks": {}}, PR))


class FormatTests(unittest.TestCase):
    def test_a_review_carries_its_verdict_and_drops_bot_markers(self):
        m = load(tempfile.mkdtemp())
        text = m.format_event("acme/app", 8, ev("review:1", "<!-- bot:x -->Needs a test", "review",
                                                "srosro", "CHANGES_REQUESTED"), owned=True)
        self.assertIn("[PR review] acme/app#8 (your PR) by srosro — changes requested: Needs a test", text)
        self.assertNotIn("<!--", text)


class PollRoutingTests(unittest.TestCase):
    """One real poll with GitHub, the board and mp send stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        # The Boss id is read when a message is routed, not at import: keep this node's identity
        # (and none of the live install's) in place for the whole test.
        env = mock.patch.dict(os.environ, {"HOST_ID": "node", "MYPEOPLE_CONFIG_PATH": "/nonexistent"})
        env.start()
        self.addCleanup(env.stop)
        self.events = [ev("comment:1")]
        self.sent = []
        self.board = {"tasks": {"a": card("node/main:eng-1", comments=[PR])}}
        self.deliver_ok = True
        for name, fn in {
            "open_prs": lambda author: [("acme/app", 8, "t%d" % len(self.events), PR)],
            "pr_events": lambda repo, n: list(self.events),
            "fetch_board": lambda: self.board,
            "deliver": lambda agent, text: (self.sent.append((agent, text)), self.deliver_ok)[1],
        }.items():
            p = mock.patch.object(self.m, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def poll(self):
        state = self.m.load_state()
        self.m.poll("fleet", state)

    def test_a_new_review_reaches_the_owner_of_the_pr(self):
        self.poll()                                   # bootstrap: silent
        self.assertEqual([], self.sent)
        self.events.append(ev("review:2", "Please add a test", "review", "srosro", "COMMENTED"))
        self.poll()
        (agent, text), = self.sent
        self.assertEqual("node/main:eng-1", agent)
        self.assertIn("(your PR)", text)

    def test_an_unreachable_owner_falls_back_to_the_boss(self):
        self.poll()
        self.events.append(ev("comment:3"))
        self.deliver_ok = False
        self.poll()
        self.assertEqual(["node/main:eng-1", "node/main:Boss"], [a for a, _ in self.sent])

    def test_an_unreadable_board_still_delivers_to_the_boss(self):
        self.poll()
        self.events.append(ev("comment:4"))
        with mock.patch.object(self.m, "fetch_board", side_effect=OSError("down")):
            self.poll()
        self.assertEqual(["node/main:Boss"], [a for a, _ in self.sent])

    def test_state_survives_a_restart(self):
        self.poll()
        self.events.append(ev("comment:5"))
        self.poll()
        fresh = load(self.tmp.name)                   # a fresh process reads the same state file
        state = fresh.load_state()
        self.assertIn("comment:5", state["seen"])


class WiringTests(unittest.TestCase):
    def test_supervise_starts_it_only_when_turned_on(self):
        sup = (ROOT / "mypeople" / "runtime" / "bin" / "supervise.sh").read_text()
        self.assertIn('[ -n "${GITHUB_PRS:-}" ] && ensure "$ID/plugins/github-prs/github-prs.py"', sup)

    def test_down_stops_it(self):
        cli = (ROOT / "mypeople" / "cli.py").read_text()
        self.assertIn('"plugins", "github-prs", "github-prs.py"', cli)


if __name__ == "__main__":
    unittest.main()


class CatchupTests(unittest.TestCase):
    """The reviews the ordinary poll can never announce: they landed before it was watching."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = load(self.tmp.name)
        self.reviews = [{"id": 1, "user": {"login": "srosro"}, "state": "APPROVED",
                         "submitted_at": "2026-09-20T10:00:00Z", "body": "ship it",
                         "html_url": PR + "#r1"}]
        self.comments = []
        self.sent = []
        api = {"reviews": lambda: self.reviews, "comments": lambda: self.comments}
        p = mock.patch.object(self.m, "gh", lambda *a: api["reviews" if "/reviews" in a[-1] else "comments"]())
        p.start(); self.addCleanup(p.stop)
        for name, fn in {
            "open_prs": lambda author: [("acme/app", 8, "t", PR)],
            "fetch_board": lambda: {"tasks": {"a": card("node/main:eng-1", comments=[PR])}},
            "deliver": lambda agent, text: (self.sent.append((agent, text)), True)[1],
        }.items():
            q = mock.patch.object(self.m, name, fn); q.start(); self.addCleanup(q.stop)
        env = mock.patch.dict(os.environ, {"HOST_ID": "node", "MYPEOPLE_CONFIG_PATH": "/nonexistent"})
        env.start(); self.addCleanup(env.stop)

    def test_an_unanswered_review_is_reported_and_delivered_to_the_owner(self):
        found = self.m.catchup("fleet", {})
        self.assertEqual([("acme/app", 8)], [(r, n) for r, n, _ in found])
        (agent, text), = self.sent
        self.assertEqual("node/main:eng-1", agent)
        self.assertIn("[catch-up]", text)
        self.assertIn("approved", text)

    def test_a_review_we_already_answered_is_left_alone(self):
        self.comments = [{"user": {"login": "fleet"}, "created_at": "2026-09-20T11:00:00Z"}]
        self.assertEqual([], self.m.catchup("fleet", {}))
        self.assertEqual([], self.sent)

    def test_our_own_review_is_not_something_to_answer(self):
        self.reviews = [dict(self.reviews[0], user={"login": "fleet"})]
        self.assertEqual([], self.m.catchup("fleet", {}))

    def test_a_dry_run_sends_nothing(self):
        found = self.m.catchup("fleet", {}, send=False)
        self.assertEqual(1, len(found))
        self.assertEqual([], self.sent)

    def test_running_it_twice_does_not_send_twice(self):
        state = {}
        self.m.catchup("fleet", state)
        self.assertEqual(1, len(self.sent))
        # the ordinary poll must not repeat what catch-up just sent
        self.assertEqual([], self.m.fresh_events("acme/app#8", [ev("review:1")], state))

    def test_a_pr_no_card_owns_tells_the_boss_what_to_do(self):
        with mock.patch.object(self.m, "fetch_board", lambda: {"tasks": {}}):
            self.m.catchup("fleet", {})
        (agent, text), = self.sent
        self.assertEqual("node/main:Boss", agent)
        self.assertIn("no card on the board links this PR", text)
        self.assertIn("give it to an agent", text)
