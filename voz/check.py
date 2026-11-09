"""Teste real: Kokoro gera português; Nemotron transcreve o áudio pelo fluxo ao vivo."""

import asyncio
import io
import json
import math
import re
import time
import unicodedata
from pathlib import Path

import aiohttp
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from voice_config import load

PHRASE = "Olá. Hoje é um bom dia para testar a voz em português no computador."


def words(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", text)
    return set(re.findall(r"[a-z]+", text.encode("ascii", "ignore").decode().lower()))


async def check() -> None:
    settings = load()
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=180)
    ) as client:
        async with client.get(settings.stt_url + "/stats") as response:
            response.raise_for_status()
            before = await response.json()
        if before["mics_connected"]:
            raise RuntimeError("Desligue o mic.sh antes deste teste.")
        async with client.get(settings.stt_url + "/events") as response:
            response.raise_for_status()
            first = await response.content.readline()
            baseline = json.loads(first.decode().removeprefix("data: "))["text"]
        started = time.monotonic()
        async with client.post(
            settings.tts_url + "/speak", json={"text": PHRASE}
        ) as response:
            response.raise_for_status()
            wav = await response.read()
        tts_seconds = time.monotonic() - started
        audio, rate = sf.read(io.BytesIO(wav), dtype="float32")
        if audio.size < rate or float(np.max(np.abs(audio))) < 0.001:
            raise RuntimeError("O sintetizador não produziu fala audível.")
        divisor = math.gcd(rate, 16000)
        samples = resample_poly(audio, 16000 // divisor, rate // divisor)
        samples = np.concatenate((samples, np.zeros(32000, dtype=np.float32)))
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        started = time.monotonic()
        async with client.ws_connect(settings.stt_url + "/audio") as ws:
            for offset in range(0, len(pcm), 2560):
                await ws.send_bytes(pcm[offset : offset + 2560])
            await ws.send_str("end")
            message = await ws.receive_json()
            if not message.get("done"):
                raise RuntimeError("A transcrição não terminou.")
        stt_seconds = time.monotonic() - started
        text = message["text"].removeprefix(baseline).strip()
        overlap = len(words(PHRASE) & words(text)) / len(words(PHRASE))
        result = {
            "phrase": PHRASE,
            "transcript": text,
            "word_match": round(overlap, 3),
            "audio_seconds": round(len(audio) / rate, 2),
            "tts_seconds": round(tts_seconds, 2),
            "stt_seconds": round(stt_seconds, 2),
            "passed": overlap >= 0.8,
        }
        output = Path(settings.logs)
        output.mkdir(parents=True, exist_ok=True)
        (output / "teste-voz.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        (output / "teste-voz.wav").write_bytes(wav)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["passed"]:
            raise RuntimeError(
                "O reconhecimento da frase ficou abaixo do mínimo do teste."
            )


if __name__ == "__main__":
    asyncio.run(check())
