#!/usr/bin/env bash
# Build what the app carries for an older macOS than the machine building it. Homebrew builds each bottle for one macOS and keeps
# none for 14 any more, while GitHub stopped serving macos-14 runners (11-05): taken from Homebrew on macos-15, tmux needed macOS 15
# and a Mac on 14 could not run the app. So tmux, asciinema and ttyd's libraries are built here from the sources Homebrew names, at
# the versions it builds, for MACOSX_DEPLOYMENT_TARGET. A call to anything newer than that target is a build error, not a crash on
# an older Mac. tmux links the system's ncurses, which every Mac has.
#
#   build-floor.sh PREFIX   -> PREFIX/bin/{tmux,asciinema}, PREFIX/lib/{libssl,libcrypto,libuv,libjson-c,libevent,libutf8proc}
#
# CMAKE overrides the cmake to run, as in build-ttyd.sh. ttyd itself is built by build-ttyd.sh with DEPS=PREFIX.
set -euo pipefail
P="${1:?usage: build-floor.sh PREFIX}"
mkdir -p "$P"
P="$(cd "$P" && pwd)"
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-14.0}"
export CFLAGS="-O2 -Werror=unguarded-availability-new" PKG_CONFIG_PATH="$P/lib/pkgconfig"
CMAKE="${CMAKE:-cmake}"
J="$(sysctl -n hw.ncpu)"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
say() { printf '[floor] %s\n' "$*"; }
quiet() { "$@" >"$W/log" 2>&1 || { tail -40 "$W/log" >&2; exit 1; }; }
get() {
  url="$(curl -fsSL "https://formulae.brew.sh/api/formula/$1.json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["urls"]["stable"]["url"])')"
  say "$1 <- $url"
  mkdir -p "$W/$1"
  curl -fsSL --retry 4 "$url" | tar -xz -C "$W/$1" --strip-components 1
}
cm() { quiet "$CMAKE" -S "$W/$1" -B "$W/$1/b" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$P" -DCMAKE_INSTALL_LIBDIR=lib \
         -DCMAKE_POLICY_VERSION_MINIMUM=3.5 "${@:2}"; quiet "$CMAKE" --build "$W/$1/b" --target install -j "$J"; }

get openssl@3
(cd "$W/openssl@3" && quiet ./Configure darwin64-arm64-cc shared no-tests no-docs --prefix="$P" --libdir=lib && quiet make -j"$J" && quiet make install_sw)
get libuv;  cm libuv -DLIBUV_BUILD_TESTS=OFF -DLIBUV_BUILD_BENCH=OFF
get json-c; cm json-c -DBUILD_TESTING=OFF -DBUILD_STATIC_LIBS=OFF
get libevent
(cd "$W/libevent" && quiet ./configure --prefix="$P" --disable-openssl --disable-samples --disable-libevent-regress && quiet make -j"$J" && quiet make install)
get utf8proc
quiet make -C "$W/utf8proc" -j"$J" prefix="$P" install
get tmux
# strchrnul: the macOS 15.4 SDK has it and macOS 14 does not, and configure finds it either way, so tmux keeps its own.
(cd "$W/tmux" && quiet ./configure --prefix="$P" --enable-utf8proc ac_cv_func_strchrnul=no && quiet make -j"$J" && quiet make install)
get asciinema
quiet cargo install --locked --path "$W/asciinema" --root "$P"
say "built for macOS $MACOSX_DEPLOYMENT_TARGET in $P: $(ls "$P/bin" | tr '\n' ' ')"
