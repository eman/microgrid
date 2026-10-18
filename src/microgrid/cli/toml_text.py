"""Plain data as TOML text: the configurations the CLI writes beside a dataset
(``plant/2``, ``engine/2``), as their ``to_mapping`` gives them. Python reads TOML
(``tomllib``) but does not write it; this writes the subset those mappings use, and
:func:`dumps` reads its text back to check it is the same data.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence

__all__ = [
    "dumps",
]


def _scalar(value: object, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        # A JSON string is a TOML basic string; unescaped beyond ASCII, as TOML takes no
        # surrogate pairs, but DEL escaped, as TOML takes it raw nowhere.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, Sequence) and not any(isinstance(v, Mapping) for v in value):
        return "[" + ", ".join(_scalar(v, where) for v in value) + "]"
    raise ValueError(f"{where}: no TOML for {value!r}")


def _tables(value: object) -> bool:
    """A non-empty list of tables: an array of tables."""
    return (
        isinstance(value, Sequence)
        and not isinstance(value, str)
        and bool(value)
        and all(isinstance(v, Mapping) for v in value)
    )


def _key(name: str) -> str:
    """A bare key where TOML allows one (ASCII letters, digits, ``_``, ``-``)."""
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _scalar(name, name)


def _table(lines: list[str], path: list[str], table: Mapping[str, object], array: bool) -> None:
    if path:
        name = ".".join(_key(p) for p in path)
        lines.extend(["", f"[[{name}]]" if array else f"[{name}]"])
    nested: list[tuple[str, object]] = []
    for key, value in table.items():
        if isinstance(value, Mapping) or _tables(value):
            nested.append((key, value))
        else:
            lines.append(f"{_key(key)} = {_scalar(value, '.'.join([*path, key]))}")
    for key, value in nested:
        if isinstance(value, Mapping):
            _table(lines, [*path, key], value, array=False)
        else:
            assert isinstance(value, Sequence)
            for item in value:
                _table(lines, [*path, key], item, array=True)


def dumps(data: Mapping[str, object], *, comment: str | None = None) -> str:
    """``data`` as TOML: tables, arrays of tables, and scalars or lists of them. Raises
    ``ValueError`` for anything else, or if the text would not read back as ``data``."""
    lines = [f"# {comment}"] if comment else []
    _table(lines, [], data, array=False)
    text = "\n".join(lines).lstrip("\n") + "\n"
    if tomllib.loads(text) != data:
        raise ValueError("the TOML written does not read back as the data")
    return text
