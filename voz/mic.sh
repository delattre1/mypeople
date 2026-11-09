#!/bin/bash
set -euo pipefail
VOICE_ROOT="$(cd "$(dirname "$0")" && pwd)"
VOICE_PYTHON="$(python3 "$VOICE_ROOT/voice_config.py" python)"
exec "$VOICE_PYTHON" "$VOICE_ROOT/mic.py" "$@"
