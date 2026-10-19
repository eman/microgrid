"""The Home Assistant integration's manifest installs this version of the library: its
version and its requirement's pin are the project's version (a release updates the
three together: AGENTS.md, *Releases*)."""

import json
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_the_manifest_pins_this_version() -> None:
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    manifest = json.loads(
        (REPO / "custom_components" / "microgrid" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == version
    assert manifest["requirements"] == [f"microgrid[sources]=={version}"]
