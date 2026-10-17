"""Import packaged runtime scripts as fresh modules, pointed at a throwaway install.

mpcommon lets the ambient process env OVERRIDE the config file, so every mypeople key is pinned in
both. A test launched from an agent shell otherwise inherits its live HOST_ID/QUEUE_URL/INSTALL_DIR,
decides test agent_ids are REMOTE and dispatches real tasks at the running queue (it has left a
`test-node/main:eng-1` fossil in a live roster), or writes the real board. QUEUE_URL is a closed
port so a regression can never reach a live daemon.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"
# Pinned in the env as well as the file, because the live env outranks the file. The rest stay in
# the file only, where a test can still override one through its own env.
PINNED = {"HOST_ID": "test-node", "QUEUE_URL": "http://127.0.0.1:1", "QUEUE_SECRET": "secret",
          "DEFAULT_BACKEND": "claude"}
DEFAULTS = {"TTYD_PORT": "7681", "HUD_PORT": "9901", "TODO_PORT": "9933",
            "DEFAULT_ENG_MODEL": "claude-opus-4-8", "DEFAULT_CLAUDE_MODEL": "claude-opus-4-8",
            "DEFAULT_CODEX_MODEL": "", "DEFAULT_GROK_MODEL": ""}


def load_file(path, name, env=()):
    """Execute a script file (no .py suffix needed) as a new module, with env patched meanwhile."""
    with mock.patch.dict(os.environ, dict(env)):
        loader = importlib.machinery.SourceFileLoader(name, str(path))
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        loader.exec_module(module)
    return module


def load(home, filename="mp", **keys):
    """Load bin/<filename> against <home>/state. **keys are pinned like PINNED."""
    state = Path(home) / "state"
    (state / "todos").mkdir(parents=True, exist_ok=True)
    keys = dict(PINNED, INSTALL_DIR=str(state), **keys)
    config = Path(home) / "queue.env"
    config.write_text("".join('export %s="%s"\n' % kv for kv in dict(DEFAULTS, **keys).items()),
                      encoding="utf-8")
    for cached in ("mpcommon", "mprole", "boardstore"):
        sys.modules.pop(cached, None)
    sys.path.insert(0, str(BIN))
    try:
        module = load_file(BIN / filename, "mypeople_test_%s_%d" % (filename.split(".")[0], id(home)),
                           dict(keys, HOME=str(home), MYPEOPLE_CONFIG_PATH=str(config),
                                MYPEOPLE_HOME=str(state)))
    finally:
        sys.path.remove(str(BIN))
    resolved = getattr(module, "INSTALL_DIR", str(state))
    assert resolved == str(state), "test escaped isolation: INSTALL_DIR=%s" % resolved
    return module


def load_mpcommon_standalone(install):
    """Import mpcommon the way a daemon does: standalone, off INSTALL_DIR, not as part of the
    package. Re-imported per test because CFG/version are resolved at import time."""
    # never let a developer's real ~/.config/mypeople/queue.env leak into the test
    env = {"INSTALL_DIR": install, "MYPEOPLE_CONFIG_PATH": os.path.join(install, "config", "queue.env")}
    with mock.patch.dict(os.environ, env):
        os.environ.pop("MYPEOPLE_VERSION", None)
        sys.path.insert(0, str(BIN))
        sys.modules.pop("mpcommon", None)
        try:
            import mpcommon
            return mpcommon
        finally:
            sys.path.remove(str(BIN))
