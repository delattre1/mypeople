#!/bin/bash
set -euo pipefail
VOICE_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$VOICE_ROOT/.."
export PATH="$VOICE_ROOT/runtime-bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
VOICE_BACKEND="$(python3 "$VOICE_ROOT/voice_config.py" backend)"
export MYPEOPLE_HOME="$(python3 "$VOICE_ROOT/voice_config.py" install_dir)"
export MYPEOPLE_CONFIG_PATH="$(python3 "$VOICE_ROOT/voice_config.py" mypeople_config)"
exec mypeople up --backend "$VOICE_BACKEND" --detach
