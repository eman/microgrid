"""The outputs as plain data: each output's ``to_mapping``, checked against the schema
the library ships (``valuation/2``, a data file in the package)."""

import json
from dataclasses import replace
from datetime import timedelta
from importlib.resources import files
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from engine_fixtures import CONFIG, NOW, PACIFIC, inputs, live_reading, reading
from microgrid.engine import (
    VALUATION_SCHEMA,
    Cause,
    Engine,
    EngineConfig,
    LiveValue,
    Pricing,
    Valuation,
    ValuationKind,
)
from microgrid.plant import Plant
from microgrid.plant.examples import EXAMPLE_NAMES, example_plant

SCHEMA: dict[str, Any] = json.loads(
    (files("microgrid.engine") / "schemas" / "valuation-2.schema.json").read_text("utf-8")
)
UNTIL = NOW + timedelta(minutes=30)


def _validator(definition: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def _valid(output: Valuation | LiveValue) -> dict[str, object]:
    mapping = output.to_mapping()
    # Plain data: JSON and back gives it again.
    assert json.loads(json.dumps(mapping)) == mapping
    definition = "Valuation" if isinstance(output, Valuation) else "LiveValue"
    errors = [error.message for error in _validator(definition).iter_errors(mapping)]
    assert errors == []
    return mapping


def test_the_schema_is_valuation_1_and_a_valid_schema() -> None:
    assert SCHEMA["title"] == VALUATION_SCHEMA == "valuation/2"
    Draft202012Validator.check_schema(SCHEMA)


def test_a_valuation_is_its_schemas() -> None:
    valuation = Engine(CONFIG).value(inputs(), at=NOW)
    mapping = _valid(valuation)
    assert mapping["kind"] == "stored_value_v1" and mapping["now"] == NOW.isoformat()
    slots = mapping["slots"]
    assert isinstance(slots, list) and len(slots) == len(valuation.slots)
    first = valuation.slots[0]
    assert slots[0]["start"] == first.start.isoformat()
    assert slots[0]["regime"] == first.regime.value
    assert slots[0]["marginal_cost"] == first.marginal_cost  # unrounded
    assert "model" not in mapping  # opaque, not part of the contract


def test_a_fallback_and_an_unpriced_valuation_are_their_schemas() -> None:
    engine = Engine(CONFIG)
    fallback = engine.value(inputs(soc=None), at=NOW)
    assert fallback.kind is ValuationKind.FALLBACK_IMPORT
    assert _valid(fallback)["degraded"] == ["battery_soc"]
    unpriced = engine.value(inputs(prices=None), at=NOW)
    assert unpriced.kind is ValuationKind.UNPRICED
    mapping = _valid(unpriced)
    assert mapping["slots"] == [] and "prices" in mapping["degraded"]  # type: ignore[operator]


@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_each_example_plants_valuation_is_its_schemas(name: str) -> None:
    plant = Plant(example_plant(name, PACIFIC))
    engine = Engine(EngineConfig(plant=plant, pricing=Pricing(scenarios=3)))
    overrides: dict[str, object] = {}
    if not plant.has_pv:
        overrides["pv"] = None
    if plant.storage is None:
        overrides["soc"] = reading(None)
        overrides["dispatch_as_modelled"] = reading(None)
    _valid(engine.value(inputs(**overrides), at=NOW))


def test_a_live_value_is_its_schemas() -> None:
    engine = Engine(replace(CONFIG, smoothing=timedelta(0)))
    valuation = engine.value(inputs(), at=NOW)
    live = engine.live_session().update(live_reading(), valuation, UNTIL)
    mapping = _valid(live)
    assert mapping["at"] == live.at.isoformat() and mapping["external"] == []
    warming = Engine(CONFIG).live_session().update(live_reading(), valuation, UNTIL)
    assert _valid(warming)["degraded"] == [Cause.SMOOTHING.value]
    theirs = engine.live_session().update(live_reading(external=("meter",)), valuation, UNTIL)
    assert _valid(theirs)["external"] == ["meter"]
    unpriced = engine.live_session().update(
        live_reading(), engine.value(inputs(prices=None), at=NOW), UNTIL
    )
    assert _valid(unpriced)["p_import"] is None
