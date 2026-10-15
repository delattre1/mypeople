#!/usr/bin/env bash
# Build the ttyd the app carries: ttyd 1.7.7 on a libwebsockets with libuv compiled in.
#
# Homebrew's libwebsockets loads its libuv support at runtime as a plugin, from a folder compiled
# into it (Homebrew's Cellar). That folder exists on no Mac without Homebrew, so the carried ttyd
# died there with "failed to load evlib_uv" and served no terminal (the CEO's second Mac, 10-15).
# Built here, libwebsockets is linked into ttyd statically with no plugin to load. libuv, json-c and
# OpenSSL still come from Homebrew as dylibs; bundle-runtime.sh vendors them like any other.
#
#   build-ttyd.sh OUT_DIR     -> OUT_DIR/ttyd
#
# CMAKE overrides the cmake to run (e.g. CMAKE="uvx cmake" where cmake is not installed).
set -euo pipefail
OUT="${1:?usage: build-ttyd.sh OUT_DIR}"
LWS_VERSION=4.3.5
TTYD_VERSION=1.7.7
CMAKE="${CMAKE:-cmake}"
BREW="$(brew --prefix)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
say() { printf '[ttyd] %s\n' "$*"; }

say "libwebsockets $LWS_VERSION (static, libuv built in)"
curl -fsSL "https://github.com/warmcat/libwebsockets/archive/refs/tags/v$LWS_VERSION.tar.gz" | tar -xz -C "$WORK"
$CMAKE -S "$WORK/libwebsockets-$LWS_VERSION" -B "$WORK/lws-build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
  -DCMAKE_INSTALL_PREFIX="$WORK/lws" \
  -DLWS_WITH_LIBUV=ON -DLWS_WITH_EVLIB_PLUGINS=OFF \
  -DLWS_WITH_SHARED=OFF -DLWS_WITH_STATIC=ON -DLWS_STATIC_PIC=ON \
  -DLWS_WITHOUT_TESTAPPS=ON -DLWS_WITHOUT_CLIENT=ON -DLWS_UNIX_SOCK=ON -DLWS_IPV6=ON \
  -DLWS_WITH_HTTP2=ON -DLWS_WITH_UDP=OFF -DLWS_ROLE_RAW_FILE=OFF \
  -DLWS_WITH_LEJP=OFF -DLWS_WITH_LEJP_CONF=OFF -DLWS_WITH_LWSAC=OFF -DLWS_WITH_SEQUENCER=OFF \
  -DLIBUV_INCLUDE_DIRS="$BREW/opt/libuv/include" -DLIBUV_LIBRARIES="$BREW/opt/libuv/lib/libuv.dylib" \
  -DOPENSSL_ROOT_DIR="$BREW/opt/openssl@3" >/dev/null
$CMAKE --build "$WORK/lws-build" --target install -j 4 >/dev/null
# Its exported config names the shared library too, which a static-only build never makes, and
# ttyd links whatever that list says.
cfg="$(find "$WORK/lws" -name libwebsockets-config.cmake | head -1)"
[ -n "$cfg" ] || { echo "[ttyd] no libwebsockets-config.cmake under $WORK/lws" >&2; exit 1; }
perl -pi -e 's/ websockets_shared//' "$cfg"

say "ttyd $TTYD_VERSION"
curl -fsSL "https://github.com/tsl0922/ttyd/archive/refs/tags/$TTYD_VERSION.tar.gz" | tar -xz -C "$WORK"
$CMAKE -S "$WORK/ttyd-$TTYD_VERSION" -B "$WORK/ttyd-build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
  -DCMAKE_PREFIX_PATH="$WORK/lws;$BREW/opt/libuv;$BREW/opt/json-c;$BREW/opt/openssl@3" >/dev/null
$CMAKE --build "$WORK/ttyd-build" -j 4 >/dev/null

mkdir -p "$OUT"
cp "$WORK/ttyd-build/ttyd" "$OUT/ttyd"
say "built $OUT/ttyd"
