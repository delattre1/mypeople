#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"

have_tty() {
  [ -t 0 ] && [ -t 1 ]
}

# Status probes must never hang: without a TTY a wedged CLI stalls the whole install.
probe() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 30 "$@"
  else
    "$@"
  fi
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "[myplow] root access is required to install host packages: $*" >&2
    return 1
  fi
}

install_host_deps() {
  local missing=()
  for cmd in python3 tmux ttyd asciinema git curl; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  [ ${#missing[@]} -eq 0 ] && return
  echo "[myplow] missing: ${missing[*]}"
  if command -v brew >/dev/null 2>&1; then
    local brew_pkgs=()
    command -v tmux >/dev/null 2>&1 || brew_pkgs+=(tmux)
    command -v ttyd >/dev/null 2>&1 || brew_pkgs+=(ttyd)
    command -v asciinema >/dev/null 2>&1 || brew_pkgs+=(asciinema)
    [ ${#brew_pkgs[@]} -eq 0 ] || brew install "${brew_pkgs[@]}"
  elif command -v apt-get >/dev/null 2>&1; then
    as_root apt-get update
    as_root apt-get install -y python3 tmux git curl asciinema ca-certificates
  fi
  if ! command -v ttyd >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
    local arch asset sha
    arch="$(uname -m)"
    case "$arch" in
      x86_64|amd64) asset=x86_64; sha=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55 ;;
      arm64|aarch64) asset=aarch64; sha=b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165 ;;
      *) asset="" ;;
    esac
    if [ -n "$asset" ]; then
      local tmp
      tmp="$(mktemp)"
      curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.${asset}" -o "$tmp"
      python3 - "$tmp" "$sha" <<'PY'
import hashlib, sys
path, expected = sys.argv[1:]
actual = hashlib.sha256(open(path, "rb").read()).hexdigest()
if actual != expected:
    raise SystemExit("ttyd checksum mismatch: %s" % actual)
PY
      as_root install -m 0755 "$tmp" /usr/local/bin/ttyd
      rm -f "$tmp"
    fi
  fi
  for cmd in python3 tmux ttyd asciinema git curl; do
    command -v "$cmd" >/dev/null 2>&1 || {
      echo "[myplow] $cmd is still missing; install it and rerun ./install.sh" >&2
      exit 2
    }
  done
}

install_host_deps

if ! command -v claude >/dev/null 2>&1 || ! probe claude --version >/dev/null 2>&1; then
  echo "[myplow] installing Claude Code"
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v codex >/dev/null 2>&1 || ! probe codex --version >/dev/null 2>&1; then
  echo "[myplow] installing Codex CLI"
  curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh
  export PATH="$HOME/.local/bin:$PATH"
fi

BACKEND="${MYPEOPLE_BACKEND:-claude}"

# The login flows are all interactive browser handshakes. Called from a pipe, a CI
# job or `ssh host cmd` they print a URL and wait for a keypress that can never
# arrive, so the installer hangs forever. Without a TTY we skip the login, say so,
# and finish the install with auth deferred.
AUTH_PENDING=""
defer_login() {
  AUTH_PENDING="$1"
  cat >&2 <<EOF
[myplow] ============================================================
[myplow] AUTH PENDING: this shell has no TTY, so the interactive
[myplow] '$BACKEND' login was skipped (it would hang forever here).
[myplow] The install continues; agents stay idle until you log in.
[myplow] Finish it from a real terminal:
[myplow]     $1
[myplow]     mypeople up --backend $BACKEND
[myplow] ============================================================
EOF
}

case "$BACKEND" in
  claude)
    probe claude auth status >/dev/null 2>&1 || {
      if have_tty; then claude auth login; else defer_login "claude auth login"; fi
    }
    ;;
  codex)
    probe codex login status >/dev/null 2>&1 || {
      if have_tty; then codex login; else defer_login "codex login"; fi
    }
    ;;
  grok)
    # Not auto-installed: grok ships an internal self-updater and publishes no install
    # script we can pin, so we require an operator-installed CLI instead of curl|bash.
    if ! command -v grok >/dev/null 2>&1; then
      echo "[myplow] MYPEOPLE_BACKEND=grok but the grok CLI is not on PATH." >&2
      echo "[myplow] Install Grok yourself, then re-run this installer." >&2
      exit 2
    fi
    # `grok models` exits 0 even when logged out, so the stdout marker is the only real check.
    probe grok models 2>&1 | grep -qi "you are logged in" || {
      if have_tty; then grok login; else defer_login "grok login"; fi
    }
    ;;
  *)
    echo "[myplow] MYPEOPLE_BACKEND must be claude, codex or grok" >&2
    exit 2
    ;;
esac
export MYPEOPLE_BACKEND="$BACKEND"

if ! command -v uv >/dev/null 2>&1; then
  echo "[myplow] installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

cd "$ROOT"
uv build --wheel --out-dir dist
WHEEL="$(ls -t dist/mypeople-*.whl | head -1)"
uv tool install --force "$WHEEL"
export PATH="$HOME/.local/bin:$PATH"
UP_ARGS=(up --detach --backend "$MYPEOPLE_BACKEND")
if [ -n "$AUTH_PENDING" ]; then
  # With the login deferred, `up` refuses to start unauthenticated agents. That refusal
  # is correct, not an install failure: the package is installed and only the login is
  # missing, so report what is left to do instead of dying on its exit status.
  mypeople "${UP_ARGS[@]}" || true
  echo "[myplow] ============================================================" >&2
  echo "[myplow] MyPlow is INSTALLED, but '$BACKEND' is NOT authenticated." >&2
  echo "[myplow] Nothing is running yet. From a terminal with a TTY, run:" >&2
  echo "[myplow]     $AUTH_PENDING" >&2
  echo "[myplow]     mypeople up --backend $BACKEND" >&2
  echo "[myplow] Then open http://localhost:${TODO_PORT:-9933}" >&2
  echo "[myplow] ============================================================" >&2
  exit 0
fi
mypeople "${UP_ARGS[@]}"
mypeople status
echo "[myplow] open http://localhost:${TODO_PORT:-9933}"
