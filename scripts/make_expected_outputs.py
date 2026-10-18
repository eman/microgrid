"""The example dataset's expected outputs: what each
``microgrid`` command prints on it, for the CLI's offline end-to-end test
(``tests/unit/test_end_to_end.py``). Run after a change that moves them on purpose,
and say why in the change.

    uv run python scripts/make_expected_outputs.py
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path

from microgrid.cli.main import main

DATASET = Path(__file__).resolve().parents[1] / "examples" / "dataset"
EXPECTED = DATASET / "expected"
#: Each expected output: its file, and the command (the dataset's path added).
COMMANDS: dict[str, list[str]] = {
    "value-2026-10-22T13-03.txt": ["value", "--at", "2026-10-22T13:03", "--slots", "288"],
    "value-2026-10-25T02-30.txt": ["value", "--at", "2026-10-25T02:30", "--slots", "288"],
    "live-2026-10-25.txt": [
        "live",
        "--from",
        "2026-10-25T01:00",
        "--to",
        "2026-10-25T04:00",
        "--every",
        "5m",
    ],
    "replay-2026-10-22.txt": ["replay", "--from", "2026-10-22", "--to", "2026-10-24"],
    "ledger-2026-10-21.txt": ["ledger", "--from", "2026-10-21", "--to", "2026-10-28"],
}


def run(name: str) -> str:
    """What the command for ``name`` prints on the example dataset."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = main([COMMANDS[name][0], "--data", str(DATASET), *COMMANDS[name][1:]])
    if status != 0:
        raise SystemExit(f"{name}: exit {status}")
    return out.getvalue()


def main_() -> None:
    EXPECTED.mkdir(exist_ok=True)
    for name in COMMANDS:
        (EXPECTED / name).write_text(run(name), encoding="utf-8")
        print(f"wrote {EXPECTED / name}")


if __name__ == "__main__":
    main_()
