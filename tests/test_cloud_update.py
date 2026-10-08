"""cloud/update.py pulls ONE layer of the pinned image -- the release dir -- and nothing it can't verify.

A fake registry in-process: it demands an anonymous bearer token first (the public ECR dance),
serves an index -> amd64 manifest -> config with the myplow.release label -> gzip layer.
"""
import gzip
import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def sha(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


def layer_with(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            t.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue())


def registry(blobs, manifests):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/token"):
                return self.reply(200, json.dumps({"token": "anon"}).encode())
            if self.headers.get("Authorization") != "Bearer anon":
                self.send_response(401)
                self.send_header("WWW-Authenticate",
                                 f'Bearer realm="http://127.0.0.1:{self.server.server_port}/token",'
                                 'service="test",scope="repository:myplow:pull"')
                return self.end_headers()
            ref = self.path.rsplit("/", 1)[1]
            body = manifests.get(ref) or blobs.get(ref)
            self.reply(200 if body else 404, body or b"")

        def reply(self, code, body):
            self.send_response(code)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class CloudUpdatePull(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        (root / "releases").mkdir()
        with mock.patch.dict(os.environ, {"MYPLOW_ROOT": str(root), "MYPLOW_REGISTRY_SCHEME": "http"}):
            spec = importlib.util.spec_from_file_location("update", ROOT / "cloud" / "update.py")
            self.u = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.u)
        self.root = root
        self.layer = layer_with({"opt/myplow/current": b"", "opt/myplow/releases/9.9.9/run.sh": b"#!/bin/sh\n",
                                 "opt/myplow/releases/9.9.9/RELEASE": b"9.9.9\n", "etc/passwd": b"no"})
        config = json.dumps({"config": {"Labels": {"myplow.release": "9.9.9"}}}).encode()
        base = gzip.compress(b"base layer")
        man = json.dumps({"config": {"digest": sha(config)},
                          "layers": [{"digest": sha(base)}, {"digest": sha(self.layer),
                                     "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]}).encode()
        index = json.dumps({"manifests": [
            {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": sha(man), "platform": {"os": "linux", "architecture": "amd64"}}]}).encode()
        self.blobs = {sha(config): config, sha(base): base, sha(self.layer): self.layer}
        self.srv = registry(self.blobs, {"sha256:idx": index, sha(man): man})
        self.image = f"127.0.0.1:{self.srv.server_port}/myplow@sha256:idx"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.td.cleanup()

    def test_stages_only_the_release_dir_of_the_last_layer(self):
        release, layer, reg = self.u.release_of(self.image)
        self.assertEqual("9.9.9", release)
        self.u.stage(release, layer, reg)
        self.assertEqual("9.9.9\n", (self.root / "releases/9.9.9/RELEASE").read_text())
        self.assertFalse((self.root / "etc").exists(), "only opt/myplow/releases/<release>/ may land")
        self.assertEqual(["9.9.9"], [p.name for p in (self.root / "releases").iterdir()])

    def test_a_layer_that_does_not_match_its_digest_is_refused(self):
        release, layer, reg = self.u.release_of(self.image)
        self.blobs[layer["digest"]] = layer_with({"opt/myplow/releases/9.9.9/run.sh": b"evil"})
        with self.assertRaises(ValueError):
            self.u.stage(release, layer, reg)
        self.assertFalse((self.root / "releases/9.9.9").exists())


class CloudUpdateIdleGate(unittest.TestCase):
    """A turn is never cut, but a resumed agent nobody has prompted yet is not mid-turn."""

    def busy(self, statuses):
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.dict(os.environ, {"MYPEOPLE_HOME": td, "MYPLOW_ROOT": td}):
            spec = importlib.util.spec_from_file_location("update", ROOT / "cloud" / "update.py")
            u = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(u)
            for win, (status, age) in statuses.items():
                d = Path(td) / "status" / "mc-main"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{win}.json").write_text(json.dumps({"status": status, "timestamp": u.time.time() - age}))
            windows = "".join(f"main\t{w}\n" for w in statuses)
            with mock.patch.object(u.subprocess, "run", return_value=mock.Mock(stdout=windows)):
                return u.busy_agents()

    def test_working_and_a_fresh_start_are_busy_an_old_start_and_idle_are_not(self):
        self.assertEqual(["main:Boss", "main:eng-1"], self.busy({
            "Boss": ("working", 900), "eng-1": ("starting", 30),
            "eng-2": ("starting", 1200), "eng-3": ("idle", 5)}))


if __name__ == "__main__":
    unittest.main()
