"""Fala uma resposta e só retorna quando o alto-falante termina de tocar."""

import io
import json
import os
import sys
import urllib.request

import sounddevice as sd
import soundfile as sf
from voice_config import load


def speak(text: str) -> None:
    settings = load()
    request = urllib.request.Request(
        os.environ.get("TTS_URL", settings.tts_url).rstrip("/") + "/speak",
        data=json.dumps(
            {
                "text": text,
                "speed": float(os.environ.get("TTS_SPEED", settings.tts_speed)),
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        audio, rate = sf.read(io.BytesIO(response.read()), dtype="float32")
    device = os.environ.get("TTS_OUTPUT_DEVICE") or settings.speaker
    sd.check_output_settings(device=device, samplerate=rate, channels=1)
    sd.play(audio, rate, device=device, blocking=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Uso: fala.py texto para falar")
    try:
        speak(" ".join(sys.argv[1:]))
    except Exception as exc:
        raise SystemExit(f"Não foi possível tocar a resposta: {exc}") from exc
