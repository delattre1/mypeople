# Read from the package so a release bump cannot leave this behind (it shipped 0.3.1
# images from 0.3.2 source).
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' mypeople/__init__.py)

.PHONY: wheel image verify clean live app released

wheel:
	uv build --wheel --out-dir dist

image:
	docker build -t mypeople:$(VERSION) .

verify:
	mypeople verify

clean:
	rm -rf build .hatch dist/*.whl

# The one way a release reaches this Mac's live install. `mypeople up` from this checkout copies
# the runtime into INSTALL_DIR and only then stamps VERSION, restarts every daemon and plugin still
# serving older code, and stamps run/serving.version -- so the version shown is the code running.
# Never write those two files by hand: on 10-03 three fixes were stamped live that were not running.
# env -i because daemons inherit this environment and an agent's shell carries its own AGENT_ID,
# TMUX and session; PATH is the one the desktop app gives them (desktop/src-tauri/src/main.rs).
# A version number names exactly one tree. On 10-03 two commits both read 5.21.17, and untagged
# 5.21.26-28 reached the live install by hand. So only the commit tagged v$(VERSION), with nothing
# uncommitted under mypeople/, may become a live install or an app.
released:
	@test "$$(git describe --exact-match --tags HEAD 2>/dev/null)" = "v$(VERSION)" || \
	  { echo "HEAD is not tag v$(VERSION): bump __version__, commit, tag that commit, push the tag" >&2; exit 1; }
	@test -z "$$(git status --porcelain -- mypeople)" || \
	  { echo "uncommitted changes under mypeople/ would ship as v$(VERSION)" >&2; exit 1; }

APP_RT := /Applications/MyPlow.app/Contents/Resources/runtime
live: released
	env -i HOME="$$HOME" USER="$$USER" LOGNAME="$$LOGNAME" SHELL="$$SHELL" TMPDIR="$$TMPDIR" \
	  LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 SSH_AUTH_SOCK="$$SSH_AUTH_SOCK" \
	  PATH="$(APP_RT)/bin:$(APP_RT)/python/bin:$$HOME/.local/bin:$$HOME/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
	  python3 -m mypeople.cli up --detach

# MyPlow.app carrying this checkout, as a DMG. The app's version is the package it carries, not the
# literal in tauri.conf.json, which had the app reading 5.14.0 over a 5.21 runtime. The target dir
# stays outside the repo, and the built .app is deleted once the smoke test passes: macOS reopens
# any MyPlow.app it can find, and a stale build adopted the live install four times in September.
# Set APPLE_SIGNING_IDENTITY to sign with the Developer ID; unset builds ad-hoc.
APP_TARGET ?= $(HOME)/launch-build/tauri-target-release
app: released
	rm -rf $(APP_TARGET)/release/bundle/macos/MyPlow.app $(APP_TARGET)/release/bundle/dmg/MyPlow_*.dmg
	desktop/scripts/bundle-runtime.sh
	cd desktop && CI=true CARGO_TARGET_DIR=$(APP_TARGET) cargo tauri build --config '{"version":"$(VERSION)"}'
	desktop/scripts/smoke-test.sh $(APP_TARGET)/release/bundle/macos/MyPlow.app
	rm -rf $(APP_TARGET)/release/bundle/macos/MyPlow.app
