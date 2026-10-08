"""The auth gate must not report a login that cannot answer (card 8a6ebe4e48).

Proven failure this pins shut: with an OAuth credential whose access AND refresh tokens had both
expired, `claude auth status` still answered `{"loggedIn": true}` with exit 0, so
firstrun.resolve_auth() announced `this node's claude login is active`, MyPlow started the Boss,
the HUD painted ALIVE/WORKING and the board delivered pings -- while the agent sat at
"Not logged in · Please run /login" and `claude -p` returned
`Failed to authenticate. API Error: 401 OAuth access token is invalid.`

Two things are pinned here: the gate refuses a dead credential, and a node whose credential was
refused never renders an agent as ALIVE or WORKING.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mypeople import firstrun

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"

HOUR_MS = 3600 * 1000.0


def load_authcheck():
    loader = importlib.machinery.SourceFileLoader("authcheck_under_test", str(BIN / "authcheck.py"))
    spec = importlib.util.spec_from_loader("authcheck_under_test", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


A = load_authcheck()


def write_creds(dirpath, **oauth):
    path = Path(dirpath) / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    return str(path)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class CredentialStructureTests(unittest.TestCase):
    """Layer 1: what the credential's own stamps prove, with no network."""

    def test_expired_refresh_token_is_dead(self):
        now = 1_784_000_000_000.0
        with tempfile.TemporaryDirectory() as td:
            p = write_creds(td, accessToken="x", refreshToken="y",
                            expiresAt=now - 10 * HOUR_MS,
                            refreshTokenExpiresAt=now - 5 * HOUR_MS)
            state, detail = A.claude_credential_state(path=p, now_ms=now)
        self.assertEqual(state, A.DEAD)
        self.assertIn("cannot be refreshed", detail)

    def test_open_window_is_live(self):
        now = 1_784_000_000_000.0
        with tempfile.TemporaryDirectory() as td:
            p = write_creds(td, accessToken="x", refreshToken="y",
                            expiresAt=now + HOUR_MS,
                            refreshTokenExpiresAt=now + 100 * HOUR_MS)
            state, _ = A.claude_credential_state(path=p, now_ms=now)
        self.assertEqual(state, A.LIVE)

    def test_stale_access_token_with_valid_refresh_is_not_dead(self):
        """The CLI refreshes this on its own; calling it dead would be the opposite lie."""
        now = 1_784_000_000_000.0
        with tempfile.TemporaryDirectory() as td:
            p = write_creds(td, accessToken="x", refreshToken="y",
                            expiresAt=now - HOUR_MS,
                            refreshTokenExpiresAt=now + 100 * HOUR_MS)
            state, _ = A.claude_credential_state(path=p, now_ms=now)
        self.assertEqual(state, A.LIVE)

    def test_missing_file_is_unknown_not_authenticated(self):
        state, _ = A.claude_credential_state(path="/nonexistent/.credentials.json")
        self.assertEqual(state, A.UNKNOWN)


class ProbeTests(unittest.TestCase):
    """Layer 2: the only signal a file on disk cannot fake."""

    def _probe(self, stdout="", stderr="", returncode=0):
        with mock.patch.object(A.shutil, "which", return_value="/usr/bin/claude"):
            return A.claude_probe(runner=lambda *a, **k: completed(stdout, stderr, returncode))

    def test_401_refusal_is_dead(self):
        state, detail = self._probe(
            stderr="Failed to authenticate. API Error: 401 OAuth access token is invalid.",
            returncode=1)
        self.assertEqual(state, A.DEAD)
        self.assertIn("401", detail)

    def test_expired_session_message_is_dead(self):
        state, _ = self._probe(
            stderr="OAuth session expired and could not be refreshed", returncode=1)
        self.assertEqual(state, A.DEAD)

    def test_answer_is_live(self):
        state, _ = self._probe(stdout="PONG\n", returncode=0)
        self.assertEqual(state, A.LIVE)

    def test_non_auth_failure_is_unknown(self):
        state, _ = self._probe(stderr="connect ETIMEDOUT 1.2.3.4:443", returncode=1)
        self.assertEqual(state, A.UNKNOWN)

    def test_timeout_is_unknown(self):
        with mock.patch.object(A.shutil, "which", return_value="/usr/bin/claude"):
            def boom(*a, **k):
                raise subprocess.TimeoutExpired("claude", 90)
            state, _ = A.claude_probe(runner=boom)
        self.assertEqual(state, A.UNKNOWN)


class VerdictTests(unittest.TestCase):
    def test_offline_node_with_open_window_stays_live(self):
        """A broken network must not be rewritten as an expired login."""
        now = 1_784_000_000_000.0
        with tempfile.TemporaryDirectory() as td:
            p = write_creds(td, expiresAt=now + HOUR_MS, refreshTokenExpiresAt=now + 100 * HOUR_MS)
            with mock.patch.object(A.shutil, "which", return_value="/usr/bin/claude"):
                state, detail = A.claude_verdict(
                    path=p, now_ms=now,
                    runner=lambda *a, **k: completed(stderr="getaddrinfo ENOTFOUND", returncode=1))
        self.assertEqual(state, A.LIVE)
        self.assertIn("inconclusive", detail)

    def test_revoked_token_with_open_window_is_dead(self):
        """Stamps look fine, server says no. The probe is the authority."""
        now = 1_784_000_000_000.0
        with tempfile.TemporaryDirectory() as td:
            p = write_creds(td, expiresAt=now + HOUR_MS, refreshTokenExpiresAt=now + 100 * HOUR_MS)
            with mock.patch.object(A.shutil, "which", return_value="/usr/bin/claude"):
                state, _ = A.claude_verdict(
                    path=p, now_ms=now,
                    runner=lambda *a, **k: completed(
                        stderr="Failed to authenticate. API Error: 401", returncode=1))
        self.assertEqual(state, A.DEAD)

    def test_dead_stamps_short_circuit_without_probing(self):
        now = 1_784_000_000_000.0
        calls = []
        with tempfile.TemporaryDirectory() as td:
            p = write_creds(td, refreshTokenExpiresAt=now - HOUR_MS)
            state, _ = A.claude_verdict(
                path=p, now_ms=now,
                runner=lambda *a, **k: calls.append(a) or completed("PONG"))
        self.assertEqual(state, A.DEAD)
        self.assertEqual(calls, [], "a provably dead credential must not cost a model round-trip")

    def test_probe_runs_without_the_callers_agent_id(self):
        """Run from an agent's shell, the probe's Stop hook used to ping the Boss 'finished: PONG'."""
        envs = []
        with mock.patch.dict(os.environ, {"AGENT_ID": "node/main:eng-7"}):
            A.claude_probe(runner=lambda *a, **k: envs.append(k["env"]) or completed("PONG"))
        self.assertNotIn("AGENT_ID", envs[0])
        self.assertIn("PATH", envs[0])


class FirstrunGateTests(unittest.TestCase):
    """The regression itself: `loggedIn: true` is no longer enough."""

    def _status_ok(self):
        return mock.patch.object(
            firstrun.subprocess, "run",
            return_value=completed(json.dumps({"loggedIn": True, "authMethod": "claude.ai"})))

    def _verdict(self, state, detail="test"):
        module = mock.Mock()
        module.claude_verdict.return_value = (state, detail)
        return mock.patch.object(firstrun, "_authcheck", return_value=module)

    def test_dead_credential_is_not_authenticated_despite_logged_in_true(self):
        with mock.patch.object(firstrun.shutil, "which", return_value="/usr/bin/claude"), \
             self._status_ok(), self._verdict("dead", "OAuth session expired"):
            self.assertFalse(firstrun._claude_authenticated())

    def test_live_credential_still_passes(self):
        with mock.patch.object(firstrun.shutil, "which", return_value="/usr/bin/claude"), \
             self._status_ok(), self._verdict("live"):
            self.assertTrue(firstrun._claude_authenticated())

    def test_inconclusive_probe_still_passes(self):
        with mock.patch.object(firstrun.shutil, "which", return_value="/usr/bin/claude"), \
             self._status_ok(), self._verdict("unknown"):
            self.assertTrue(firstrun._claude_authenticated())

    def test_dead_credential_produces_the_login_message(self):
        with mock.patch.object(firstrun.shutil, "which", return_value="/usr/bin/claude"), \
             self._status_ok(), self._verdict("dead"), \
             mock.patch.object(firstrun, "_codex_authenticated", return_value=False), \
             mock.patch.object(firstrun, "_grok_authenticated", return_value=False):
            ok, backend, msg = firstrun.resolve_auth("claude")
        self.assertFalse(ok)
        self.assertIn("not authenticated", msg)
        self.assertIn("claude auth login", msg)


class NodeHealthCacheTests(unittest.TestCase):
    def test_probe_is_rate_limited_but_structural_check_is_not(self):
        seen = []

        def verdict(probe=True, timeout=90):
            seen.append(probe)
            return A.LIVE, "ok"

        with tempfile.TemporaryDirectory() as td:
            A.node_auth_health(td, probe_ttl=900, now=1000.0, verdict=verdict)
            A.node_auth_health(td, probe_ttl=900, now=1010.0, verdict=verdict)
            A.node_auth_health(td, probe_ttl=900, now=5000.0, verdict=verdict)
        self.assertEqual(seen, [True, False, True],
                         "probe once, then coast on the cache until the TTL expires")

    def test_refusal_is_remembered_until_a_probe_earns_the_green_back(self):
        with tempfile.TemporaryDirectory() as td:
            A.node_auth_health(td, now=1000.0, verdict=lambda **k: (A.DEAD, "401 refused"))
            # structural-only pass right after: the file looks fine, but nothing re-proved it
            out = A.node_auth_health(td, probe_ttl=900, now=1010.0,
                                     verdict=lambda **k: (A.LIVE, "credential window open"))
            self.assertEqual(out["state"], A.DEAD)
            # TTL expired -> a real probe runs and may restore the green
            out = A.node_auth_health(td, probe_ttl=900, now=5000.0,
                                     verdict=lambda **k: (A.LIVE, "backend answered the probe"))
            self.assertEqual(out["state"], A.LIVE)


class HudHonestyTests(unittest.TestCase):
    """An agent that cannot answer must never render ALIVE or WORKING."""

    def _queue_server(self, home):
        config = Path(home) / "queue.env"
        state = Path(home) / "state"
        config.write_text(
            'export INSTALL_DIR="%s"\n'
            'export HOST_ID="test-node"\n'
            'export QUEUE_URL="http://127.0.0.1:1"\n'
            'export QUEUE_SECRET="secret"\n'
            'export TTYD_PORT="7681"\n' % state, encoding="utf-8")
        env = {"HOME": str(home), "MYPEOPLE_CONFIG_PATH": str(config),
               "MYPEOPLE_HOME": str(state), "INSTALL_DIR": str(state),
               "HOST_ID": "test-node", "QUEUE_URL": "http://127.0.0.1:1",
               "QUEUE_SECRET": "secret", "TTYD_PORT": "7681"}
        with mock.patch.dict(os.environ, env, clear=False):
            sys.modules.pop("mpcommon", None)
            sys.path.insert(0, str(BIN))
            try:
                loader = importlib.machinery.SourceFileLoader(
                    "queue_server_auth_test", str(BIN / "queue-server.py"))
                spec = importlib.util.spec_from_loader("queue_server_auth_test", loader)
                module = importlib.util.module_from_spec(spec)
                loader.exec_module(module)
                return module
            finally:
                sys.path.remove(str(BIN))

    def test_unauthenticated_agent_is_blocked_not_working(self):
        with tempfile.TemporaryDirectory() as td:
            qs = self._queue_server(td)
            rec = {"agent_id": "test-node/main:Boss", "host": "test-node", "session": "main",
                   "tab": "Boss", "backend": "claude", "state": "unauthenticated",
                   "auth_detail": "OAuth session expired and cannot be refreshed"}
            # the lifecycle hook froze this agent at "working" when the ping it can never answer
            # was delivered -- exactly the false green from the card
            with mock.patch.object(qs, "status_for", return_value={"status": "working"}):
                row = qs.agent_row("test-node/main:Boss", rec)
        self.assertEqual(row["state"], "unauthenticated")
        self.assertNotEqual(row["status"], "working")
        self.assertEqual(row["status"], "blocked")
        self.assertIn("expired", row["summary"])

    def test_authenticated_agent_is_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            qs = self._queue_server(td)
            rec = {"agent_id": "test-node/main:eng-1", "host": "test-node", "session": "main",
                   "tab": "eng-1", "backend": "claude", "state": "alive"}
            with mock.patch.object(qs, "status_for",
                                   return_value={"status": "working", "summary": "shipping"}):
                row = qs.agent_row("test-node/main:eng-1", rec)
        self.assertEqual(row["state"], "alive")
        self.assertEqual(row["status"], "working")
        self.assertEqual(row["summary"], "shipping")


if __name__ == "__main__":
    unittest.main()
