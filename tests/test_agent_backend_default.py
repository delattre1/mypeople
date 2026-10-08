"""New agents and the Boss have separate default backends.

The CEO wanted new agents on Grok 4.7 while his Boss stays where it is. DEFAULT_BACKEND could not
carry that: `mp ensure-boss` (run by boss-supervisor every ~15s) counts the Boss alive only if its
window runs DEFAULT_BACKEND, so flipping it to grok would make the supervisor "recover" a healthy
Claude Boss. DEFAULT_AGENT_BACKEND is the knob for new agents; unset, it means DEFAULT_BACKEND.

Spawns are aimed at another node so do_spawn hands its resolved backend to remote_task before any
tmux or roster work — the test reads the decision without creating anything.
"""
import os
import tempfile
import unittest
from unittest import mock

from test_backends import load_mp

REMOTE = "other-node/main:%s"
KEYS = ("DEFAULT_AGENT_BACKEND", "DEFAULT_GROK_MODEL")


def env_with(**cfg):
    """The ambient env minus our keys, plus the test's own. An agent shell inherits the live
    install's config, so without the pop an "unset" test would read the live value."""
    env = {k: v for k, v in os.environ.items() if k not in KEYS}
    env.update(cfg)
    return mock.patch.dict(os.environ, env, clear=True)


class AgentBackendDefaultTests(unittest.TestCase):
    def spawn_backend(self, argv, **cfg):
        """The backend do_spawn resolves for `argv`, with extra config keys in the env."""
        with tempfile.TemporaryDirectory() as td, env_with(**cfg):
            mp = load_mp(td)
            with mock.patch.object(mp, "remote_task", return_value=(True, "ok")) as remote:
                self.assertEqual(0, mp.do_spawn(argv))
            (_, _, payload), _ = remote.call_args
            return payload["backend"]

    def test_new_agents_use_the_agent_default(self):
        self.assertEqual("grok", self.spawn_backend(
            [REMOTE % "eng-1", "--boss", "other-node/main:Boss"],
            DEFAULT_AGENT_BACKEND="grok"))

    def test_the_boss_keeps_the_node_default(self):
        """The whole point: new agents moving to grok must not move the Boss."""
        self.assertEqual("claude", self.spawn_backend(
            [REMOTE % "Boss", "--master", "--role", "boss"],
            DEFAULT_AGENT_BACKEND="grok"))

    def test_an_explicit_backend_always_wins(self):
        # This is also how revive keeps an existing agent on the backend its session lives in:
        # do_revive passes the recorded --backend, so no default can move a revived agent.
        self.assertEqual("claude", self.spawn_backend(
            [REMOTE % "eng-2", "--backend", "claude"], DEFAULT_AGENT_BACKEND="grok"))

    def test_unset_means_the_same_as_before(self):
        self.assertEqual("claude", self.spawn_backend([REMOTE % "eng-3"]))

    def test_the_value_is_normalised(self):
        self.assertEqual("grok", self.spawn_backend(
            [REMOTE % "eng-4"], DEFAULT_AGENT_BACKEND=" Grok "))

    def test_new_grok_agents_get_the_grok_model(self):
        with tempfile.TemporaryDirectory() as td, \
             env_with(DEFAULT_AGENT_BACKEND="grok", DEFAULT_GROK_MODEL="grok-4.7"):
            mp = load_mp(td)
            with mock.patch.object(mp, "remote_task", return_value=(True, "ok")) as remote:
                mp.do_spawn([REMOTE % "eng-5"])
            (_, _, payload), _ = remote.call_args
        self.assertEqual(("grok", "grok-4.7"), (payload["backend"], payload["model"]))

    def test_an_edited_queue_env_reaches_a_spawn_from_a_stale_env(self):
        """The Boss's env is a launch-time snapshot; the file is what the owner edits."""
        with tempfile.TemporaryDirectory() as td, \
             env_with(DEFAULT_AGENT_BACKEND="grok", DEFAULT_GROK_MODEL="grok-4.7"):
            mp = load_mp(td)
            conf = os.path.join(td, "queue.env")
            with open(conf) as f:
                text = f.read().replace('DEFAULT_GROK_MODEL=""', 'DEFAULT_GROK_MODEL="grok-5"')
            with open(conf, "w") as f:
                f.write(text)
            with mock.patch.object(mp, "remote_task", return_value=(True, "ok")) as remote:
                mp.do_spawn([REMOTE % "eng-6"])
            (_, _, payload), _ = remote.call_args
        self.assertEqual("grok-5", payload["model"])


if __name__ == "__main__":
    unittest.main()
