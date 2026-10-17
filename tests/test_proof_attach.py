"""A proof only counts if the board can actually serve it back to a browser.

The failure this guards against is silent: `mp proof` accepted a local path, the CLI printed
"proof attached", the card stored it, and the page rendered <img src="file:///...">, which no
browser will load from an http origin. Every layer said success and the CEO saw nothing.
"""
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import sandbox


class UnservableProofUrlTests(unittest.TestCase):
    """The server must refuse a URL it could never serve, instead of storing a dead proof."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = sandbox.load(cls.tmp.name, "todo-server.py")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_file_url_is_rejected_with_guidance(self):
        # The exact shape that broke card 5afa0f090b.
        why = self.srv.unservable_proof_url(
            "file:///private/tmp/claude-501/scratchpad/after-board.png")
        self.assertTrue(why, "file:// proof must be rejected")
        self.assertIn("mp proof", why, "rejection must tell the agent what to do instead")

    def test_bare_local_paths_are_rejected(self):
        for bad in ("/tmp/shot.png", "~/shot.png", "./shot.png", "../shot.png"):
            self.assertTrue(self.srv.unservable_proof_url(bad), "%s must be rejected" % bad)

    def test_empty_url_is_rejected(self):
        self.assertTrue(self.srv.unservable_proof_url(""))

    def test_servable_urls_are_allowed(self):
        for good in ("/todo/proof-file/abc-shot.png",
                     "http://localhost:9900/?task=5afa0f090b",
                     "https://github.com/delattre1/mypeople"):
            self.assertEqual(self.srv.unservable_proof_url(good), "",
                             "%s must be allowed" % good)

    def test_uploaded_file_url_is_classified_as_image(self):
        self.assertEqual(
            self.srv.classify_kind(url="/todo/proof-file/abc-shot.png"), "image")


class MultipartRoundTripTests(unittest.TestCase):
    """The upload path is now the supported way to attach a screenshot, so it must return the
    exact bytes it was given. A payload ending in 0x0a is where a naive rstrip() corrupts."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = sandbox.load(cls.tmp.name, "todo-server.py")
        cls.common = sandbox.load(cls.tmp.name, "mpcommon.py")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _round_trip(self, payload, name="shot.png"):
        """Build a body with the client encoder, parse it with the server parser."""
        src = Path(self.tmp.name) / name
        src.write_bytes(payload)
        captured = {}

        class FakeReq:
            def __init__(self, url, data=None, headers=None, method=None):
                captured["data"] = data
                captured["headers"] = headers or {}

        with mock.patch.object(self.common.urllib.request, "Request", FakeReq), \
             mock.patch.object(self.common.urllib.request, "urlopen",
                               side_effect=OSError("no server")):
            self.common.http_upload("http://x/todo/proof", {"task_id": "t1", "body": "note"},
                                    str(src))

        class FakeHandler:
            def __init__(self, body):
                import io
                self.rfile = io.BytesIO(body)
                self.headers = {"Content-Length": str(len(body))}

        h = FakeHandler(captured["data"])
        return self.srv.parse_multipart(h, captured["headers"]["Content-Type"])

    def test_fields_and_file_survive(self):
        fields, fileobj = self._round_trip(b"\x89PNG\r\n\x1a\nBODY")
        self.assertEqual(fields.get("task_id"), "t1")
        self.assertEqual(fields.get("body"), "note")
        self.assertIsNotNone(fileobj, "file part must be parsed")
        self.assertEqual(fileobj[0], "shot.png")
        self.assertEqual(fileobj[2], b"\x89PNG\r\n\x1a\nBODY")

    def test_payload_ending_in_newline_is_not_truncated(self):
        payload = b"\x89PNG\r\n\x1a\ndata\r\n\n"
        _, fileobj = self._round_trip(payload)
        self.assertEqual(fileobj[2], payload,
                         "trailing CR/LF bytes belong to the file, not the delimiter")


class LocalProofPathTests(unittest.TestCase):
    """`mp proof` must recognise what has to be uploaded rather than linked."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.mp = sandbox.load(cls.tmp.name, "mp")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_file_url_resolves_to_a_path_to_upload(self):
        self.assertEqual(
            self.mp._local_proof_path("file:///private/tmp/scratchpad/after-board.png"),
            "/private/tmp/scratchpad/after-board.png")

    def test_percent_escapes_are_decoded(self):
        self.assertEqual(self.mp._local_proof_path("file:///tmp/my%20shot.png"),
                         "/tmp/my shot.png")

    def test_plain_path_is_uploaded(self):
        self.assertEqual(self.mp._local_proof_path("/tmp/x/shot.png"), "/tmp/x/shot.png")

    def test_http_and_board_urls_stay_links(self):
        for link in ("http://localhost:9900/?task=1", "https://example.com/a.png",
                     "/todo/proof-file/abc.png"):
            self.assertIsNone(self.mp._local_proof_path(link),
                              "%s must stay a link proof" % link)


if __name__ == "__main__":
    unittest.main()
