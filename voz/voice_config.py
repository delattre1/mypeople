"""Shared, local-only settings for the optional Apple Silicon voice setup."""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import pwd
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def owner_name() -> str:
    return pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0] or getpass.getuser()


def config_path() -> Path:
    value = os.environ.get("MYPLOW_VOICE_CONFIG")
    return (
        absolute_path(value)
        if value
        else Path.home() / ".config/mypeople/voice-metal.json"
    )


def absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or any(c in str(path) for c in "\n\r\0"):
        raise ValueError("Use an absolute path or a path starting with ~/.")
    return path


@dataclass(frozen=True)
class Settings:
    name: str
    microphone: str | int | None
    speaker: str | int | None
    cue: str
    backend: str
    venv: str
    logs: str
    install_dir: str
    mypeople_config: str
    stt_port: int = 8103
    tts_port: int = 8975
    tts_speed: float = 1.25
    restart_hour: int = 4
    restart_minute: int = 0
    autostart_microphone: bool = True

    @property
    def python(self) -> str:
        return str(Path(self.venv) / "bin/python")

    @property
    def stt_host(self) -> str:
        return f"127.0.0.1:{self.stt_port}"

    @property
    def stt_url(self) -> str:
        return f"http://{self.stt_host}"

    @property
    def tts_url(self) -> str:
        return f"http://127.0.0.1:{self.tts_port}"


def load(path: Path | None = None) -> Settings:
    home = Path.home()
    defaults = Settings(
        name=owner_name(),
        microphone=None,
        speaker=None,
        cue="ei Boss",
        backend="codex",
        venv=str(home / "pirata-voz"),
        logs=str(home / "pirata-logs"),
        install_dir=str(home / ".local/share/mypeople"),
        mypeople_config=str(home / ".config/mypeople/queue.env"),
    )
    source = path or config_path()
    data = json.loads(source.read_text()) if source.exists() else {}
    if not source.exists() and (
        path is not None or "MYPLOW_VOICE_CONFIG" in os.environ
    ):
        raise ValueError(f"Voice configuration does not exist: {source}")
    if not isinstance(data, dict) or set(data) - set(asdict(defaults)):
        raise ValueError(
            "Voice configuration must be an object with known setting names."
        )
    values = asdict(defaults) | data
    for key in ("name", "cue", "backend"):
        if (
            not isinstance(values[key], str)
            or not values[key].strip()
            or any(c in values[key] for c in "\n\r\0")
        ):
            raise ValueError(f"Invalid {key}.")
    if values["backend"] not in ("codex", "claude", "grok"):
        raise ValueError("backend must be codex, claude, or grok.")
    for key in ("microphone", "speaker"):
        value = values[key]
        if value is not None and not (
            type(value) is int
            and value >= 0
            or isinstance(value, str)
            and value.strip()
        ):
            raise ValueError(
                f"{key} must be a device name, index, or null for the system default."
            )
    for key in ("venv", "logs", "install_dir", "mypeople_config"):
        if not isinstance(values[key], str):
            raise TypeError(f"Invalid {key} path.")
        values[key] = str(absolute_path(values[key]))
    for key, low, high in (
        ("stt_port", 1024, 65535),
        ("tts_port", 1024, 65535),
        ("restart_hour", 0, 23),
        ("restart_minute", 0, 59),
    ):
        if type(values[key]) is not int or not low <= values[key] <= high:
            raise ValueError(f"{key} must be between {low} and {high}.")
    if values["stt_port"] == values["tts_port"]:
        raise ValueError("STT and TTS must use different ports.")
    speed = values["tts_speed"]
    if (
        type(speed) not in (int, float)
        or not math.isfinite(speed)
        or not 0.5 <= speed <= 2
    ):
        raise ValueError("tts_speed must be between 0.5 and 2.")
    if type(values["autostart_microphone"]) is not bool:
        raise ValueError("autostart_microphone must be true or false.")
    return Settings(**values)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "setting",
        choices=[
            "python",
            "logs",
            "stt_host",
            "backend",
            "install_dir",
            "mypeople_config",
            "autostart_microphone",
        ],
    )
    args = parser.parse_args()
    print(getattr(load(), args.setting))
