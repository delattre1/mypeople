"""Choosing the agent backend on a node with more than one live login.

resolve_auth() used to return the first authenticated backend in VALID_BACKENDS order whenever no
backend was requested. On a machine that merely had Claude installed that silently produced Claude
and never asked -- "since I have claude installed it used that for mypeople instead of chatgpt
automatically" (card 5211716904, items 8+9). List order is an implementation detail, so these tests
pin that it is never allowed to be the deciding factor on its own.
"""
import sys
import unittest
from unittest import mock

from mypeople import firstrun


def with_auth(*live):
    """Patch the per-backend login checks so only `live` backends are authenticated."""
    return mock.patch.multiple(
        firstrun,
        _claude_authenticated=mock.Mock(return_value="claude" in live),
        _codex_authenticated=mock.Mock(return_value="codex" in live),
        _grok_authenticated=mock.Mock(return_value="grok" in live),
    )


class ResolveAuthTest(unittest.TestCase):
    def test_single_login_is_used_without_asking(self):
        asked = []
        with with_auth("codex"):
            ok, backend, msg = firstrun.resolve_auth(chooser=lambda a: asked.append(a) or "")
        self.assertTrue(ok)
        self.assertEqual(backend, "codex")
        self.assertEqual(asked, [], "one option is not a choice; nobody should be prompted")

    def test_multiple_logins_defer_to_the_chooser(self):
        """The reported case: claude installed alongside codex must not win by list position."""
        seen = {}

        def chooser(available):
            seen["available"] = available
            return "codex"

        with with_auth("claude", "codex"):
            ok, backend, msg = firstrun.resolve_auth(chooser=chooser)
        self.assertTrue(ok)
        self.assertEqual(backend, "codex")
        self.assertEqual(seen["available"], ["claude", "codex"])

    def test_explicit_preference_is_never_second_guessed(self):
        asked = []
        with with_auth("claude", "codex"):
            ok, backend, _ = firstrun.resolve_auth("codex",
                                                   chooser=lambda a: asked.append(a) or "claude")
        self.assertTrue(ok)
        self.assertEqual(backend, "codex")
        self.assertEqual(asked, [], "an explicit --backend must not trigger a prompt")

    def test_explicit_preference_that_is_not_authenticated_fails(self):
        with with_auth("claude"):
            ok, backend, msg = firstrun.resolve_auth("grok")
        self.assertFalse(ok)
        self.assertIn("not authenticated", msg)

    def test_ambiguity_without_a_chooser_states_the_pick_and_the_override(self):
        """Non-interactive installs must not block -- but must not choose silently either."""
        with with_auth("claude", "codex", "grok"):
            ok, backend, msg = firstrun.resolve_auth(chooser=None)
        self.assertTrue(ok)
        self.assertEqual(backend, "claude")
        self.assertIn("claude, codex, grok", msg)
        self.assertIn("--backend", msg, "the user must be told how to override the default")

    def test_chooser_answer_outside_the_authenticated_set_is_ignored(self):
        with with_auth("claude", "codex"):
            ok, backend, msg = firstrun.resolve_auth(chooser=lambda a: "grok")
        self.assertTrue(ok)
        self.assertEqual(backend, "claude")
        self.assertIn("--backend", msg)

    def test_no_login_at_all_still_fails_closed(self):
        with with_auth():
            ok, backend, msg = firstrun.resolve_auth()
        self.assertFalse(ok)
        self.assertEqual(backend, "none")

    def test_authenticated_backends_lists_every_live_login(self):
        with with_auth("claude", "grok"):
            self.assertEqual(firstrun.authenticated_backends(), ["claude", "grok"])


class PromptBackendTest(unittest.TestCase):
    """The prompt must never hang an unattended install."""

    def test_non_tty_never_prompts(self):
        fake = mock.Mock()
        fake.isatty.return_value = False
        with mock.patch.object(sys, "stdin", fake), \
             mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.assertEqual(firstrun._prompt_backend(["claude", "codex"]), "")

    def test_tty_number_selects_that_backend(self):
        fake = mock.Mock()
        fake.isatty.return_value = True
        with mock.patch.object(sys, "stdin", fake), \
             mock.patch("builtins.input", return_value="2"), \
             mock.patch.object(firstrun, "_echo"):
            self.assertEqual(firstrun._prompt_backend(["claude", "codex"]), "codex")

    def test_tty_empty_answer_takes_the_offered_default(self):
        fake = mock.Mock()
        fake.isatty.return_value = True
        with mock.patch.object(sys, "stdin", fake), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch.object(firstrun, "_echo"):
            self.assertEqual(firstrun._prompt_backend(["claude", "codex"]), "claude")

    def test_eof_on_a_claimed_tty_does_not_explode(self):
        """A closed stdin mid-install must degrade to 'no answer', not crash the installer."""
        fake = mock.Mock()
        fake.isatty.return_value = True
        with mock.patch.object(sys, "stdin", fake), \
             mock.patch("builtins.input", side_effect=EOFError), \
             mock.patch.object(firstrun, "_echo"):
            self.assertEqual(firstrun._prompt_backend(["claude", "codex"]), "")


if __name__ == "__main__":
    unittest.main()
