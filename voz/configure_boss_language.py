"""Opt in to a local Portuguese Boss profile, preserving the distributed profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from install_launchagents import write_preserving
from voice_config import Settings, absolute_path, load


def configure(settings: Settings) -> str:
    roles = Path(settings.install_dir) / "roles"
    registry_path = roles / "registry.json"
    registry = json.loads(registry_path.read_text())
    profile = json.loads((roles / registry["roles"]["boss"]).read_text())
    spec = profile["spec"]
    key = "doctrineRef" if "doctrineRef" in spec else "personalityRef"
    original = (
        Path(settings.install_dir) / "mp-boss-doctrine.md"
        if spec[key] == "mypeople://boss-doctrine"
        else roles / spec[key]
    ).read_text()
    language = f"""## Language

The owner, {json.dumps(settings.name, ensure_ascii=False)}, wants Brazilian Portuguese.
Respond in Brazilian Portuguese in the pane, on cards, and in spoken replies.
This local preference replaces earlier English-only instructions.
Use plain language; preserve technical identifiers and existing code conventions.
When addressed by {json.dumps(settings.cue, ensure_ascii=False)} or a similar transcription,
answer once through the voice plugin's blocking say command. Use short, natural
Portuguese sentences without Markdown. Ignore your own voice and background speech.

"""
    doctrine, count = re.subn(
        r"^## Language\n.*?(?=^## |\Z)",
        lambda _: language,
        original,
        count=1,
        flags=re.MULTILINE | re.DOTALL,
    )
    if count != 1:
        raise ValueError(
            "The Boss profile has no Language section; no settings changed."
        )
    if doctrine == original:
        return registry["roles"]["boss"]
    digest = hashlib.sha256(doctrine.encode()).hexdigest()[:12]
    # Keep updates idempotent without nesting local suffixes on each preference change.
    base = profile["metadata"]["version"].split("-voice-pt-br.")[0]
    if not re.fullmatch(r"[A-Za-z0-9.+-]+", base):
        raise ValueError("Unsupported Boss profile version.")
    version = f"{base}-voice-pt-br.{digest}"
    profile["metadata"]["version"] = version
    spec[key] = f"personalities/boss/{version}.md"
    ref = f"profiles/boss/{version}.json"
    for path, text in (
        (roles / spec[key], doctrine),
        (roles / ref, json.dumps(profile, indent=2, ensure_ascii=False) + "\n"),
    ):
        if path.exists() and path.read_text() != text:
            raise ValueError("A local profile already exists with different content.")
        write_preserving(path, text.encode(), mode=0o444)
    registry["roles"]["boss"] = ref
    write_preserving(
        registry_path, (json.dumps(registry, indent=2) + "\n").encode(), mode=0o444
    )
    return ref


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=absolute_path)
    args = parser.parse_args()
    print("Selected local profile:", configure(load(args.config)))
    print(
        "Running agents were not restarted. Resume the Boss with the new profile to apply it."
    )
