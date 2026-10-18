"""A griffe extension for the documentation site (mkdocs.yml): the code's docstrings as
the API pages render them.

- **``#:`` comments** document an attribute (a dataclass field, a module constant) the
  way Sphinx reads them; griffe does not. Each attribute without a docstring takes the
  ``#:`` lines just above it.
- **Sphinx roles** (``:class:`~a.b.C```, ``:func:`f```, ...) are not Markdown. Each
  becomes inline code naming the object (the last part, where ``~`` asks for it).

The docs' build only: nothing in the library imports it.
"""

from __future__ import annotations

import ast
import re
from typing import Any

import griffe

_ROLE = re.compile(r":(?:class|func|meth|attr|data|mod|exc|obj):`(~?)([^`]+)`")


def _role(match: re.Match[str]) -> str:
    short, target = match.group(1), match.group(2)
    name = target.rsplit(".", 1)[-1] if short else target.lstrip(".")
    return f"`{name}`"


def _markdown(text: str) -> str:
    return _ROLE.sub(_role, text)


def _comment_doc(attr: griffe.Attribute) -> str | None:
    """The ``#:`` lines just above ``attr``, as its docstring; None without any."""
    filepath = attr.filepath
    if attr.lineno is None or isinstance(filepath, list):
        return None
    try:
        lines = attr.lines_collection[filepath]
    except KeyError:
        return None
    found: list[str] = []
    index = attr.lineno - 2  # the line above, 0-based
    while index >= 0:
        line = lines[index].strip()
        if line == "#:" or line.startswith("#: "):
            found.append(line[3:] if len(line) > 2 else "")
            index -= 1
            continue
        break
    if not found:
        return None
    return "\n".join(reversed(found)).strip() or None


class MicrogridDocstrings(griffe.Extension):
    """``#:`` comments as attributes' docstrings; Sphinx roles as inline code."""

    def on_attribute_instance(
        self,
        *,
        node: ast.AST | griffe.ObjectNode,
        attr: griffe.Attribute,
        agent: griffe.Visitor | griffe.Inspector,
        **kwargs: Any,
    ) -> None:
        if attr.docstring is None and (text := _comment_doc(attr)) is not None:
            attr.docstring = griffe.Docstring(text, parent=attr)

    def on_package(self, *, pkg: griffe.Module, **kwargs: Any) -> None:
        self._convert(pkg, set())

    def _convert(self, obj: griffe.Object, seen: set[str]) -> None:
        if obj.path in seen:
            return
        seen.add(obj.path)
        if obj.docstring is not None:
            obj.docstring.value = _markdown(obj.docstring.value)
        for member in obj.members.values():
            if not member.is_alias:
                assert isinstance(member, griffe.Object)
                self._convert(member, seen)
