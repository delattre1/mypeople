import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"


def load_module(home, filename, modname):
    config = Path(home) / "queue.env"
    config.write_text(
        'export INSTALL_DIR="%s"\n'
        'export HOST_ID="test-node"\n'
        'export QUEUE_URL="http://127.0.0.1:9900"\n'
        'export QUEUE_SECRET="secret"\n'
        'export TTYD_PORT="7681"\n'
        'export HUD_PORT="9900"\n'
        'export TODO_PORT="9933"\n'
        'export TTYD_BROWSER_PORT="7681"\n'
        'export DEFAULT_BACKEND="claude"\n'
        'export DEFAULT_ENG_MODEL="claude-opus-4-8"\n'
        'export DEFAULT_CLAUDE_MODEL="claude-opus-4-8"\n'
        'export DEFAULT_CODEX_MODEL=""\n' % (Path(home) / "state"),
        encoding="utf-8",
    )
    state = str(Path(home) / "state")
    # mpcommon lets the live environment override the config file (mpcommon.load_env: "Live env
    # overrides the file"). A test launched from an agent shell inherits that shell's exported
    # INSTALL_DIR, so setting MYPEOPLE_CONFIG_PATH alone is NOT isolation -- the module would
    # resolve to the real install and write the real board. Override the key explicitly.
    with mock.patch.dict(os.environ, {
        "HOME": str(home),
        "MYPEOPLE_CONFIG_PATH": str(config),
        "MYPEOPLE_HOME": state,
        "INSTALL_DIR": state,
    }, clear=False):
        sys.modules.pop("mpcommon", None)
        sys.path.insert(0, str(BIN))
        try:
            loader = importlib.machinery.SourceFileLoader(modname, str(BIN / filename))
            spec = importlib.util.spec_from_loader(modname, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            # Fail loudly rather than ever touching a real install.
            resolved = getattr(module, "INSTALL_DIR", state)
            assert resolved == state, "test escaped isolation: INSTALL_DIR=%s" % resolved
            return module
        finally:
            sys.path.remove(str(BIN))


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
            srv = load_module(self.tmp.name, "todo-server.py", "ts_sqlite_1")
            self.assertEqual(srv.BOARD_BACKEND, "sqlite")
            srv.save_board(sample_board())
            self.assertTrue(os.path.exists(srv._sqlite_path()), "sqlite db was not created")
            self.assertFalse(os.path.exists(srv.BOARD_PATH), "must not write the legacy JSON board")

            # a second import is a fresh process for our purposes: the board must come back off disk
            srv2 = load_module(self.tmp.name, "todo-server.py", "ts_sqlite_2")
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
            srv = load_module(self.tmp.name, "todo-server.py", "ts_seed_1")
            srv.save_board(sample_board())
            self.assertFalse(os.path.exists(srv.BOARD_PATH), "sqlite backend writes no JSON board")

            # a fresh process boots against the same state dir: this is the real restart
            srv2 = load_module(self.tmp.name, "todo-server.py", "ts_seed_2")
            self.assertTrue(srv2.board_exists(), "an initialized sqlite board must count as existing")
            self.assertFalse(srv2.seed_board_if_missing(), "boot must not seed over a live board")
            self.assertEqual(srv2.load_board()["tasks"]["a1"]["text"], "olá — açaí ☕")

    def test_boot_still_seeds_a_genuinely_fresh_install(self):
        with mock.patch.dict(os.environ, {"MYPEOPLE_BOARD_BACKEND": "sqlite"}, clear=False):
            srv = load_module(self.tmp.name, "todo-server.py", "ts_seed_3")
            self.assertFalse(srv.board_exists())
            self.assertTrue(srv.seed_board_if_missing(), "a fresh install still gets its empty board")
            self.assertEqual(srv.load_board()["tasks"], {})

    def test_json_backend_still_works(self):
        with mock.patch.dict(os.environ, {"MYPEOPLE_BOARD_BACKEND": "json"}, clear=False):
            srv = load_module(self.tmp.name, "todo-server.py", "ts_json_1")
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
            srv = load_module(self.tmp.name, "todo-server.py", "ts_exp_1")
            srv.save_board(sample_board())
            exp = load_module(self.tmp.name, "board-exporter.py", "exp_1")
            self.assertEqual(exp.BOARD_BACKEND, "sqlite")
            live = exp.read_live_board()
        self.assertIsNotNone(live, "exporter saw no board -> backups would silently stop")
        self.assertEqual(live["tasks"]["a1"]["text"], "olá — açaí ☕")


if __name__ == "__main__":
    unittest.main()
