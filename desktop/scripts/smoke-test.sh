#!/usr/bin/env bash
# Prove a built MyPlow.app actually carries a working runtime.
#
# This starts the stack using ONLY the binaries inside the .app — the same PATH main.rs
# builds — and asserts the three front doors answer. It is the check that fails if the
# bundle script missed a dylib, if a signature broke, or if Tauri dropped an exec bit.
#
#   ./scripts/smoke-test.sh [path/to/MyPlow.app]
#
# ⚠️ ISOLATION. This machine runs a live MyPlow fleet, and the live install's config is
# exported into every agent shell (INSTALL_DIR, QUEUE_SECRET, TMUX, ...). mpcommon.load_env()
# lets process env override the config file, so an un-overridden run would materialize over
# the live install, restart the live daemons, and — because tmux keys its socket to the UID,
# not HOME — deliver into the live Boss pane. Every mypeople key is therefore overridden
# explicitly below, TMUX_TMPDIR is a short real dir, and TMUX itself is unset.
set -uo pipefail

APP="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src-tauri/target/release/bundle/macos/MyPlow.app}"
RES="$APP/Contents/Resources/runtime"
[ -d "$RES" ] || { echo "no runtime in $APP — run bundle-runtime.sh then cargo tauri build" >&2; exit 1; }

# Short paths: tmux appends /tmux-<uid>/default and macOS caps a unix socket at ~104 chars.
PREFIX=/tmp/mpsmoke
MUX=/tmp/mpsmokemux
HUD=39900; TODO=39933; TTYD=37681

# The isolated environment. Mirrors the PATH/PYTHONPATH main.rs builds, plus the overrides
# that keep this off the live install.
run() {
  env -u TMUX -u TMUX_PANE -u QUEUE_SECRET -u UPSTREAM_QUEUE_URL -u UPSTREAM_QUEUE_SECRET \
      PATH="$RES/bin:$RES/python/bin:$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
      PYTHONPATH="$RES/pylib" PYTHONDONTWRITEBYTECODE=1 \
      MYPEOPLE_DESKTOP=1 HOME="$PREFIX/home" \
      MYPEOPLE_HOME="$PREFIX" MYPEOPLE_CONFIG_PATH="$PREFIX/config/queue.env" \
      INSTALL_DIR="$PREFIX" HOST_ID=mpsmoke \
      HUD_PORT=$HUD TODO_PORT=$TODO TTYD_PORT=$TTYD TTYD_RO_PORT=$((TTYD+1)) \
      TTYD_BROWSER_PORT=$TTYD QUEUE_URL="http://127.0.0.1:$HUD" \
      BIND_ADDR=127.0.0.1 TMUX_TMPDIR="$MUX" \
      "$RES/python/bin/python3" "$@"
}

cleanup() {
  run -m mypeople.cli down >/dev/null 2>&1
  env -u TMUX TMUX_TMPDIR="$MUX" "$RES/bin/tmux" kill-server >/dev/null 2>&1
  rm -rf "$PREFIX" "$MUX"
}
trap cleanup EXIT

# Its own HOME too: firstrun points ~/.claude/settings.json hooks and ~/.tmux.conf at the install it
# sets up, so a run on a developer's Mac would aim every live agent's hooks at this throwaway dir.
# An empty tpm dir skips the tmux theme download: it is cosmetic, needs the network, and is not
# what this test proves.
rm -rf "$PREFIX" "$MUX"; mkdir -p "$PREFIX/home/.tmux/plugins/tpm" "$MUX"

fail=0
check() {  # check <name> <url> [expected-substring]
  local name="$1" url="$2" want="${3:-}" code body
  body="$(curl -sS --max-time 8 -w '\n%{http_code}' "$url" 2>/dev/null)"
  code="$(printf '%s' "$body" | tail -n1)"
  if [ "$code" != "200" ]; then
    printf '  FAIL  %-22s %s -> HTTP %s\n' "$name" "$url" "${code:-none}"; fail=1; return
  fi
  if [ -n "$want" ] && ! printf '%s' "$body" | grep -qi -- "$want"; then
    printf '  FAIL  %-22s %s -> 200 but no %q\n' "$name" "$url" "$want"; fail=1; return
  fi
  printf '  ok    %-22s %s\n' "$name" "$url"
}

echo "[smoke] starting the carried runtime from $APP"
# --detach returns once the HUD answers /health, or after its own 40s warning.
run -m mypeople.cli up --detach 2>&1 | sed 's/^/  | /'

echo "[smoke] front doors"
check "HUD health"   "http://127.0.0.1:$HUD/health" '"status"'
check "board"        "http://127.0.0.1:$TODO/"
check "ttyd (rw)"    "http://127.0.0.1:$TTYD/"
check "ttyd (ro)"    "http://127.0.0.1:$((TTYD+1))/"

# The point of the whole exercise: the running daemons must be the carried binaries, not
# whatever Homebrew happens to have on this developer's machine. On a stranger's Mac there
# is no Homebrew, so a test that passes via /opt/homebrew proves nothing.
#
# Ask the kernel which executable is listening on each port. `ps -o comm` is not enough: for a
# command exec'd by bare name through PATH it reports just "ttyd", which proves nothing.
# lsof's `txt` descriptor is the mapped executable image, i.e. the file actually running.
echo "[smoke] every listener is a binary the app carries"
for spec in "$HUD:HUD (python3)" "$TODO:board (python3)" "$TTYD:ttyd rw" "$((TTYD+1)):ttyd ro"; do
  port="${spec%%:*}"; name="${spec#*:}"
  pid="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n1)"
  exe="$( [ -n "$pid" ] && lsof -a -p "$pid" -d txt -Fn 2>/dev/null | sed -n 's/^n//p' | head -n1 )"
  case "$exe" in
    "$RES"/*) printf '  ok    %-22s %s\n' "$name" "${exe#"$APP"/}" ;;
    *)        printf '  FAIL  %-22s port %s served by %s\n' "$name" "$port" "${exe:-nothing}"; fail=1 ;;
  esac
done

# The bundle is signed: nothing that runs it may write into it. Agents call this python3 off PATH
# with no PYTHONDONTWRITEBYTECODE (run() above sets it, which is why this went unseen).
echo "[smoke] running the carried python writes nothing into the bundle"
touch "$PREFIX/before-bytecode"; sleep 1
env -u PYTHONDONTWRITEBYTECODE "$RES/python/bin/python3" -c 'import urllib.request, json, email, http.client, ssl, base64' \
  && env -u PYTHONDONTWRITEBYTECODE PYTHONPATH="$RES/pylib" "$RES/python/bin/python3" -c 'import mypeople.cli'
wrote="$(find "$APP" -newer "$PREFIX/before-bytecode" -type f | head -3)"
if [ -z "$wrote" ]; then printf '  ok    %-22s %s\n' "bundle unchanged" "after an unflagged run"
else printf '  FAIL  %-22s %s\n' "bundle written" "$(echo $wrote)"; fail=1; fi

# A version number is only worth something if it is the code that runs: the app's own label,
# the install's VERSION and the daemons' serving.version must all be the package it carries.
echo "[smoke] one version everywhere"
want="$(run -c 'import mypeople; print(mypeople.__version__)')"
label="$(defaults read "$APP/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null)"
for spec in "app label:$label" "VERSION:$(cat "$PREFIX/VERSION" 2>/dev/null)" \
            "serving.version:$(cat "$PREFIX/run/serving.version" 2>/dev/null)"; do
  name="${spec%%:*}"; got="${spec#*:}"
  if [ "$got" = "$want" ]; then printf '  ok    %-22s %s\n' "$name" "$got"
  else printf '  FAIL  %-22s %s, the app carries %s\n' "$name" "${got:-missing}" "$want"; fail=1; fi
done

[ "$fail" = 0 ] && echo "[smoke] PASS" || echo "[smoke] FAIL"
exit "$fail"
