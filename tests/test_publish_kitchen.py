"""Every release also publishes the Kitchen kind's name, ghcr.io/delattre1/kitchen:<version>, on the MyPlow image's own
digest (card 42d657d0fe): new-myplow starts Kitchen homes from it, so a release without it leaves new-myplow nothing to move to."""
import re
import unittest
from pathlib import Path

WF = Path(__file__).resolve().parents[1] / ".github/workflows/publish-container.yml"


class PublishKitchen(unittest.TestCase):
    def test_every_release_tags_the_kitchen_name_on_the_same_digest(self):
        m = re.search(r"^  kitchen:\n(.*?)(?=^  [a-z]+:\n|\Z)", WF.read_text(), re.M | re.S)
        self.assertIsNotNone(m, "publish-container.yml has no kitchen job")
        job = m.group(1)
        self.assertIn("needs: merge", job)  # its own job after the merge: a kitchen failure never holds back the cloud image
        self.assertIn("if: github.event_name == 'push'", job)  # real releases only, never a dispatch build's 0.0.0 tag
        self.assertIn("v='${{ needs.merge.outputs.version }}'", job)
        self.assertIn('imagetools create -t "ghcr.io/delattre1/kitchen:$v" "${IMAGE}:$v"', job)
        self.assertIn('[ "$a" = "$b" ]', job)  # and fails unless both names give the same index


if __name__ == "__main__":
    unittest.main()
