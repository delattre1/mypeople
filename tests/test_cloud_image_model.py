"""The cloud image's Boss runs the model baked into it, and its Claude Code is new enough to.

The Boss is spawned without --model, so cloud/claude-settings.json (re-asserted by run.sh on
every boot) picks the model; a Claude Code older than that model needs answers every turn with a 400 instead.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Claude Code releases that first accepted each baked model (from the 400 the older CLI returns).
MIN_CLI = {"claude-opus-5-5": (2, 1, 280)}


def claude_version(dockerfile):
    m = re.search(r"^ARG CLAUDE_VERSION=([\d.]+)$", (ROOT / dockerfile).read_text(), re.M)
    return tuple(int(p) for p in m.group(1).split("."))


def baked_settings():
    return json.loads((ROOT / "cloud/claude-settings.json").read_text())


class CloudImageModel(unittest.TestCase):
    def test_baked_model_is_supported_by_the_pinned_cli(self):
        model = baked_settings()["model"]
        self.assertIn(model, MIN_CLI, "add the first Claude Code release that accepts %s" % model)
        self.assertGreaterEqual(claude_version("Dockerfile"), MIN_CLI[model])


if __name__ == "__main__":
    unittest.main()
