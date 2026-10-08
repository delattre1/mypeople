"""The YouTube chat plugin is Node (stdlib websocket); its checks live next to it as youtube-chat.test.mjs."""
from pathlib import Path
import shutil
import subprocess
import unittest

TEST = Path(__file__).resolve().parent.parent / "mypeople/runtime/plugins/youtube-chat/youtube-chat.test.mjs"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class YoutubeChatPlugin(unittest.TestCase):
    def test_guards(self):
        r = subprocess.run(["node", str(TEST)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
