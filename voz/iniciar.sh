#!/bin/bash
# Religa o setup já instalado; não cria outra cópia dos servidores.
set -euo pipefail
VOICE_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$VOICE_ROOT/.."
VOICE_DOMAIN="gui/$(id -u)"
for voice_service in com.myplow.voice.stt com.myplow.voice.tts com.myplow.start com.myplow.voice.restart-stt com.myplow.voice.microphone; do
  if ! launchctl print "$VOICE_DOMAIN/$voice_service" >/dev/null 2>&1; then
    launchctl bootstrap "$VOICE_DOMAIN" "$HOME/Library/LaunchAgents/$voice_service.plist"
    if [ "$voice_service" = com.myplow.voice.microphone ] && [ "$(python3 "$VOICE_ROOT/voice_config.py" autostart_microphone)" = False ]; then
      launchctl kickstart "$VOICE_DOMAIN/$voice_service"
    fi
  elif [ "$voice_service" != com.myplow.voice.restart-stt ]; then
    if [ "$voice_service" = com.myplow.voice.microphone ] && pgrep -f "$VOICE_ROOT/mic.py" >/dev/null; then
      continue
    fi
    launchctl kickstart "$VOICE_DOMAIN/$voice_service"
  fi
done
printf 'MyPlow e voz iniciados. Logs em %s\n' "$(python3 "$VOICE_ROOT/voice_config.py" logs)"
