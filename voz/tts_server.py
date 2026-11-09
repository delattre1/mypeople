"""Voz portuguesa Kokoro pm_santa, executada localmente com Metal."""

import argparse
import asyncio
import io
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import mlx.core as mx
import numpy as np
import soundfile as sf
from aiohttp import web
from mlx_audio.tts.utils import load
from voice_config import load as load_settings

LOG = logging.getLogger("tts")
MODEL = "mlx-community/Kokoro-82M-bf16"
REVISION = "a71e4d38b236d968966a2002c4c895dbd12b1c3c"


class Speaker:
    def __init__(self):
        if not mx.metal.is_available():
            raise RuntimeError("Metal não está disponível neste processo.")
        mx.set_default_device(mx.gpu)
        mx.set_cache_limit(256 * 1024 * 1024)
        self.model: Any = load(MODEL, revision=REVISION)
        self.synthesize("Pronto.", 1.25)

    def synthesize(self, text: str, speed: float) -> bytes:
        # Kokoro's Portuguese pipeline needs short segments to avoid truncation.
        segments = []
        for sentence in re.split(r"(?<=[.!?;:])\s+|\n+", text):
            words = sentence.split()
            while words:
                segments.append(" ".join(words[:35]))
                words = words[35:]
        chunks = []
        rate = 24000
        for result in self.model.generate(
            "\n".join(segments), voice="pm_santa", lang_code="p", speed=speed
        ):
            chunks.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
            rate = result.sample_rate
        if not chunks:
            raise RuntimeError("O modelo não produziu áudio.")
        buffer = io.BytesIO()
        sf.write(buffer, np.concatenate(chunks), rate, format="WAV", subtype="PCM_16")
        mx.clear_cache()
        return buffer.getvalue()


async def serve(port: int) -> None:
    settings = load_settings()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metal-tts")
    speaker = await loop.run_in_executor(executor, Speaker)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(
            {"ready": True, "model": MODEL, "voice": "pm_santa", "device": "Metal"}
        )

    async def synthesize(request: web.Request) -> web.Response:
        if request.headers.get("Origin"):
            raise web.HTTPForbidden(text="Use o comando de voz local.")
        try:
            data = await request.json()
            text = str(data.get("text", "")).strip()
            speed = float(
                data.get("speed", os.environ.get("TTS_SPEED", settings.tts_speed))
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise web.HTTPBadRequest(text="Texto ou velocidade inválida.") from exc
        if not text or len(text) > 8000 or not 0.5 <= speed <= 2:
            raise web.HTTPBadRequest(
                text="Envie até 8000 caracteres e velocidade entre 0.5 e 2."
            )
        audio = await loop.run_in_executor(executor, speaker.synthesize, text, speed)
        return web.Response(body=audio, content_type="audio/wav")

    app = web.Application(client_max_size=65536)
    app.router.add_get("/health", health)
    app.router.add_post("/speak", synthesize)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    LOG.info("[tts] pronto — pm_santa, Metal, 127.0.0.1:%s", port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        executor.shutdown(wait=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=load_settings().tts_port)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s"
    )
    LOG.setLevel(logging.INFO)
    try:
        asyncio.run(serve(args.port))
    except KeyboardInterrupt:
        pass
