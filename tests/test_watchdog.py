"""Watchdog deferred jobs: nudging when a card goes unanswered or its owner never speaks.

The gate is re-checked at fire time, not schedule time, so the interesting cases are all about
what happened in between. A gate that is too eager nudges the Watchdog about cards that are fine
(noise the CEO pays for); one that is too shy is the whole reason the subsystem exists.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"
WATCHDOG = "test-node/watchdog:Watchdog"
BOSS = "test-node/main:Boss"


def load_todo_server(home):
    state = Path(home) / "state"
    (state / "todos").mkdir(parents=True, exist_ok=True)
    config = Path(home) / "queue.env"
    config.write_text(
        'export INSTALL_DIR="%s"\n'
        'export HOST_ID="test-node"\n'
        'export QUEUE_URL="http://127.0.0.1:1"\n'
        'export QUEUE_SECRET="secret"\n'
        'export TTYD_PORT="7681"\n'
        'export HUD_PORT="9901"\n'
        'export TODO_PORT="9933"\n'
        'export DEFAULT_ENG_MODEL="claude-opus-4-8"\n' % state,
        encoding="utf-8",
    )
    with mock.patch.dict(os.environ, {
        "HOME": str(home), "MYPEOPLE_CONFIG_PATH": str(config), "MYPEOPLE_HOME": str(state),
        "INSTALL_DIR": str(state), "HOST_ID": "test-node", "QUEUE_URL": "http://127.0.0.1:1",
        "QUEUE_SECRET": "secret", "MYPEOPLE_BOARD_BACKEND": "json",
    }, clear=False):
        sys.modules.pop("mpcommon", None)
        sys.modules.pop("boardstore", None)
        sys.path.insert(0, str(BIN))
        try:
            name = "mypeople_test_wd_%s" % id(home)
            loader = importlib.machinery.SourceFileLoader(name, str(BIN / "todo-server.py"))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(BIN))


def comment(by, cid="c1"):
    return {"id": cid, "by": by, "kind": "comment", "body": "x", "ts": time.time()}


class UnansweredGateTests(unittest.TestCase):
    """Fires when the LAST word on the card is the CEO's (or the Watchdog's) and nobody replied."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ts = load_todo_server(self._td.name)
        self.job = {"kind": "unanswered", "card": "c", "comment_id": "c1", "by": "CEO"}

    def holds(self, **task):
        base = {"id": "c", "state": "working", "assignee": "", "comments": []}
        base.update(task)
        return self.ts.wd_gate_holds(base, self.job)

    def test_an_unanswered_ceo_comment_fires(self):
        self.assertTrue(self.holds(comments=[comment("CEO")]))

    def test_an_answered_ceo_comment_does_not_fire(self):
        """Somebody replied after the CEO -- that is the whole point, so stay quiet."""
        self.assertFalse(self.holds(comments=[comment("CEO"), comment("test-node/main:eng-1")]))

    def test_the_watchdogs_own_unanswered_comment_fires(self):
        self.assertTrue(self.holds(comments=[comment(WATCHDOG)]))

    def test_a_card_with_no_comments_never_fires(self):
        self.assertFalse(self.holds(comments=[]))

    def test_a_deleted_card_never_fires(self):
        self.assertFalse(self.ts.wd_gate_holds(None, self.job))


class TaskCreateGateTests(unittest.TestCase):
    """Fires when the card's OWNER still has not said the first word."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ts = load_todo_server(self._td.name)
        self.job = {"kind": "taskcreate", "card": "c", "comment_id": None, "by": "CEO"}

    def holds(self, **task):
        base = {"id": "c", "state": "working", "assignee": "", "comments": []}
        base.update(task)
        return self.ts.wd_gate_holds(base, self.job)

    def test_an_unowned_card_fires(self):
        self.assertTrue(self.holds(assignee="", comments=[]))

    def test_an_owned_but_silent_card_fires(self):
        """The real failure: Boss triages in seconds, so a comments==0 gate never catches this."""
        self.assertTrue(self.holds(assignee="test-node/main:eng-1", comments=[comment(BOSS)]))

    def test_an_owner_who_spoke_does_not_fire(self):
        self.assertFalse(self.holds(assignee="test-node/main:eng-1",
                                    comments=[comment(BOSS), comment("test-node/main:eng-1")]))

    def test_a_done_card_never_fires(self):
        """Terminal cards need no nudge, however silent their owner was."""
        self.assertFalse(self.holds(state="done", assignee="test-node/main:eng-1"))
        self.assertFalse(self.holds(state="cancelled", assignee=""))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ts = load_todo_server(self._td.name)

    def test_only_the_ceo_or_watchdog_schedules_an_unanswered_job(self):
        """An engineer's comment must not arm a nudge about itself."""
        self.ts.wd_schedule_unanswered("c", "c1", "test-node/main:eng-1")
        self.assertEqual([], self.ts.wd_load_store()["jobs"])
        self.ts.wd_schedule_unanswered("c", "c1", "CEO")
        self.assertEqual(1, len(self.ts.wd_load_store()["jobs"]))

    def test_a_second_comment_supersedes_the_pending_job(self):
        """Two CEO comments must mean one nudge about the latest, not two about both."""
        self.ts.wd_schedule_unanswered("c", "c1", "CEO")
        self.ts.wd_schedule_unanswered("c", "c2", "CEO")
        jobs = self.ts.wd_load_store()["jobs"]
        self.assertEqual(1, len(jobs))
        self.assertEqual("c2", jobs[0]["comment_id"])

    def test_the_two_kinds_coexist_on_one_card(self):
        self.ts.wd_schedule_taskcreate("c", "working")
        self.ts.wd_schedule_unanswered("c", "c1", "CEO")
        self.assertEqual({"taskcreate", "unanswered"},
                         {j["kind"] for j in self.ts.wd_load_store()["jobs"]})

    def test_a_corrupt_store_does_not_take_the_server_down(self):
        with open(self.ts.JOBS_PATH, "w") as fh:
            fh.write("{not json")
        self.assertEqual({"version": 1, "jobs": []}, self.ts.wd_load_store())

    def test_junk_jobs_are_dropped_on_load(self):
        with open(self.ts.JOBS_PATH, "w") as fh:
            json.dump({"version": 1, "jobs": [
                {"card": "ok", "fire_at": 1.0, "kind": "unanswered"},
                {"card": "no-fire-at", "kind": "unanswered"},
                {"card": "bad-kind", "fire_at": 1.0, "kind": "nonsense"},
                "not-even-a-dict",
            ]}, fh)
        jobs = self.ts.wd_load_store()["jobs"]
        self.assertEqual(["ok"], [j["card"] for j in jobs])


class ResolveDueTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ts = load_todo_server(self._td.name)

    def board(self, **task):
        base = {"id": "c", "state": "working", "assignee": "", "comments": [comment("CEO")]}
        base.update(task)
        return {"tasks": {"c": base}}

    def test_a_due_job_fires_once_and_leaves_the_store(self):
        """Jobs are popped BEFORE dispatch, so a concurrent scan cannot double-send."""
        store = {"version": 1, "jobs": [
            {"id": "j", "card": "c", "comment_id": "c1", "by": "CEO", "fire_at": 1.0,
             "kind": "unanswered"}]}
        fire = self.ts.wd_resolve_due(store, self.board(), 100.0)
        self.assertEqual(1, len(fire))
        self.assertEqual([], store["jobs"])

    def test_a_job_that_is_not_due_stays(self):
        store = {"version": 1, "jobs": [
            {"id": "j", "card": "c", "comment_id": "c1", "by": "CEO", "fire_at": 999.0,
             "kind": "unanswered"}]}
        self.assertEqual([], self.ts.wd_resolve_due(store, self.board(), 100.0))
        self.assertEqual(1, len(store["jobs"]))

    def test_a_job_whose_gate_went_stale_is_cancelled_not_fired(self):
        """Answered between schedule and fire: drop it silently rather than nudge."""
        store = {"version": 1, "jobs": [
            {"id": "j", "card": "c", "comment_id": "c1", "by": "CEO", "fire_at": 1.0,
             "kind": "unanswered"}]}
        answered = self.board(comments=[comment("CEO"), comment("test-node/main:eng-1")])
        self.assertEqual([], self.ts.wd_resolve_due(store, answered, 100.0))
        self.assertEqual([], store["jobs"])

    def test_a_job_for_a_deleted_card_is_dropped(self):
        store = {"version": 1, "jobs": [
            {"id": "j", "card": "gone", "comment_id": "c1", "by": "CEO", "fire_at": 1.0,
             "kind": "unanswered"}]}
        self.assertEqual([], self.ts.wd_resolve_due(store, self.board(), 100.0))
        self.assertEqual([], store["jobs"])


class IncidentTextTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ts = load_todo_server(self._td.name)

    def test_the_unowned_wording_is_verbatim(self):
        """The Watchdog persona keys its nudge off this exact phrase -- do not reword."""
        job = {"kind": "taskcreate", "card": "c", "by": "CEO"}
        text = self.ts.wd_incident_text(job, {"id": "c", "assignee": "", "text": "ship it"})
        self.assertIn("new task unowned", text)

    def test_an_owned_silent_card_names_the_owner(self):
        job = {"kind": "taskcreate", "card": "c", "by": "CEO"}
        text = self.ts.wd_incident_text(
            job, {"id": "c", "assignee": "test-node/main:eng-1", "text": "ship it"})
        self.assertIn("owner assigned but silent", text)
        self.assertIn("test-node/main:eng-1", text)

    def test_an_unanswered_incident_quotes_the_comment_it_was_armed_for(self):
        job = {"kind": "unanswered", "card": "c", "comment_id": "c1", "by": "CEO"}
        t = {"id": "c", "comments": [{"id": "c1", "by": "CEO", "body": "THE-QUOTED-ONE"},
                                     {"id": "c2", "by": "CEO", "body": "later"}]}
        self.assertIn("THE-QUOTED-ONE", self.ts.wd_incident_text(job, t))


if __name__ == "__main__":
    unittest.main()
