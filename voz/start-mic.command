#!/bin/bash
set -euo pipefail
VOICE_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$VOICE_ROOT/.."
VOICE_LOGS="$(python3 "$VOICE_ROOT/voice_config.py" logs)"
mkdir -p "$VOICE_LOGS"
printf 'Microfone do Boss. Para desligar, pressione Control-C.\n'
exec "$VOICE_ROOT/mic.sh" --capture >> "$VOICE_LOGS/microfone.log" 2>&1
