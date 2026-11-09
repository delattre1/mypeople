"""Legendas locais para o MyPlow: Nemotron 3.5 usando Metal, sem nuvem."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import mlx.core as mx
import numpy as np
from aiohttp import WSMsgType, web
from mlx_audio.stt import load
from voice_config import load as load_settings

LOG = logging.getLogger("stt")
RATE = 16000
BLOCK = 1280  # 80 ms; bounded input keeps the streaming decoder caught up.
MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
REVISION = "e550040c0478027ed679b2b6b0d055502c103663"


class Recognizer:
    def __init__(self, lookahead: int):
        if not mx.metal.is_available():
            raise RuntimeError("Metal não está disponível neste processo.")
        mx.set_default_device(mx.gpu)
        mx.set_cache_limit(512 * 1024 * 1024)
        self.model: Any = load(MODEL, revision=REVISION)
        self.model.default_att_context_size = [56, lookahead]
        self.session: Any = None
        self.text = ""
        self.prefix = ""
        self.phrase = ""
        self.preroll: deque[np.ndarray] = deque(maxlen=3)
        self.levels: deque[tuple[float, float]] = deque()
        self.silence = 0.0
        self.duration = 0.0
        self.audio_seconds = 0.0
        self.compute_seconds = 0.0
        self.started = time.monotonic()

    def _append(self, pieces: list[str]) -> None:
        self.phrase += "".join(pieces)
        self.text = (self.prefix + self.phrase).strip()[-8000:]

    def finish(self) -> str:
        if self.session is not None:
            self.session.close()
            while not self.session.done:
                self._append(self.session.step(max_decode_tokens=512))
            self.session = None
            self.prefix = (self.text + " ") if self.text else ""
            self.phrase = ""
            self.silence = self.duration = 0.0
            mx.clear_cache()
        return self.text

    def reset_input(self) -> None:
        self.finish()
        self.preroll.clear()
        self.levels.clear()

    def feed(self, data: bytes) -> str:
        if len(data) % 2 or len(data) > BLOCK * 2:
            raise ValueError("Envie PCM mono de 16 kHz em blocos de até 80 ms.")
        started = time.monotonic()
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        if not samples.size:
            return self.text
        seconds = samples.size / RATE
        self.audio_seconds += seconds
        peak = 20 * math.log10(max(float(np.max(np.abs(samples))), 1e-9))
        rms = 20 * math.log10(max(float(np.sqrt(np.mean(samples**2))), 1e-9))
        now = time.monotonic()
        self.levels.append((now, peak))
        while self.levels and self.levels[0][0] < now - 1:
            self.levels.popleft()
        voiced = rms > -50
        if self.session is None:
            self.preroll.append(samples)
            if not voiced:
                return self.text
            self.session = self.model.create_streaming_session(language="pt-BR")
            for previous in self.preroll:
                self.session.feed(previous)
                self._append(self.session.step(max_decode_tokens=512))
            self.preroll.clear()
        else:
            self.session.feed(samples)
            self._append(self.session.step(max_decode_tokens=512))
        self.silence = 0 if voiced else self.silence + seconds
        self.duration += seconds
        # Flush on a pause; reset caches between utterances instead of growing all day.
        if self.silence >= 1.2 or self.duration >= 30:
            self.finish()
        self.compute_seconds += time.monotonic() - started
        return self.text

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "ready": True,
            "model": MODEL,
            "device": "Metal",
            "language": "pt-BR",
            "energy_peak_db": max(
                (db for t, db in self.levels if t >= now - 1), default=-180
            ),
            "audio_seconds": round(self.audio_seconds, 2),
            "compute_seconds": round(self.compute_seconds, 2),
            "real_time_factor": round(
                self.compute_seconds / max(self.audio_seconds, 0.001), 3
            ),
            "metal_memory_mb": round(mx.get_active_memory() / 1048576),
            "uptime_seconds": round(now - self.started),
        }


async def serve(port: int, lookahead: int) -> None:
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metal-stt")
    recognizer = await loop.run_in_executor(executor, Recognizer, lookahead)
    listeners: set[asyncio.Queue[str]] = set()
    current: web.WebSocketResponse | None = None
    input_lock = asyncio.Lock()
    latest = ""

    def publish(text: str) -> None:
        nonlocal latest
        if text == latest:
            return
        latest = text
        for queue in listeners:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(text)

    async def stats(_request: web.Request) -> web.Response:
        data = await loop.run_in_executor(executor, recognizer.stats)
        data["mics_connected"] = int(current is not None and not current.closed)
        return web.json_response(data)

    async def events(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
        listeners.add(queue)
        queue.put_nowait(latest)
        try:
            while True:
                try:
                    text = await asyncio.wait_for(queue.get(), timeout=10)
                    payload = (
                        "data: "
                        + json.dumps({"text": text}, ensure_ascii=False)
                        + "\n\n"
                    )
                except asyncio.TimeoutError:
                    payload = ": keepalive\n\n"
                await response.write(payload.encode())
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            listeners.discard(queue)
        return response

    async def audio(request: web.Request) -> web.WebSocketResponse:
        nonlocal current
        # This endpoint is for the local microphone process, not third-party web pages.
        if request.headers.get("Origin"):
            raise web.HTTPForbidden(text="Use o microfone local.")
        ws = web.WebSocketResponse(max_msg_size=65536, heartbeat=20)
        await ws.prepare(request)
        if current is not None:
            await current.close(code=1000, message=b"Novo microfone conectado")
        async with input_lock:
            current = ws
            await loop.run_in_executor(executor, recognizer.reset_input)
            LOG.info("microfone conectado")
            try:
                async for message in ws:
                    if message.type == WSMsgType.BINARY:
                        publish(
                            await loop.run_in_executor(
                                executor, recognizer.feed, message.data
                            )
                        )
                    elif message.type == WSMsgType.TEXT and message.data == "end":
                        publish(await loop.run_in_executor(executor, recognizer.finish))
                        await ws.send_json({"text": latest, "done": True})
                        break
            except Exception:
                LOG.exception("falha na transcrição")
                await ws.close(code=1011, message=b"Falha na transcricao")
            finally:
                publish(await loop.run_in_executor(executor, recognizer.finish))
                current = None
                LOG.info("microfone desconectado")
        await ws.close()
        return ws

    app = web.Application(client_max_size=65536)
    app.router.add_get("/stats", stats)
    app.router.add_get("/health", stats)
    app.router.add_get("/events", events)
    app.router.add_get("/audio", audio)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    LOG.info("[live] model ready — Metal, português, 127.0.0.1:%s", port)
    try:
        await asyncio.Event().wait()
    finally:
        if current is not None:
            await current.close()
        await runner.cleanup()
        executor.shutdown(wait=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=load_settings().stt_port)
    parser.add_argument("--lookahead", type=int, choices=[0, 3, 6, 13], default=3)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s"
    )
    LOG.setLevel(logging.INFO)
    try:
        asyncio.run(serve(args.port, args.lookahead))
    except KeyboardInterrupt:
        pass
