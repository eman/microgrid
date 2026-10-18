"""The CLI end to end on the example dataset, offline: each command prints what it
printed when its expected output was made (``scripts/make_expected_outputs.py``). A
change that moves an output on purpose remakes them, and says why."""

import contextlib
import importlib.util
import io
from pathlib import Path

import pytest

from microgrid.cli.main import main

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_expected_outputs.py"
_spec = importlib.util.spec_from_file_location("make_expected_outputs", SCRIPT)
assert _spec is not None and _spec.loader is not None
expected = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(expected)


@pytest.mark.parametrize("name", sorted(expected.COMMANDS))
def test_the_command_prints_its_expected_output(name: str) -> None:
    assert expected.run(name) == (expected.EXPECTED / name).read_text(encoding="utf-8")


def test_every_expected_output_has_its_command() -> None:
    assert {p.name for p in expected.EXPECTED.iterdir()} == set(expected.COMMANDS)


MAKE_UP = Path(__file__).resolve().parent / "makeup"


@pytest.mark.parametrize("name", sorted(p.name for p in MAKE_UP.glob("*.txt")))
def test_the_make_up_rule_prints_what_it_did_before_the_stored_cost(name: str) -> None:
    """``battery_value = "replacement"`` is the rule before 2026-10-17, exactly: each
    command prints what ``main`` printed then (its expected outputs at de7e4d8)."""
    out = io.StringIO()
    command = expected.COMMANDS[name]
    with contextlib.redirect_stdout(out):
        status = main(
            [
                command[0],
                "--data",
                str(expected.DATASET),
                "--engine",
                str(MAKE_UP / "engine.toml"),
                *command[1:],
            ]
        )
    assert status == 0
    assert out.getvalue() == (MAKE_UP / name).read_text(encoding="utf-8")
