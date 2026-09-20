#!/usr/bin/env bash
# Collect everything the app must carry into src-tauri/runtime/, which tauri.conf.json ships
# as a bundle resource. Run this before `cargo tauri build`.
#
# The app carries what Homebrew used to provide: python3, ttyd, tmux, asciinema, plus the
# mypeople package itself. git and curl are on every Mac already and are NOT carried.
#
# Homebrew's ttyd and tmux link against Homebrew dylibs that do not exist on a stranger's
# machine, so every non-system dylib is copied in beside them and the load commands are
# rewritten to @executable_path. asciinema links only system frameworks and is copied as-is.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(dirname "$HERE")"
REPO="$(dirname "$DESKTOP")"
OUT="$DESKTOP/src-tauri/runtime"

# python-build-standalone is relocatable by construction (it loads libpython via @rpath).
# uv ships exactly that, so a tree it already manages is the source rather than a download.
# Any relocatable CPython >= 3.9 works; override to point at your own.
PYTHON_SRC="${PYTHON_SRC:-$HOME/.local/share/uv/python/cpython-3.13-macos-aarch64-none}"

say() { printf '[bundle] %s\n' "$*"; }

rm -rf "$OUT"
mkdir -p "$OUT/bin" "$OUT/lib" "$OUT/pylib"

# ---------------------------------------------------------------- python
[ -x "$PYTHON_SRC/bin/python3" ] || {
  echo "[bundle] no relocatable python at $PYTHON_SRC" >&2
  echo "[bundle] install one with: uv python install 3.13" >&2
  exit 1
}
say "python  <- $PYTHON_SRC"
# -L resolves uv's symlinked tree into a real one. Tests and caches are dead weight in a DMG.
cp -RL "$PYTHON_SRC/" "$OUT/python/"
chmod -R u+w "$OUT/python"
# Nothing in the runtime imports tkinter, and tcl/tk is the single largest thing in the tree.
rm -rf "$OUT/python/lib/python3."*/test "$OUT/python/lib/python3."*/idlelib \
       "$OUT/python/lib/python3."*/tkinter "$OUT/python/lib/tcl"* "$OUT/python/lib/tk"* \
       "$OUT/python/lib/itcl"* "$OUT/python/lib/thread"* "$OUT/python/share"
find "$OUT/python" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------- mypeople itself
# Pure stdlib (pyproject dependencies = []), so the package tree IS the install. Copying it
# keeps firstrun.runtime_dir() — which resolves relative to the package — pointing at the
# runtime/ that ships with it.
say "mypeople <- $REPO/mypeople"
cp -R "$REPO/mypeople" "$OUT/pylib/mypeople"
# The same paths .gitignore holds back: a developer's playwright install is ~500MB of
# browser that no install needs, and it is not part of the product.
rm -rf "$OUT/pylib/mypeople/runtime/verify/node_modules" \
       "$OUT/pylib/mypeople/runtime/verify/videos" \
       "$OUT/pylib/mypeople/runtime/verify/browser.out"
find "$OUT/pylib" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---------------------------------------------------------------- native binaries
# Recursively vendor a Mach-O file's non-system dylibs and repoint its load commands.
vendor_deps() {
  local target="$1" dep base
  # Skip the install-name line (first line of otool output for a dylib) and system paths that
  # every Mac already has: those must keep resolving to the OS copy, not a frozen one.
  while read -r dep; do
    case "$dep" in
      /usr/lib/*|/System/*|@*|'') continue ;;
    esac
    base="$(basename "$dep")"
    if [ ! -f "$OUT/lib/$base" ]; then
      cp "$dep" "$OUT/lib/$base"
      chmod u+w "$OUT/lib/$base"
      install_name_tool -id "@executable_path/../lib/$base" "$OUT/lib/$base" 2>/dev/null
      vendor_deps "$OUT/lib/$base"          # its own dependencies come along too
    fi
    install_name_tool -change "$dep" "@executable_path/../lib/$base" "$target" 2>/dev/null
  done < <(otool -L "$target" | tail -n +2 | awk '{print $1}')
}

for tool in ttyd tmux asciinema; do
  src="$(command -v "$tool")" || { echo "[bundle] $tool not found on PATH" >&2; exit 1; }
  say "$tool <- $src"
  cp "$src" "$OUT/bin/$tool"
  chmod u+w "$OUT/bin/$tool"
  vendor_deps "$OUT/bin/$tool"
done

# install_name_tool invalidates the signature it rewrote, and arm64 macOS SIGKILLs any
# unsigned Mach-O — the binary dies with 137 and prints nothing. Ad-hoc signing makes them
# runnable now; `cargo tauri build` re-signs the whole bundle with the Developer ID for a
# release. Dylibs before the binaries that load them.
# The role store ships its versioned files read-only (444) because a role version is
# immutable once published. Tauri copies resources mode and all, so the second `cargo tauri
# build` then dies with "Permission denied (os error 13)" trying to overwrite its own output.
# Only this staging copy is relaxed; the repo keeps its modes, and firstrun._replace_file
# already handles read-only destinations when it materializes an install.
chmod -R u+w "$OUT"

#
# With APPLE_SIGNING_IDENTITY set (the release workflow), EVERY Mach-O in the tree is signed
# with the Developer ID, hardened runtime and a timestamp: notarization rejects the whole DMG
# over a single unsigned or ad-hoc file nested in Resources, and python alone ships ~70 .so's.
IDENTITY="${APPLE_SIGNING_IDENTITY:--}"
if [ "$IDENTITY" = "-" ]; then
  say "signing (ad-hoc — set APPLE_SIGNING_IDENTITY for a release)"
  find "$OUT/lib" -name '*.dylib' -exec codesign --force --sign - {} \; 2>/dev/null
  for tool in ttyd tmux asciinema; do codesign --force --sign - "$OUT/bin/$tool" 2>/dev/null; done
else
  say "signing with: $IDENTITY"
  # Libraries first, executables last: a binary's signature seals the libraries it loads.
  find "$OUT" -type f \( -name '*.dylib' -o -name '*.so' \) -print0 |
    xargs -0 codesign --force --timestamp --options runtime --sign "$IDENTITY"
  find "$OUT/bin" "$OUT/python/bin" -type f -perm -u+x -print0 |
    while IFS= read -r -d '' f; do
      file -b "$f" | grep -q Mach-O &&
        codesign --force --timestamp --options runtime --sign "$IDENTITY" "$f"
    done
fi

# A rewrite that silently missed one dylib produces an app that dies only on a machine
# without Homebrew — exactly the machine we cannot test on. Fail here instead.
leaked=0
while read -r f; do
  bad="$(otool -L "$f" | tail -n +2 | awk '{print $1}' | grep -c '^/opt/homebrew\|^/usr/local' || true)"
  [ "$bad" -eq 0 ] || { echo "[bundle] UNVENDORED dep in $f:" >&2
                        otool -L "$f" | grep '/opt/homebrew\|/usr/local' >&2; leaked=1; }
done < <(find "$OUT/bin" "$OUT/lib" -type f -perm -u+x)
[ "$leaked" -eq 0 ] || { echo "[bundle] refusing to ship a Homebrew-dependent bundle" >&2; exit 1; }

# The check that actually fails if any of the above is wrong: run each carried binary with an
# empty environment. A missing dylib or a broken signature is a SIGKILL (137) and silence, so
# checking the exit code is the only way to notice.
for probe in "bin/ttyd --version" "bin/tmux -V" "bin/asciinema --version" "python/bin/python3 -V"; do
  # shellcheck disable=SC2086  # deliberate: each probe is "binary + flag"
  if ! env -i PATH=/usr/bin:/bin "$OUT"/${probe%% *} ${probe#* } >/dev/null 2>&1; then
    echo "[bundle] carried binary does not run: $probe" >&2
    exit 1
  fi
done

say "ok — $(du -shL "$OUT" | cut -f1) in $OUT, all carried binaries run"
