# MyPlow desktop app (Tauri)

MyPlow as a Mac app: download the DMG, drag it to Applications, double-click. The app brings
everything Homebrew used to provide, so nothing else has to be installed except the user's own
AI CLI login.

## What the app carries

| | from | why |
|---|---|---|
| python 3.13 | python-build-standalone (via uv) | runs the daemons; the runtime is pure stdlib |
| ttyd 1.7.7 | Homebrew, dylibs vendored | the terminal tiles are real ttyd iframes |
| tmux | Homebrew, dylibs vendored | every agent lives in a tmux pane |
| asciinema | Homebrew (system libs only) | session recording |
| the `mypeople` package | this repo | `mypeople up` / `down` and the runtime it materializes |

git and curl ship with macOS and are not carried. Size: ~64 MB app, ~24 MB DMG.

## How it runs

`src-tauri/src/main.rs` has no logic of its own. It puts the carried `bin/` directories first on
PATH, runs `python3 -m mypeople.cli up --detach`, and then points the window at the board URL
the CLI reports. From there `supervise.sh` starts and respawns the seven daemons as usual.
Quitting runs `mypeople down`. As with the CLI, that stops the daemons and leaves agents in tmux
so the next launch resumes them.

`MYPEOPLE_DESKTOP=1` makes first run come up in "board + HUD, no login yet" mode instead of
exiting, because a double-clicked app has no terminal to show the refusal in. The login is then
done from the terminal tab inside the window.

## Build (local, ad-hoc signed)

```sh
./scripts/bundle-runtime.sh          # stage runtime into src-tauri/runtime/, fails if any binary cannot run
CI=true cargo tauri build            # CI=true skips the Finder-layout AppleScript (needs Automation permission)
./scripts/smoke-test.sh              # isolated from any live install; asserts every port is served by a carried binary
```

Requires the Rust toolchain, `cargo install tauri-cli --version "^2"`, `uv python install 3.13`, and
`brew install ttyd tmux asciinema` **on the build machine only**.

## Release (signed + notarized DMG)

Run the `Desktop release (signed DMG)` workflow (`.github/workflows/desktop-release.yml`). It uses
the Plow Developer ID and notary credentials Latch ships with, from the same 1Password item, and
needs the `OP_SERVICE_ACCOUNT_TOKEN` repo secret. With `APPLE_SIGNING_IDENTITY` set,
`bundle-runtime.sh` signs every nested Mach-O with hardened runtime before Tauri signs and
notarizes the app.

## Known limits

- **arm64 only.** The binaries are copied from the build machine. An Intel/universal DMG would need
  x86_64 copies of each one, fused with `lipo`.
- **Version** lives in both `tauri.conf.json` and `Cargo.toml` (currently 5.14.0). Bump them with the
  product release.
