"""Exercise audio routing without recording or playing on the host."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fala
import mic


class AudioRoutingTests(unittest.TestCase):
    def test_playback_uses_configured_url_speed_device_and_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "voice.json"
            source.write_text(
                json.dumps({"speaker": 0, "tts_speed": 0.8, "tts_port": 18975})
            )
            audio = io.BytesIO()
            sf.write(audio, np.zeros(2400, dtype=np.float32), 24000, format="WAV")
            audio.seek(0)
            with (
                patch.dict(
                    os.environ, {"MYPLOW_VOICE_CONFIG": str(source)}, clear=True
                ),
                patch.object(
                    fala.urllib.request, "urlopen", return_value=audio
                ) as request,
                patch.object(fala.sd, "check_output_settings") as output,
                patch.object(fala.sd, "play") as play,
            ):
                fala.speak("Teste em português.")
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, "http://127.0.0.1:18975/speak")
            self.assertEqual(
                json.loads(sent.data), {"text": "Teste em português.", "speed": 0.8}
            )
            output.assert_called_once_with(device=0, samplerate=24000, channels=1)
            self.assertTrue(play.call_args.kwargs["blocking"])
            self.assertEqual(play.call_args.kwargs["device"], 0)

    def test_virtual_microphones_are_rejected_before_opening_audio(self):
        for name in (
            "BlackHole 2ch",
            "Loopback Audio",
            "Virtual Mic",
            "Aggregate Device",
        ):
            with (
                self.subTest(name=name),
                patch.object(mic.sd, "query_devices", return_value={"name": name}),
                patch.object(mic.sd, "check_input_settings") as opening,
                self.assertRaises(ValueError),
            ):
                asyncio.run(mic.run(None, "127.0.0.1:18103"))
            opening.assert_not_called()

    def test_microphone_refuses_nonlocal_hosts_before_querying_devices(self):
        for host in (
            "example.com:8103",
            "0.0.0.0:8103",
            "127.0.0.1:8103/path",
            "localhost:8103?x=1",
        ):
            with (
                self.subTest(host=host),
                patch.object(mic.sd, "query_devices") as devices,
                self.assertRaises(ValueError),
            ):
                asyncio.run(mic.run(None, host))
            devices.assert_not_called()


if __name__ == "__main__":
    unittest.main()
