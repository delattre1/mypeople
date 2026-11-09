"""Install the optional local voice services without starting or stopping them."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shlex
from dataclasses import asdict
from pathlib import Path

from voice_config import ROOT, Settings, absolute_path, config_path, load


def write_preserving(path: Path, data: bytes, mode: int = 0o600) -> bool:
    """Keep an immutable backup when replacing existing settings; identical runs do nothing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = path.read_bytes()
        if old == data:
            return False
        backup = path.with_name(
            path.name + ".backup-" + hashlib.sha256(old).hexdigest()[:12]
        )
        if not backup.exists():
            with backup.open("xb") as handle:
                os.chmod(backup, mode)
                handle.write(old)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        os.chmod(temporary, mode)
        handle.write(data)
    temporary.replace(path)
    return True


def launch_agents(
    settings: Settings, source: Path, root: Path = ROOT
) -> dict[str, dict]:
    home = Path.home()
    environment = {
        "PATH": f"{Path(settings.venv) / 'bin'}:{root / 'voz/runtime-bin'}:{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "MYPLOW_VOICE_CONFIG": str(source),
        "MYPEOPLE_HOME": settings.install_dir,
        "MYPEOPLE_CONFIG_PATH": settings.mypeople_config,
        "PYTHONUNBUFFERED": "1",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    }
    result = {}

    def add(label: str, command: list[str], log: str, **options) -> None:
        result[label] = {
            "Label": label,
            "ProgramArguments": command,
            "WorkingDirectory": str(root),
            "EnvironmentVariables": environment.copy(),
            "StandardOutPath": str(Path(settings.logs) / f"{log}.log"),
            "StandardErrorPath": str(Path(settings.logs) / f"{log}.log"),
            **options,
        }

    for service in ("stt", "tts"):
        add(
            f"com.myplow.voice.{service}",
            [settings.python, str(root / f"voz/{service}_server.py")],
            service,
            RunAtLoad=True,
            KeepAlive=True,
            ThrottleInterval=15,
        )
    add(
        "com.myplow.start",
        ["/bin/bash", str(root / "voz/start-myplow.sh")],
        "myplow-inicio",
        RunAtLoad=True,
        AbandonProcessGroup=True,
    )
    # launchd's environment does not reach a script started inside Terminal. Pass the
    # config path as a shell-quoted argument, then escape the entire AppleScript string.
    command = shlex.join(
        [
            "/usr/bin/env",
            f"MYPLOW_VOICE_CONFIG={source}",
            "/bin/bash",
            str(root / "voz/start-mic.command"),
        ]
    )
    apple_command = command.replace("\\", "\\\\").replace('"', '\\"')
    add(
        "com.myplow.voice.microphone",
        [
            "/usr/bin/osascript",
            "-e",
            f'tell application "Terminal" to do script "{apple_command}"',
        ],
        "microfone-inicio",
        RunAtLoad=settings.autostart_microphone,
        AbandonProcessGroup=True,
    )
    add(
        "com.myplow.voice.restart-stt",
        [
            "/bin/launchctl",
            "kickstart",
            "-k",
            f"gui/{os.getuid()}/com.myplow.voice.stt",
        ],
        "transcricao-reinicio",
        StartCalendarInterval={
            "Hour": settings.restart_hour,
            "Minute": settings.restart_minute,
        },
    )
    return result


def plugin_config(settings: Settings, source: Path, root: Path = ROOT) -> dict:
    return {
        "sse": settings.stt_host,
        "say": [
            "/usr/bin/env",
            f"MYPLOW_VOICE_CONFIG={source}",
            settings.python,
            str(root / "voz/fala.py"),
        ],
        "from": f"{settings.name} (microfone)",
        "cue": json.dumps(settings.cue, ensure_ascii=False),
    }


def install(settings: Settings, source: Path, root: Path = ROOT) -> None:
    if not Path(settings.python).is_file():
        raise ValueError(
            "Create the configured Python environment and install voz/requirements.txt first."
        )
    if not source.exists():
        write_preserving(
            source,
            (
                json.dumps(asdict(settings), indent=2, ensure_ascii=False) + "\n"
            ).encode(),
        )
    logs = Path(settings.logs)
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = Path(settings.install_dir) / "state/voice/config.json"
    existing = json.loads(target.read_text()) if target.exists() else {}
    existing.update(plugin_config(settings, source, root))
    write_preserving(
        target, (json.dumps(existing, indent=2, ensure_ascii=False) + "\n").encode()
    )
    for label, config in launch_agents(settings, source, root).items():
        target = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        write_preserving(target, plistlib.dumps(config), mode=0o644)
        print("Configured:", label)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=absolute_path)
    args = parser.parse_args()
    install(load(args.config), args.config or config_path())
    print("Settings saved. Run ./voz/iniciar.sh to start the services.")
