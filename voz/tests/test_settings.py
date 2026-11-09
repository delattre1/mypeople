"""Configuration and installation tests; never touch the operator's services."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configure_boss_language as language
import install_launchagents as installer
import voice_config as config


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home with spaces"
        self.home.mkdir()
        self.source = self.home / "voice.json"
        self.addCleanup(patch.stopall)
        patch.object(Path, "home", return_value=self.home).start()
        patch.object(config, "owner_name", return_value="Test Owner").start()
        patch.dict(os.environ, {"MYPLOW_VOICE_CONFIG": str(self.source)}).start()
        self.source.write_text("{}")

    def settings(self, **values):
        self.source.write_text(json.dumps(values))
        return config.load()

    def test_defaults_and_names_come_from_current_user(self):
        value = self.settings()
        self.assertEqual(value.name, "Test Owner")
        self.assertEqual(value.python, str(self.home / "pirata-voz/bin/python"))
        self.assertEqual(value.stt_url, "http://127.0.0.1:8103")
        self.assertIsNone(value.microphone)

    def test_custom_settings_propagate_to_plugin_and_every_service(self):
        value = self.settings(
            name="Test Person",
            cue="olá chefe",
            microphone="USB Mic",
            speaker=0,
            backend="claude",
            stt_port=18103,
            tts_port=18975,
            tts_speed=0.9,
            restart_hour=3,
            restart_minute=15,
            autostart_microphone=False,
        )
        agents = installer.launch_agents(value, self.source)
        plugin = installer.plugin_config(value, self.source)
        self.assertEqual(plugin["from"], "Test Person (microfone)")
        self.assertEqual(plugin["sse"], "127.0.0.1:18103")
        self.assertEqual(json.loads(plugin["cue"]), "olá chefe")
        self.assertIn(f"MYPLOW_VOICE_CONFIG={self.source}", plugin["say"])
        self.assertEqual(plugin["say"][-2], value.python)
        for item in agents.values():
            self.assertEqual(plistlib.loads(plistlib.dumps(item)), item)
            self.assertEqual(
                item["EnvironmentVariables"]["MYPLOW_VOICE_CONFIG"], str(self.source)
            )
            self.assertEqual(
                item["EnvironmentVariables"]["MYPEOPLE_CONFIG_PATH"],
                value.mypeople_config,
            )
        self.assertFalse(agents["com.myplow.voice.microphone"]["RunAtLoad"])
        self.assertEqual(
            agents["com.myplow.voice.restart-stt"]["StartCalendarInterval"],
            {"Hour": 3, "Minute": 15},
        )

    def test_terminal_receives_config_path_safely_with_quotes_and_spaces(self):
        source = self.home / 'voice "quoted" and apostrophe\'.json'
        agents = installer.launch_agents(self.settings(), source)
        script = agents["com.myplow.voice.microphone"]["ProgramArguments"][-1]
        command = json.loads(
            script.removeprefix('tell application "Terminal" to do script ')
        )
        args = shlex.split(command)
        self.assertEqual(args[1], f"MYPLOW_VOICE_CONFIG={source}")
        self.assertEqual(args[-2], "/bin/bash")

    def test_invalid_or_remote_configuration_is_rejected(self):
        cases = [
            {"stt_port": 80},
            {"stt_port": 8975},
            {"tts_port": True},
            {"tts_speed": float("nan")},
            {"tts_speed": 3},
            {"tts_speed": True},
            {"restart_hour": 24},
            {"autostart_microphone": "false"},
            {"backend": "unknown"},
            {"microphone": False},
            {"logs": "./relative"},
            {"logs": "/tmp/unsafe\npath"},
            {"speaker": -1},
            {"name": ""},
            {"bind": "0.0.0.0"},
            {"unknown_key": "value"},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises((TypeError, ValueError)):
                self.settings(**data)

    def test_missing_explicit_config_does_not_silently_use_defaults(self):
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            config.load()

    def test_install_is_idempotent_and_preserves_user_fields_and_backups(self):
        value = self.settings()
        python = Path(value.python)
        python.parent.mkdir(parents=True)
        python.touch()
        plugin = Path(value.install_dir) / "state/voice/config.json"
        plugin.parent.mkdir(parents=True)
        old = b'{"sse": "old", "custom_setting": "keep"}'
        plugin.write_bytes(old)
        installer.install(value, self.source)
        self.assertEqual(json.loads(plugin.read_text())["custom_setting"], "keep")
        self.assertEqual(plugin.stat().st_mode & 0o777, 0o600)
        backup = plugin.with_name(
            plugin.name + ".backup-" + hashlib.sha256(old).hexdigest()[:12]
        )
        self.assertEqual(backup.read_bytes(), old)
        files = [p for p in self.home.rglob("*") if p.is_file()]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        installer.install(value, self.source)
        self.assertEqual(
            before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
        )

    def test_install_refuses_missing_environment_before_writing_services(self):
        with self.assertRaises(ValueError):
            installer.install(self.settings(), self.source)
        self.assertFalse((self.home / "Library/LaunchAgents").exists())

    def make_role(
        self,
        doctrine="## Identity\nKeep this.\n\n## Language\nEnglish only.\n\n## Work\nKeep work.\n",
    ):
        value = self.settings()
        roles = Path(value.install_dir) / "roles"
        (roles / "profiles/boss").mkdir(parents=True)
        (roles / "personalities/boss").mkdir(parents=True)
        original = roles / "personalities/boss/2.5.0.md"
        original.write_text(doctrine)
        profile = {
            "metadata": {"name": "boss", "version": "2.5.0"},
            "spec": {
                "personalityRef": "personalities/boss/2.5.0.md",
                "skills": ["keep"],
            },
        }
        (roles / "profiles/boss/2.5.0.json").write_text(json.dumps(profile))
        registry = roles / "registry.json"
        registry.write_text(
            json.dumps(
                {"roles": {"boss": "profiles/boss/2.5.0.json", "engineer": "keep"}}
            )
        )
        registry.chmod(0o444)
        return value, roles, original, registry

    def test_local_language_profile_preserves_original_and_other_roles(self):
        value, roles, original, registry = self.make_role()
        original_bytes = original.read_bytes()
        ref = language.configure(value)
        profile = json.loads((roles / ref).read_text())
        doctrine = (roles / profile["spec"]["personalityRef"]).read_text()
        self.assertIn("Test Owner", doctrine)
        self.assertIn("Brazilian Portuguese", doctrine)
        self.assertIn("Keep work.", doctrine)
        self.assertEqual(profile["spec"]["skills"], ["keep"])
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual(json.loads(registry.read_text())["roles"]["engineer"], "keep")
        self.assertEqual(language.configure(value), ref)
        self.assertEqual(registry.stat().st_mode & 0o777, 0o444)
        other_ref = language.configure(replace(value, cue="novo chamado"))
        self.assertNotEqual(ref, other_ref)
        self.assertTrue((roles / ref).exists())

    def test_language_refuses_unexpected_doctrine_without_mutation(self):
        value, _, original, registry = self.make_role("Unrecognized format")
        before = registry.read_bytes()
        with self.assertRaises(ValueError):
            language.configure(value)
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual(original.read_text(), "Unrecognized format")


if __name__ == "__main__":
    unittest.main()
