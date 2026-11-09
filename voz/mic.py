"""Captura o microfone físico do Mac e envia áudio somente ao servidor local."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
from contextlib import suppress
from pathlib import Path

import aiohttp
import sounddevice as sd
from voice_config import load

LOG = logging.getLogger("microfone")


async def run(device: str | int | None, host: str) -> None:
    from urllib.parse import urlsplit

    address = urlsplit("http://" + host)
    if (
        address.hostname not in ("127.0.0.1", "localhost")
        or not address.port
        or address.path
        or address.query
        or address.fragment
        or address.username
    ):
        raise ValueError("Use apenas o servidor local em 127.0.0.1 ou localhost.")
    info = sd.query_devices(device, "input")
    if any(
        word in info["name"].lower()
        for word in ("blackhole", "loopback", "virtual", "aggregate")
    ):
        raise ValueError("Escolha um microfone físico.")
    sd.check_input_settings(device=device, channels=1, dtype="int16", samplerate=16000)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | Exception] = asyncio.Queue(maxsize=50)

    def push(data: bytes, failed: bool) -> None:
        if queue.full() or failed:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(
                RuntimeError("O áudio atrasou; reconectando sem enviar fala antiga.")
            )
        else:
            queue.put_nowait(data)

    def callback(indata, _frames, _time, status) -> None:
        loop.call_soon_threadsafe(push, bytes(indata), bool(status.input_overflow))

    async def receive(ws: aiohttp.ClientWebSocketResponse) -> None:
        async for _message in ws:
            pass
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(RuntimeError("O servidor de transcrição desconectou."))

    while True:
        while not queue.empty():
            queue.get_nowait()
        try:
            async with (
                aiohttp.ClientSession() as client,
                client.ws_connect(f"http://{host}/audio", heartbeat=20) as ws,
            ):
                LOG.info("conectado: %s -> %s", info["name"], host)
                receiver = asyncio.create_task(receive(ws))
                try:
                    with sd.RawInputStream(
                        device=device,
                        samplerate=16000,
                        channels=1,
                        dtype="int16",
                        blocksize=1280,
                        callback=callback,
                    ):
                        while not ws.closed:
                            data = await queue.get()
                            if isinstance(data, Exception):
                                raise data
                            await ws.send_bytes(data)
                finally:
                    receiver.cancel()
                    with suppress(asyncio.CancelledError):
                        await receiver
        except (aiohttp.ClientError, OSError, RuntimeError, sd.PortAudioError) as exc:
            LOG.warning("%s; nova tentativa em 3 segundos", exc)
            await asyncio.sleep(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", nargs="?")
    settings = load()
    parser.add_argument("host", nargs="?", default=settings.stt_host)
    parser.add_argument(
        "--capture",
        action="store_true",
        help="Use the microphone selected in the voice configuration.",
    )
    parser.add_argument(
        "--outputs",
        action="store_true",
        help="List output devices instead of microphones.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if args.device is None and not args.capture:
        for index, item in enumerate(sd.query_devices()):
            if item["max_output_channels" if args.outputs else "max_input_channels"]:
                print(f"{index}: {item['name']}")
    else:
        lock = Path(settings.install_dir) / "run/voice-mic.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("O microfone do Boss já está ligado.") from None
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()) + "\n")
            handle.flush()
            try:
                device = settings.microphone if args.capture else args.device
                if isinstance(device, str) and device.isdecimal():
                    device = int(device)
                asyncio.run(run(device, args.host))
            except KeyboardInterrupt:
                pass
