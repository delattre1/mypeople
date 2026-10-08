"""Card ownership: who owns a card, and who is allowed to say so.

The invariant is that /todo/owner is the ONLY door to a card's assignee, and it only opens for an
agent that is alive, unretired, reporting to THIS Boss, born for ownership, and born for THIS card.
Everything here defends one clause of that, plus the close/reopen lifecycle that keeps a terminal
card from keeping a live owner.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


def load_todo_server(home):
    """Import todo-server.py against a throwaway INSTALL_DIR, with ambient env pinned."""
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
        'export DEFAULT_BACKEND="claude"\n'
        'export DEFAULT_ENG_MODEL="claude-opus-4-8"\n' % state,
        encoding="utf-8",
    )
    with mock.patch.dict(os.environ, {
        "HOME": str(home),
        "MYPEOPLE_CONFIG_PATH": str(config),
        "MYPEOPLE_HOME": str(state),
        "INSTALL_DIR": str(state),
        "HOST_ID": "test-node",
        "QUEUE_URL": "http://127.0.0.1:1",
        "QUEUE_SECRET": "secret",
        "MYPEOPLE_BOARD_BACKEND": "json",
    }, clear=False):
        sys.modules.pop("mpcommon", None)
        sys.modules.pop("boardstore", None)
        sys.path.insert(0, str(BIN))
        try:
            name = "mypeople_test_ts_%s" % id(home)
            loader = importlib.machinery.SourceFileLoader(name, str(BIN / "todo-server.py"))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(BIN))


def task(**kw):
    base = {"id": "card1", "text": "t", "state": "working", "assignee": "",
            "ownerHistory": [], "ownerNeedsReplacement": False, "test": False}
    base.update(kw)
    return base


class OwnerStateTransitionTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ts = load_todo_server(self._td.name)

    def test_closing_a_card_kills_its_owner_through_the_queue(self):
        """The Boss may be dead or mid-crash; the kill must not depend on it (card 476fd89607)."""
        t = task(assignee="test-node/main:eng-1")
        with mock.patch.object(self.ts, "queue_kill_owner") as kill, \
             mock.patch.object(self.ts, "ping_boss"):
            self.ts.apply_owner_state_transition(t, "working", "done", "CEO")
        kill.assert_called_once()
        self.assertEqual("test-node/main:eng-1", kill.call_args[0][0])

    def test_closing_records_history_and_clears_the_replacement_flag(self):
        t = task(assignee="test-node/main:eng-1")
        with mock.patch.object(self.ts, "queue_kill_owner"), mock.patch.object(self.ts, "ping_boss"):
            self.ts.apply_owner_state_transition(t, "working", "cancelled", "CEO")
        self.assertFalse(t["ownerNeedsReplacement"])
        self.assertEqual("closed", t["ownerHistory"][-1]["action"])
        self.assertEqual("CEO", t["ownerHistory"][-1]["by"])

    def test_reopening_asks_for_a_fresh_owner_and_kills_nobody(self):
        """Reopen must not kill: the old owner is already gone, and the card needs a NEW one."""
        t = task(assignee="test-node/main:eng-1", state="working")
        with mock.patch.object(self.ts, "queue_kill_owner") as kill, \
             mock.patch.object(self.ts, "ping_boss") as ping:
            self.ts.apply_owner_state_transition(t, "done", "working", "CEO")
        kill.assert_not_called()
        self.assertTrue(t["ownerNeedsReplacement"])
        self.assertEqual("reopen_requested", t["ownerHistory"][-1]["action"])
        self.assertIn("FRESH owner", ping.call_args[0][0])

    def test_a_move_between_two_live_states_is_not_a_lifecycle_event(self):
        t = task(assignee="test-node/main:eng-1")
        with mock.patch.object(self.ts, "queue_kill_owner") as kill, \
             mock.patch.object(self.ts, "ping_boss") as ping:
            self.ts.apply_owner_state_transition(t, "working", "review", "CEO")
        kill.assert_not_called()
        ping.assert_not_called()
        self.assertEqual([], t["ownerHistory"])

    def test_a_test_card_never_touches_a_real_agent(self):
        t = task(assignee="test-node/main:eng-1", test=True)
        with mock.patch.object(self.ts, "queue_kill_owner") as kill, \
             mock.patch.object(self.ts, "ping_boss") as ping:
            self.ts.apply_owner_state_transition(t, "working", "done", "CEO")
        kill.assert_not_called()
        ping.assert_not_called()

    def test_closing_an_ownerless_card_kills_nothing(self):
        t = task(assignee="")
        with mock.patch.object(self.ts, "queue_kill_owner") as kill, \
             mock.patch.object(self.ts, "ping_boss"):
            self.ts.apply_owner_state_transition(t, "working", "done", "CEO")
        kill.assert_not_called()


class QueueKillOwnerTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ts = load_todo_server(self._td.name)

    def test_a_malformed_agent_id_is_never_submitted(self):
        """The id goes straight into a kill task; an unvalidated one is a kill on the wrong thing."""
        with mock.patch.object(self.ts.C, "http_json") as http:
            for bad in ("", "eng-1", "host/main", "a b/main:x", "host/main:x:y"):
                self.assertFalse(self.ts.queue_kill_owner(bad, "r"), bad)
        http.assert_not_called()

    def test_a_well_formed_id_is_submitted_as_a_kill(self):
        with mock.patch.object(self.ts.C, "http_json",
                               return_value=(200, {"task_id": "abc"})) as http:
            self.assertTrue(self.ts.queue_kill_owner("test-node/main:eng-1", "card-x-closed"))
        payload = http.call_args[0][2]
        self.assertEqual("kill", payload["type"])
        self.assertEqual("test-node/main:eng-1", payload["target_agent"])

    def test_a_queue_that_rejects_the_task_is_not_a_kill(self):
        with mock.patch.object(self.ts.C, "http_json", return_value=(500, {})):
            self.assertFalse(self.ts.queue_kill_owner("test-node/main:eng-1", "r"))

    def test_a_queue_that_raises_is_not_a_kill(self):
        with mock.patch.object(self.ts.C, "http_json", side_effect=OSError("down")):
            self.assertFalse(self.ts.queue_kill_owner("test-node/main:eng-1", "r"))


class LegacyOwnerMigrationTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ts = load_todo_server(self._td.name)

    def test_an_existing_owner_is_described_without_inventing_its_start_time(self):
        board = {"tasks": {"c": {"id": "c", "assignee": "test-node/main:eng-1"}}}
        self.assertTrue(self.ts.migrate_legacy_owner_fields(board))
        h = board["tasks"]["c"]["ownerHistory"]
        self.assertEqual("migrated_existing_owner", h[0]["action"])
        self.assertEqual("system", h[0]["by"])

    def test_migration_is_idempotent(self):
        board = {"tasks": {"c": {"id": "c", "assignee": "test-node/main:eng-1"}}}
        self.assertTrue(self.ts.migrate_legacy_owner_fields(board))
        self.assertFalse(self.ts.migrate_legacy_owner_fields(board))
        self.assertEqual(1, len(board["tasks"]["c"]["ownerHistory"]))

    def test_an_ownerless_card_gets_the_shape_but_no_history(self):
        board = {"tasks": {"c": {"id": "c", "assignee": ""}}}
        self.assertTrue(self.ts.migrate_legacy_owner_fields(board))
        self.assertEqual([], board["tasks"]["c"]["ownerHistory"])
        self.assertFalse(board["tasks"]["c"]["ownerNeedsReplacement"])


if __name__ == "__main__":
    unittest.main()
