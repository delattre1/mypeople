# Local voice on Apple Silicon (Metal)

Optional Portuguese voice for the existing MyPlow voice plugin. Nemotron 3.5 ASR
Streaming 0.6B transcribes through MLX/Metal; Kokoro-82M speaks with `pm_santa`.
Speech recognition and synthesis run locally, without a paid speech service.
The selected agent backend still processes the text using its configured account.

```
Physical microphone -> mic.py -> STT /events -> MyPlow voice plugin -> Boss
Boss -> voice.py say -> fala.py -> Kokoro -> selected speaker
```

Both servers bind only to `127.0.0.1`. This implementation targets Apple Silicon,
Python 3.12 and Brazilian Portuguese; it does not implement remote GPU hosts or
other recognition/synthesis languages. Tested on an M1 Pro with 32 GB of RAM.
Code and model revisions are pinned; allow several GB for the initial download.

## Install

Install and authenticate MyPlow first, as described in the repository README.
From the repository root, create a separate environment:

```sh
uv venv --python 3.12 ~/pirata-voz
uv pip install --python ~/pirata-voz/bin/python -r voz/requirements.txt
mkdir -p ~/.config/mypeople
cp -n voz/config.example.json ~/.config/mypeople/voice-metal.json
```

Edit `~/.config/mypeople/voice-metal.json`. Omitted values use the defaults below.
To use a different file, export `MYPLOW_VOICE_CONFIG=/absolute/path/voice.json`
before running any of these commands. `--config /absolute/path/voice.json` is
also accepted by the two configuration installers.

| Setting | Default / meaning |
| --- | --- |
| `name` | Current user's full name from the operating system |
| `microphone` | `null`: system default; a physical input is required |
| `speaker` | `null`: system default output |
| `cue` | `ei Boss` |
| `backend` | `codex`; also accepts `claude` or `grok` |
| `venv` | `~/pirata-voz`; must contain Python and the voice requirements |
| `logs` | `~/pirata-logs` |
| `install_dir` | `~/.local/share/mypeople` |
| `mypeople_config` | `~/.config/mypeople/queue.env`; use the matching MyPlow installation |
| `stt_port`, `tts_port` | `8103`, `8975`; distinct, loopback-only ports |
| `tts_speed` | `1.25`; range `0.5`–`2.0` |
| `restart_hour`, `restart_minute` | `4`, `0`; daily recognition restart in the Mac's local time |
| `autostart_microphone` | `true`; set `false` to leave capture off at login |

List devices before selecting their names or numeric indices:

```sh
./voz/mic.sh
./voz/mic.sh --outputs
```

Names are preferable to indices, which can change when devices are connected.
The recorder rejects named virtual/loopback/aggregate inputs. If the system default
is one of those devices, choose a physical microphone explicitly.

Install the plugin configuration and the five user LaunchAgents, then start them:

```sh
~/pirata-voz/bin/python voz/install_launchagents.py
./voz/iniciar.sh
```

If you changed `venv`, use its Python instead in these commands. The installer
writes configuration only: it does not load/unload services, restart agents, or
clear a plugin's existing off switch. Existing settings are backed up before
replacement; unchanged runs leave them untouched. If voice was deliberately
switched off in MyPlow, enable it with `mp plugins on voice`.

The microphone opens in Terminal so macOS can grant Terminal microphone access.
Allow that request. On first start, wait for `[live] model ready` in `stt.log`
and `[tts] pronto` in `tts.log`; models download automatically without login.
`http://127.0.0.1:8103/stats` should show `mics_connected: 1` and a rising
`energy_peak_db` while you speak. Substitute your configured STT port.

## Verify and use

Press Control-C in the microphone's Terminal window before running:

```sh
~/pirata-voz/bin/python voz/check.py
```

This synthesizes a fresh phrase and sends it directly through the recognition
stream. It refuses to displace a connected microphone, checks only the new
transcript, and saves `teste-voz.json`/`teste-voz.wav` in the configured log folder.
It does not test speaker output. Start the microphone again with `./voz/iniciar.sh`.
Then call **“ei Boss, que horas são?”**, using your configured cue, and verify a
single spoken reply. `fala.py` waits for playback to finish so the plugin's echo
filter can track it. Existing `TTS_URL`, `TTS_SPEED`, and `TTS_OUTPUT_DEVICE`
environment overrides remain supported by that command.

Capture continues even when you are not calling the Boss. To pause capture,
press **Control-C** in its Terminal window. To resume, run `./voz/iniciar.sh`.
Minimizing Terminal keeps capture running; closing that window stops it.
At login, the microphone starts again unless `autostart_microphone` is false.
The STT/TTS services restart after failure. STT is also restarted daily; the
microphone and the voice plugin reconnect automatically.

To change settings, edit the JSON, rerun the installer, and log out/in to reload
the LaunchAgents. Do not run two checkouts of this setup at once: it uses one set
of service labels per macOS user. Keep the checkout and configured environment
at stable paths while its services are installed.

The scoped `runtime-bin/pgrep` and `pkill` wrappers handle trailing spaces in
macOS process matching. Without them, MyPlow can start duplicate plugins or
fail to stop one. They are on the MyPlow service's PATH, not installed globally.

## Optional Portuguese Boss profile

Some distributed Boss profiles insist on English even when a voice message asks
for Portuguese. To select a local Portuguese profile using your configured name
and cue, run:

```sh
~/pirata-voz/bin/python voz/configure_boss_language.py
```

This derives a version from the currently selected Boss profile, keeps its other
rules and capabilities, and backs up the registry. It does not alter the stock
profile, delete conversation history, or restart the Boss. An existing Boss must
be resumed against the new registry entry to adopt it. When the Boss is idle,
use its full agent ID from `mypeople status`:

```sh
# Replace AGENT_ID with the full Boss ID shown by mypeople status.
~/.local/share/mypeople/bin/mp kill AGENT_ID --reason 'Apply local Portuguese profile'
MP_REVIVE_FOLLOWS_REGISTRY=1 ~/.local/share/mypeople/bin/mp revive AGENT_ID
```

Use your configured installation path if it differs. With Codex 0.167.0, a
resumed session retained its previous language instructions until its context
was rebuilt. After resuming, run:

```sh
~/.local/share/mypeople/bin/mp send AGENT_ID /compact
```

This preserves the session and its saved history. With other backends, verify
the response after resuming and use that backend's context-refresh procedure
only if the old language rule remains active.

## Logs and tests

The configured log folder contains `stt.log`, `tts.log`, `microfone.log`,
`myplow-inicio.log`, `microfone-inicio.log`, and `transcricao-reinicio.log`.
MyPlow's `logs/daemon.log` contains voice delivery and echo-filter messages.
Personal configuration, model caches, logs, and transcripts belong outside Git.

```sh
uv pip install --python ~/pirata-voz/bin/python -r voz/requirements-dev.txt
uv run --no-project ~/pirata-voz/bin/ruff check voz
uv run --no-project ~/pirata-voz/bin/pyright --pythonpath ~/pirata-voz/bin/python voz
~/pirata-voz/bin/python -m unittest discover -s voz/tests -v
```

Unit tests use temporary settings and mocked audio output. They do not modify
installed services or record/play audio. `check.py` is the separate real-model
integration test. To test a development checkout without interrupting an existing
setup, run its two servers manually with a separate config file, unused STT/TTS
ports, and a temporary log folder; run `check.py` with that same config.

Sources: [MLX Audio](https://github.com/Neywa-Labs/mlx-audio),
[Nemotron MLX](https://huggingface.co/mlx-community/nemotron-3.5-asr-streaming-0.6b),
[Kokoro MLX](https://huggingface.co/mlx-community/Kokoro-82M-bf16).
