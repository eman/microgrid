"""MkDocs hooks for the documentation site (mkdocs.yml): the docs' build only.

``docs/changelog.md`` is the repository's ``CHANGELOG.md``, read at build time, whose
links are from the repository's root (``docs/...``); on the site they are from
``docs/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def on_page_markdown(markdown: str, *, page: Any, config: Any, **kwargs: Any) -> str:
    """The changelog page: ``CHANGELOG.md``, its links made relative to the docs."""
    if page.file.src_uri != "changelog.md":
        return markdown
    root = Path(config.config_file_path).parent
    return (root / "CHANGELOG.md").read_text(encoding="utf-8").replace("](docs/", "](")
