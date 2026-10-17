import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import sandbox


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


def sample_board():
    return {"version": 2, "order": ["a1"], "pinSeq": 1,
            "tasks": {"a1": {"id": "a1", "text": "olá — açaí ☕", "state": "working",
                             "comments": [{"id": "c1", "by": "CEO", "body": "ship it"}]}}}


class BackendSelectorTests(unittest.TestCase):
    """The rule that decides where the board lives. Every board daemon shares it, so a wrong
    answer here means the server and the exporter disagree and backups silently stop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sys.path.insert(0, str(BIN))
        self.addCleanup(lambda: sys.path.remove(str(BIN)) if str(BIN) in sys.path else None)
        import boardstore
        self.BS = boardstore
        self.board_path = os.path.join(self.tmp.name, "board.v2.json")

    def test_fresh_install_defaults_to_sqlite(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MYPEOPLE_BOARD_BACKEND", None)
            self.assertEqual(self.BS.select_backend(self.board_path), "sqlite")

    def test_existing_json_board_is_never_silently_abandoned(self):
        # An upgraded install with a JSON board and no db must stay on json: switching it would
        # show an empty board, which reads as total data loss.
        Path(self.board_path).write_text(json.dumps(sample_board()), encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MYPEOPLE_BOARD_BACKEND", None)
            self.assertEqual(self.BS.select_backend(self.board_path), "json")

    def test_explicit_config_and_env_win(self):
        Path(self.board_path).write_text(json.dumps(sample_board()), encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MYPEOPLE_BOARD_BACKEND", None)
            self.assertEqual(self.BS.select_backend(self.board_path, "sqlite"), "sqlite")
        with mock.patch.dict(os.environ, {"MYPEOPLE_BOARD_BACKEND": "json"}, clear=False):
            self.assertEqual(self.BS.select_backend(self.board_path, "sqlite"), "json")


class SqliteBoardRoundTripTests(unittest.TestCase):
    """todo-server's board really persists through SQLite, including non-ASCII and nested comments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_board_survives_a_server_restart_on_sqlite(self):
        with mock.patch.dict(os.environ, {"MYPEOPLE_BOARD_BACKEND": "sqlite"}, clear=False):
            srv = sandbox.load(self.tmp.name, "todo-server.py")
            self.assertEqual(srv.BOARD_BACKEND, "sqlite")
            srv.save_board(sample_board())
            self.assertTrue(os.path.exists(srv._sqlite_path()), "sqlite db was not created")
            self.assertFalse(os.path.exists(srv.BOARD_PATH), "must not write the legacy JSON board")

            # a second import is a fresh process for our purposes: the board must come back off disk
            srv2 = sandbox.load(self.tmp.name, "todo-server.py")
            got = srv2.load_board()
        self.assertEqual(got["tasks"]["a1"]["text"], "olá — açaí ☕")
        self.assertEqual(got["tasks"]["a1"]["comments"][0]["body"], "ship it")
        self.assertEqual(got["order"], ["a1"])

    def test_boot_never_seeds_over_an_existing_sqlite_board(self):
        """Regression: a restart wiped the board of every new user.

        todo-server.main() decided "fresh install" with os.path.exists(BOARD_PATH) -- the JSON path,
        which never exists under the sqlite backend. So every boot seeded an empty board over the
        live one. It stayed invisible because boardstore's shrink guard refuses a >50% shrink only
        for boards with more than 5 cards: busy boards survived, and a new user with one or two
        cards lost everything. The round-trip test above missed it by calling load_board() directly
        and never running the boot path.
        """
        with mock.patch.dict(os.environ, {"MYPEOPLE_BOARD_BACKEND": "sqlite"}, clear=False):
            srv = sandbox.load(self.tmp.name, "todo-server.py")
            srv.save_board(sample_board())
            self.assertFalse(os.path.exists(srv.BOARD_PATH), "sqlite backend writes no JSON board")

            # a fresh process boots against the same state dir: this is the real restart
            srv2 = sandbox.load(self.tmp.name, "todo-server.py")
            self.assertTrue(srv2.board_exists(), "an initialized sqlite board must count as existing")
            self.assertFalse(srv2.seed_board_if_missing(), "boot must not seed over a live board")
            self.assertEqual(srv2.load_board()["tasks"]["a1"]["text"], "olá — açaí ☕")

    def test_boot_still_seeds_a_genuinely_fresh_install(self):
        with mock.patch.dict(os.environ, {"MYPEOPLE_BOARD_BACKEND": "sqlite"}, clear=False):
            srv = sandbox.load(self.tmp.name, "todo-server.py")
            self.assertFalse(srv.board_exists())
            self.assertTrue(srv.seed_board_if_missing(), "a fresh install still gets its empty board")
            self.assertEqual(srv.load_board()["tasks"], {})

    def test_json_backend_still_works(self):
        with mock.patch.dict(os.environ, {"MYPEOPLE_BOARD_BACKEND": "json"}, clear=False):
            srv = sandbox.load(self.tmp.name, "todo-server.py")
            self.assertEqual(srv.BOARD_BACKEND, "json")
            srv.save_board(sample_board())
            self.assertTrue(os.path.exists(srv.BOARD_PATH))
            self.assertEqual(srv.load_board()["tasks"]["a1"]["text"], "olá — açaí ☕")


class ExporterAgreesWithServerTests(unittest.TestCase):
    """The exporter must read the same board the server writes -- otherwise backups stop silently."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_exporter_reads_the_sqlite_board(self):
        with mock.patch.dict(os.environ, {"MYPEOPLE_BOARD_BACKEND": "sqlite"}, clear=False):
            srv = sandbox.load(self.tmp.name, "todo-server.py")
            srv.save_board(sample_board())
            exp = sandbox.load(self.tmp.name, "board-exporter.py")
            self.assertEqual(exp.BOARD_BACKEND, "sqlite")
            live = exp.read_live_board()
        self.assertIsNotNone(live, "exporter saw no board -> backups would silently stop")
        self.assertEqual(live["tasks"]["a1"]["text"], "olá — açaí ☕")


if __name__ == "__main__":
    unittest.main()
