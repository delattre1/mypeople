#!/usr/bin/env python3
"""Keep this cloud MyPlow on the release Plow pins for it, on the SAME VM and the SAME disk.

Plow cannot give a running agent a new image: exe.dev only has new/restart/rm, and the image is
unpacked once onto the VM's disk. The old way to ship a fix was delete + re-create, which archives
the owner's chat and throws the disk (board, sessions, memory) away. This replaces that:

  1. read the pin: GET $PLOW_API_BASE/v1/agent-images/<slug> (public; "promote" is what moves it)
  2. if its release differs from ours, download ONLY the image's last layer -- the release dir,
     by construction of cloud/Dockerfile -- verify it against its digest, stage it beside ours
  3. wait until every live agent is idle (a turn is never cut), then ask PID 1 to switch:
     write run/myplow-update-request, stop the fleet. entrypoint.sh (never replaced by an update)
     snapshots the data, flips /opt/myplow/current, boots the new release, health-checks it and
     rolls back to the old one, data included, if it is not healthy.

An updated VM runs exactly the bytes a fresh VM of that pin would: the release comes out of the
same published image. ponytail: only the release dir moves (MyPlow, Claude Code, these scripts).
OS packages, browsers, codex/grok and python stay at the VM's birth image; changing those still
needs a new VM.

  update.py loop      check every MYPLOW_UPDATE_EVERY seconds (entrypoint starts this)
  update.py check     one pass now; prints what it did
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("MYPLOW_ROOT", "/opt/myplow"))
DATA = Path(os.environ.get("MYPEOPLE_HOME", "/var/lib/mypeople"))
RUN = DATA / "run"
REQUEST = RUN / "myplow-update-request"   # staged release name; entrypoint.sh acts on it
BAD = RUN / "myplow-update-bad"           # images that failed their health check: never retried
SLUG = os.environ.get("MYPLOW_SLUG", "")
API = (os.environ.get("PLOW_API_BASE") or "https://api.plow.co").rstrip("/")
EVERY = int(os.environ.get("MYPLOW_UPDATE_EVERY", "300"))
IDLE_WAIT = int(os.environ.get("MYPLOW_UPDATE_IDLE_WAIT", "600"))
SCHEME = os.environ.get("MYPLOW_REGISTRY_SCHEME", "https")  # http only for a local test registry
# The owner's login server: env, or the file on this VM's data disk. No default; no beacons without it.
def _bank():
    try:
        return os.environ.get("MYPLOW_CLAUDE_BANK") or open("/var/lib/mypeople/state/claude-bank-url").read().strip()
    except OSError:
        return ""
BANK = _bank()
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def log(msg, beacon=False):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "myplow-update:", msg, flush=True)
    if beacon and BANK:  # the owner's login server keeps these: the only view into a VM nobody can log into
        try:
            urllib.request.urlopen(urllib.request.Request(
                BANK + "/beacon", data=f"{os.uname().nodename} update: {msg}".encode()), timeout=5).close()
        except OSError:
            pass


def current_release():
    return os.path.basename(os.path.realpath(ROOT / "current"))


# --- registry: anonymous OCI pull, stdlib only (the VM has no docker) ---

def parse_ref(ref):
    """'public.ecr.aws/e1h7x4a2/myplow-cloud@sha256:..' -> (host, repo, reference)."""
    host, _, rest = ref.partition("/")
    if "@" in rest:
        repo, _, reference = rest.partition("@")
    else:
        repo, _, reference = rest.rpartition(":") if ":" in rest else (rest, "", "latest")
    return host, repo, reference


class Registry:
    def __init__(self, host, repo):
        self.base, self.repo, self.token = f"{SCHEME}://{host}/v2/{repo}", repo, None

    def _open(self, path, accept=None):
        req = urllib.request.Request(self.base + path)
        if accept:
            req.add_header("Accept", accept)
        if self.token:
            # Not forwarded on a redirect: blob storage refuses a second credential.
            req.add_unredirected_header("Authorization", "Bearer " + self.token)
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate", "")
            if e.code != 401 or self.token or not challenge.startswith("Bearer "):
                raise
            # Anonymous bearer: the same dance `docker pull` does against a public repo.
            params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
            url = params.pop("realm") + "?" + "&".join(
                f"{k}={v}" for k, v in params.items() if k in ("service", "scope"))
            with urllib.request.urlopen(url, timeout=30) as r:
                body = json.load(r)
            self.token = body.get("token") or body.get("access_token")
            return self._open(path, accept)

    def json(self, path, accept=None):
        with self._open(path, accept) as r:
            return json.load(r)

    def blob_to(self, digest, dest):
        """Stream a blob to `dest` and refuse it unless its sha256 is the digest asked for."""
        h = hashlib.sha256()
        with self._open(f"/blobs/{digest}") as r, open(dest, "wb") as f:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
        if "sha256:" + h.hexdigest() != digest:
            raise ValueError(f"blob {digest[:19]} arrived with a different digest")


def manifest(reg, reference):
    m = reg.json(f"/manifests/{reference}", ACCEPT)
    if "manifests" in m:  # an index: Plow's VMs are linux/amd64
        pick = next(x for x in m["manifests"]
                    if x.get("platform", {}).get("architecture") == "amd64"
                    and x.get("platform", {}).get("os") == "linux")
        m = reg.json(f"/manifests/{pick['digest']}", ACCEPT)
    return m


def release_of(image):
    """(release name, last-layer descriptor, registry) of a pinned image."""
    host, repo, reference = parse_ref(image)
    reg = Registry(host, repo)
    m = manifest(reg, reference)
    config = reg.json(f"/blobs/{m['config']['digest']}")
    release = (config.get("config") or {}).get("Labels", {}).get("myplow.release")
    if not release:
        raise ValueError(f"{image} carries no myplow.release label: not an updatable MyPlow image")
    return release, m["layers"][-1], reg


def stage(release, layer, reg):
    """Put releases/<release> on disk from the image's last layer, all or nothing."""
    dest = ROOT / "releases" / release
    if (dest / "run.sh").exists():
        return dest
    if "zstd" in layer.get("mediaType", ""):
        raise ValueError("zstd layers are not supported; publish gzip")
    prefix = f"opt/myplow/releases/{release}/"
    with tempfile.TemporaryDirectory(dir=ROOT / "releases") as tmp:
        blob = Path(tmp) / "layer"
        reg.blob_to(layer["digest"], blob)
        with tarfile.open(blob) as t:
            members = [m for m in t.getmembers() if m.name.lstrip("./").startswith(prefix)]
            if not any(m.name.lstrip("./") == prefix + "run.sh" for m in members):
                raise ValueError(f"the last layer holds no {prefix}run.sh")
            if any(".." in Path(m.name).parts or (m.issym() and os.path.isabs(m.linkname))
                   for m in members):
                raise ValueError("the release layer reaches outside its own directory")
            # The VM's python (3.12) applies tarfile's "data" filter too; an older one (the repo's
            # test gate) has only the checks above.
            safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            t.extractall(tmp, members=members, **safe)
        os.replace(Path(tmp) / prefix, dest)
    log(f"staged {release} from {layer['digest'][:19]}", beacon=True)
    return dest


# --- the fleet: never cut a turn ---

STARTING_GRACE = 180  # a revived agent stays "starting" until its next prompt: only a young one is busy


def busy_agents():
    """Live agents (a tmux window exists) that are mid-turn: "working", or "starting" for under
    STARTING_GRACE. An agent resumed after a restart sits at "starting" until something prompts it,
    so an old "starting" is an idle agent -- counting it as busy would block every update after the
    first one."""
    out = subprocess.run(["tmux", "list-windows", "-a", "-F", "#{session_name}\t#{window_name}"],
                         capture_output=True, text=True).stdout
    busy = []
    for line in out.splitlines():
        sess, _, win = line.partition("\t")
        try:
            st = json.loads((DATA / "status" / f"mc-{sess}" / f"{win}.json").read_text())
        except (OSError, ValueError):
            continue
        status, age = st.get("status"), time.time() - float(st.get("timestamp") or 0)
        if status == "working" or (status == "starting" and age < STARTING_GRACE):
            busy.append(f"{sess}:{win}")
    return busy


def wait_idle():
    deadline = time.time() + IDLE_WAIT
    while (busy := busy_agents()) and time.time() < deadline:
        time.sleep(10)
    return not busy


def stop_fleet():
    """Agents and daemons down; `mypeople up` returns and PID 1 takes it from there."""
    subprocess.run(["tmux", "kill-server"], capture_output=True)
    subprocess.run(["mypeople", "down"], capture_output=True)


def bad_images():
    try:
        return set(BAD.read_text().split())
    except OSError:
        return set()


def check():
    if not SLUG:
        return "no slug (not a 1-click deploy): nothing pins this agent"
    try:
        with urllib.request.urlopen(f"{API}/v1/agent-images/{SLUG}", timeout=20) as r:
            image = json.load(r).get("image")
    except (OSError, ValueError) as e:
        return f"pin unreadable: {e}"
    if not image or image in bad_images():
        return f"nothing to do (pin {'bad' if image else 'empty'})"
    release, layer, reg = release_of(image)
    if release == current_release():
        return f"up to date: {release}"
    stage(release, layer, reg)
    if not wait_idle():
        return f"{release} staged; agents stayed busy, switching next pass"
    RUN.mkdir(parents=True, exist_ok=True)
    REQUEST.write_text(f"{release} {image}\n")
    log(f"switch requested: {current_release()} -> {release}", beacon=True)
    stop_fleet()
    return f"switching to {release}"


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "loop"
    if cmd == "check":
        print(check())
        return
    if cmd != "loop":
        raise SystemExit(__doc__)
    time.sleep(int(os.environ.get("MYPLOW_UPDATE_FIRST", "60")))  # let the fleet settle first
    while True:
        try:
            log(check())
        except Exception as e:  # noqa: BLE001 -- one bad pass must not end the loop
            log(f"check failed: {e}", beacon=True)
        if REQUEST.exists():
            return  # the switch is PID 1's now; this process belongs to the old release
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
