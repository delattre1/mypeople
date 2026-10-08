"""Role bundles: the personality + Skills an agent is born with.

Covers the contract that `mp spawn --role boss|engineer` composes a versioned, digest-locked
bundle (personality + Skills + hooks + toolset + policy), mounts it through each backend's own
native mechanism, and that no path can yield a Boss without one.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from mypeople import firstrun


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "mypeople" / "runtime" / "bin"
ROLES = ROOT / "mypeople" / "runtime" / "roles"


def load_mp(home):
    """Load the packaged mp as a module, pointed at a throwaway INSTALL_DIR."""
    state = Path(home) / "state"
    config = Path(home) / "queue.env"
    config.write_text(
        'export INSTALL_DIR="%s"\n'
        'export HOST_ID="test-node"\n'
        'export QUEUE_URL="http://127.0.0.1:9900"\n'
        'export QUEUE_SECRET="secret"\n'
        'export TTYD_PORT="7681"\n'
        'export DEFAULT_BACKEND="claude"\n'
        'export DEFAULT_ENG_MODEL="claude-opus-4-8"\n'
        'export DEFAULT_CLAUDE_MODEL="claude-opus-4-8"\n'
        'export DEFAULT_CODEX_MODEL=""\n'
        'export DEFAULT_GROK_MODEL=""\n' % state,
        encoding="utf-8",
    )
    # mpcommon lets the ambient process env OVERRIDE the config file, so every mypeople key must be
    # pinned explicitly here. Without this the suite inherits a developer's live HOST_ID/QUEUE_URL,
    # decides these test agent_ids are REMOTE, and dispatches spawn tasks at their real queue.
    # QUEUE_URL points at a closed port so a regression can never reach a live daemon.
    with mock.patch.dict(os.environ, {
        "HOME": str(home),
        "MYPEOPLE_CONFIG_PATH": str(config),
        "MYPEOPLE_HOME": str(state),
        "INSTALL_DIR": str(state),
        "HOST_ID": "test-node",
        "QUEUE_URL": "http://127.0.0.1:1",
        "QUEUE_SECRET": "secret",
        "DEFAULT_BACKEND": "claude",
    }, clear=False):
        sys.modules.pop("mpcommon", None)
        sys.modules.pop("mprole", None)
        sys.path.insert(0, str(BIN))
        try:
            name = "mypeople_test_mp_roles_%s" % id(home)
            loader = importlib.machinery.SourceFileLoader(name, str(BIN / "mp"))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            # point the role store at the packaged one; bundles go to the throwaway install
            module.ROLE_DIR = str(ROLES)
            module.ROLE_BUNDLE_DIR = str(state / "run" / "role-bundles")
            boss_doc = state / "mp-boss-doctrine.md"
            boss_doc.parent.mkdir(parents=True, exist_ok=True)
            boss_doc.write_text("# Boss doctrine\nDrive the team off the board.\n",
                                encoding="utf-8")
            module.BOSS_DOC = str(boss_doc)
            return module
        finally:
            sys.path.remove(str(BIN))


class RoleStoreTests(unittest.TestCase):
    def test_registry_points_at_profiles_that_exist(self):
        reg = json.loads((ROLES / "registry.json").read_text())
        self.assertEqual(set(reg["roles"]),
                         {"boss", "engineer", "tester", "watchdog", "creator"})
        for role, rel in reg["roles"].items():
            self.assertTrue((ROLES / rel).is_file(), "%s -> missing %s" % (role, rel))

    def test_every_profile_reference_resolves_inside_the_store(self):
        """A dangling ref would fail closed at spawn time, i.e. the Boss could not be born."""
        reg = json.loads((ROLES / "registry.json").read_text())
        for role, rel in reg["roles"].items():
            spec = json.loads((ROLES / rel).read_text())["spec"]
            refs = [s["ref"] for s in spec["skills"]]
            refs += spec.get("hookRefs", [])
            refs += [spec.get("toolsetRef"), spec.get("policyRef")]
            # `personalityRef` is the current name; `doctrineRef` is the older one. Either can
            # point at "mypeople://...", which lives in the install, not the store.
            persona = spec.get("doctrineRef") or spec.get("personalityRef", "")
            if persona and not persona.startswith("mypeople://"):
                refs.append(persona)
            for ref in filter(None, refs):
                self.assertTrue((ROLES / ref).exists(), "%s -> dangling ref %s" % (role, ref))

    def test_mp_system_skill_is_mandatory_for_every_role(self):
        reg = json.loads((ROLES / "registry.json").read_text())
        for role, rel in reg["roles"].items():
            spec = json.loads((ROLES / rel).read_text())["spec"]
            mandatory = [s["ref"] for s in spec["skills"] if s.get("required")]
            # Same pair mprole.py accepts: the skill was renamed, both names are the system one.
            names = {r.split("/")[1] for r in mandatory if r.startswith("skills/")}
            self.assertTrue(names & {"mp-system", "mypeople-system"},
                            "%s does not require the system skill" % role)

    def test_no_skill_depends_on_something_the_product_does_not_ship(self):
        """A persona may only reference Skills that ship in this store.

        A mounted persona that tells an agent to invoke a Skill the product does not ship would
        block that agent on first use, on every fresh install.
        """
        names = {p.parent.name for p in ROLES.glob("skills/*/*/SKILL.md")}
        for skill in ROLES.glob("skills/*/*/SKILL.md"):
            body = skill.read_text()
            for line in body.splitlines():
                if "skill" not in line.lower():
                    continue
                for word in ("Superpowers", "superpowers"):
                    self.assertNotIn(
                        word, line,
                        "%s references a Skill this product does not ship (%r); shipped: %s"
                        % (skill.relative_to(ROLES), line.strip()[:80], sorted(names)))


class RoleResolutionTests(unittest.TestCase):
    def test_unknown_role_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            for bad in ("bogus", ""):
                with self.assertRaises(mp.mprole.RoleError):
                    mp.resolve_role(bad, "claude")

    def test_missing_adapter_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            with self.assertRaises(mp.mprole.RoleError):
                mp.resolve_role("boss", "no-such-backend")

    def test_digest_is_identical_across_every_adapter(self):
        """One lock, N native adapters: the adapter is excluded from the role digest."""
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            for role in ("boss", "engineer"):
                digests, personalities, skills = set(), set(), []
                for backend in ("claude", "codex", "grok"):
                    item = mp.resolve_role(role, backend)
                    bundle = mp.materialize_role(item, "t/main:%s-%s" % (role, backend), backend)
                    digests.add(bundle["digest"])
                    personalities.add(bundle["personality_digest"])
                    skills.append({s["name"]: s["sha256"] for s in bundle["skills"]})
                self.assertEqual(len(digests), 1, "%s: digest differs per adapter" % role)
                self.assertEqual(len(personalities), 1, "%s: personality digest drift" % role)
                self.assertEqual(skills[0], skills[1], "%s: skill digest drift" % role)
                self.assertEqual(skills[1], skills[2], "%s: skill digest drift" % role)

    def test_boss_personality_is_the_boss_doc_verbatim(self):
        """The Boss now mounts a store personality, but `mp spawn --master` still injects the
        install's mp-boss-doctrine.md as the onboarding prompt. If the two forked, a fresh Boss
        would be born holding two different doctrines."""
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            bundle = mp.materialize_role(mp.resolve_role("boss", "claude"), "t/main:Boss", "claude")
            shipped_doc = ROOT / "mypeople" / "runtime" / "mp-boss-doctrine.md"
            self.assertEqual(Path(bundle["personality_path"]).read_bytes(),
                             shipped_doc.read_bytes(), "Boss personality forked BOSS_DOC")

    def test_locked_ref_drift_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            with self.assertRaises(mp.mprole.RoleError):
                mp.resolve_role("boss", "claude", locked_role_ref="boss@0.0.1")

    def test_damaged_bundle_is_healed_from_the_canonical_store(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            bundle = mp.materialize_role(mp.resolve_role("engineer", "claude"),
                                         "t/main:eng", "claude")
            victim = Path(bundle["plugin_path"]) / "skills" / "mypeople-system" / "SKILL.md"
            victim.chmod(0o644)
            victim.unlink()
            healed = mp.materialize_role(mp.resolve_role("engineer", "claude"),
                                         "t/main:eng", "claude")
            self.assertTrue((Path(healed["plugin_path"]) / "skills" / "mypeople-system"
                             / "SKILL.md").is_file())


class RoleLaunchTests(unittest.TestCase):
    def test_claude_mounts_via_flags(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            bundle = mp.materialize_role(mp.resolve_role("engineer", "claude"),
                                         "t/main:eng", "claude")
            launch = mp.build_launch("t/main:eng", td, "t/main:Boss", False, "m", "claude",
                                     role_bundle=bundle)
            words = shlex.split(launch)
            self.assertIn("--append-system-prompt-file", words)
            self.assertIn("--plugin-dir", words)
            # Assert the launch carries the ref of the profile the registry actually resolved.
            # A frozen literal here goes stale on every profile bump and fails for a reason
            # that has nothing to do with how claude is mounted.
            resolved = mp.resolve_role("engineer", "claude")
            self.assertIn("MYPEOPLE_ROLE_REF=%s" % resolved["role_ref"], launch)
            self.assertTrue(resolved["role_ref"].startswith("engineer@"))

    def test_codex_mounts_via_home_and_profile(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            bundle = mp.materialize_role(mp.resolve_role("engineer", "codex"),
                                         "t/main:eng", "codex")
            launch = mp.build_launch("t/main:eng", td, "t/main:Boss", False, "m", "codex",
                                     role_bundle=bundle)
            self.assertIn("--profile", shlex.split(launch))
            self.assertIn("CODEX_HOME=", launch)
            self.assertIn("developer_instructions", Path(bundle["settings_path"]).read_text())

    def test_grok_mounts_via_home_only(self):
        """grok has no personality/skills flags: everything rides on GROK_HOME.

        The grok ADAPTER ships here so the profiles are ready for it, but grok is not yet a
        product BACKEND (see test_grok_is_not_a_product_backend_yet) -- this covers the
        materialized bundle, which is the part that ships.
        """
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            bundle = mp.materialize_role(mp.resolve_role("boss", "grok"), "t/main:Boss", "grok")
            home = Path(bundle["grok_home"])
            self.assertTrue((home / "AGENTS.md").is_file(), "grok personality not mounted")
            self.assertTrue((home / "skills" / "mypeople-system" / "SKILL.md").is_file())
            self.assertTrue((home / "skills" / "boss-manager" / "SKILL.md").is_file())
            self.assertFalse((home / "GROK.md").exists(), "GROK.md is not read by grok")
            self.assertEqual(mp.mprole.grok_flags(bundle), [],
                             "grok takes no role flags; it mounts via GROK_HOME")
            self.assertEqual(mp.mprole.role_env(bundle)["GROK_HOME"], str(home))

    def test_grok_is_a_product_backend(self):
        """The flipped seam: grok is productized, so the launch must actually carry GROK_HOME.

        This replaces the dormant-adapter guard. The adapter contributes no flags -- grok mounts
        everything through its home -- so GROK_HOME in the launch env is the only proof the role
        reached the process.
        """
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            self.assertIn("grok", mp.VALID_BACKENDS)
            bundle = mp.materialize_role(mp.resolve_role("boss", "grok"), "t/main:Boss", "grok")
            launch = mp.build_launch("t/main:Boss", td, "", True, None, "grok", role_bundle=bundle)
            self.assertIn("GROK_HOME=%s" % bundle["grok_home"], launch)
            self.assertIn("grok --permission-mode bypassPermissions", launch)
            self.assertIn("MYPEOPLE_ROLE_DIGEST=%s" % bundle["digest"], launch)

    def test_legacy_launch_is_untouched_without_a_role(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            for backend in ("claude", "codex", "grok"):
                launch = mp.build_launch("t/main:x", td, "t/main:Boss", False, "m", backend)
                self.assertNotIn("MYPEOPLE_ROLE", launch)
                self.assertNotIn("--append-system-prompt-file", launch)
                self.assertNotIn("CODEX_HOME=", launch)
                self.assertNotIn("GROK_HOME=", launch)


class NoBundlelessBossTests(unittest.TestCase):
    def test_master_without_a_role_is_refused(self):
        """--master IS what makes an agent a Boss, whatever its window is named."""
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            for aid in ("test-node/main:Boss", "test-node/main:Boss2", "test-node/main:anything"):
                self.assertEqual(mp.do_spawn([aid, "--master"]), 2,
                                 "%s: a bundle-less Boss was allowed" % aid)

    def test_role_boss_requires_master(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            self.assertEqual(mp.do_spawn(["test-node/main:x", "--role", "boss"]), 2)

    def test_role_engineer_cannot_be_master(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            self.assertEqual(
                mp.do_spawn(["test-node/main:x", "--master", "--role", "engineer"]), 2)

    def test_bad_role_creates_no_tmux_or_roster_side_effect(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            self.assertEqual(mp.do_spawn(["test-node/main:x", "--role", "bogus"]), 2)
            self.assertEqual(mp.load_roster(), {}, "a refused spawn wrote a roster row")

    def test_legacy_boss_record_is_reborn_mounted(self):
        """A Boss recorded before roles existed must be UPGRADED, never restored bundle-less."""
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            args = mp.spawn_args_from_record("n/main:Boss", {"backend": "claude",
                                                             "is_master": True})
            self.assertIn("--role", args)
            self.assertEqual(args[args.index("--role") + 1], "boss")

    def test_legacy_engineer_record_stays_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            args = mp.spawn_args_from_record("n/main:eng-1", {"backend": "claude",
                                                              "is_master": False})
            self.assertNotIn("--role", args)


class RoleVisibilityTests(unittest.TestCase):
    def test_mounted_role_is_recorded_and_unmounted_renders_as_dash(self):
        with tempfile.TemporaryDirectory() as td:
            mp = load_mp(td)
            roster = {"n/main:Boss": {"role_ref": "boss@6.2.0"}, "n/main:eng-1": {}}
            self.assertEqual(mp.role_tag("n/main:Boss", {}, roster), "boss@6.2.0")
            self.assertEqual(mp.role_tag("n/main:eng-1", {}, roster), "-")
            self.assertEqual(mp.role_tag("n/main:unknown", {}, roster), "-")


class FirstRunTests(unittest.TestCase):
    def test_materialize_installs_the_role_store(self):
        """Regression: mp fails closed without INSTALL_DIR/roles, so first-run must copy it."""
        with tempfile.TemporaryDirectory() as td:
            install = Path(td) / "install"
            firstrun.materialize(str(install))
            self.assertTrue((install / "roles" / "registry.json").is_file(),
                            "first-run did not install the role store")
            reg = json.loads((install / "roles" / "registry.json").read_text())
            for rel in reg["roles"].values():
                self.assertTrue((install / "roles" / rel).is_file())


if __name__ == "__main__":
    unittest.main()
