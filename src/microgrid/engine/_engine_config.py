"""The ``engine/2`` mapping: how the price is computed, as plain data.

Every field is optional and defaults to today's value, so ``{"schema": "engine/2"}`` is
the engine as it runs. ``engine/1`` is read as it was: the same keys, but without
``price_max_age_minutes`` prices have no age limit (``engine/2`` defaults it: a file
read differently is a new version). What the plant is comes apart (``plant/2``); where
the data comes from is the client's. Durations are named by their unit
(``slot_minutes``, ``horizon_hours``, ``load_max_age_minutes``, ``smoothing_seconds``),
power in kW, the SOC as 0-1.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import fields
from datetime import timedelta
from typing import Final, TypedDict

from microgrid.engine._value import BATTERY_VALUE_RULES, OBSERVED, Noise, Pricing

ENGINE_SCHEMA: Final = "engine/2"
#: The schemas read: ``engine/1`` as it was (no default price age), and this one.
ENGINE_SCHEMAS: Final = ("engine/1", ENGINE_SCHEMA)

#: The fields of ``engine/2`` (and ``engine/1``) besides ``schema`` and ``observed``: each with its
#: ``EngineConfig`` field and the unit its number is in.
_DURATIONS: Final = {
    "slot_minutes": ("slot", timedelta(minutes=1)),
    "horizon_hours": ("horizon", timedelta(hours=1)),
    "pv_max_age_hours": ("pv_max_age", timedelta(hours=1)),
    "load_max_age_minutes": ("load_max_age", timedelta(minutes=1)),
    "price_max_age_minutes": ("price_max_age", timedelta(minutes=1)),
    "smoothing_seconds": ("smoothing", timedelta(seconds=1)),
}
_NUMBERS: Final = ("soc_tolerance", "load_jump_kw")
_KEYS: Final = ("schema", "scenarios", "battery_value", *_DURATIONS, *_NUMBERS, "observed")
_NOISE: Final = tuple(f.name for f in fields(Noise))
#: The SOC fractions (0-1): a tolerance or floor of a whole battery or more would let
#: a percent pass as a fraction, the mistake the tolerance catches.
_FRACTIONS: Final = frozenset({"soc_tolerance", "observed.soc"})


class EngineFields(TypedDict, total=False):
    """``EngineConfig``'s keyword arguments, but the plant."""

    pricing: Pricing
    slot: timedelta
    horizon: timedelta
    pv_max_age: timedelta
    load_max_age: timedelta
    price_max_age: timedelta | None
    smoothing: timedelta
    observed: Noise
    soc_tolerance: float
    load_jump_kw: float


#: The ``EngineConfig`` fields ``engine/1`` writes: a field added to ``EngineConfig``
#: and not here fails ``write``.
FIELDS: Final = frozenset(EngineFields.__annotations__)


class EngineConfigError(ValueError):
    """An ``engine/1`` mapping the engine cannot take; ``errors`` names each problem
    with its path."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__(
            "engine configuration is invalid:\n" + "\n".join(f"  {e}" for e in self.errors)
        )


def _number(value: object) -> float | None:
    """``value`` as a finite float; None if it is not one (a bool is not, nor an int
    too large for a float)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _check(path: str, value: object, errors: list[str], *, zero_ok: bool) -> float | None:
    """``value`` as a number of ``path``'s kind, or None with the problem named."""
    number = _number(value)
    if number is None:
        errors.append(f"{path}: must be a number")
    elif number < 0 or (not zero_ok and number == 0):
        errors.append(f"{path}: must be {'0 or more' if zero_ok else 'more than 0'}")
    elif path in _FRACTIONS and number >= 1:
        errors.append(f"{path}: must be under 1 (a fraction of the battery)")
    else:
        return number
    return None


def read(data: object) -> EngineFields:
    """``EngineConfig``'s keyword arguments, but the plant, from an ``engine/1``
    mapping; raises :class:`EngineConfigError` naming every problem."""
    errors: list[str] = []
    if not isinstance(data, Mapping):
        raise EngineConfigError(["(top level): must be a table"])
    for key in data:
        if key not in _KEYS:
            errors.append(f"{key}: is not a field")
    schema = data.get("schema")
    if schema not in ENGINE_SCHEMAS:
        errors.append(f"schema: must be {ENGINE_SCHEMA!r} (or the older 'engine/1')")
    out: EngineFields = {}
    if schema == "engine/1" and "price_max_age_minutes" not in data:
        out["price_max_age"] = None  # engine/1's meaning: no limit unless one is given

    scenarios: object = data.get("scenarios", Pricing().scenarios)
    rule: object = data.get("battery_value", Pricing().battery_value)
    if isinstance(scenarios, bool) or not isinstance(scenarios, int):
        errors.append("scenarios: must be a whole number")
    elif not 1 <= scenarios <= 50:
        errors.append("scenarios: must be in [1, 50]")
    if rule not in BATTERY_VALUE_RULES:
        errors.append(f"battery_value: must be one of {', '.join(BATTERY_VALUE_RULES)}")
    if ("scenarios" in data or "battery_value" in data) and not errors:
        out["pricing"] = Pricing(scenarios, rule)  # type: ignore[arg-type]

    durations: dict[str, timedelta | None] = {}
    for key, (name, unit) in _DURATIONS.items():
        if key not in data:
            continue
        value = data[key]
        if value is None and key == "price_max_age_minutes":
            durations[name] = None  # no limit
            continue
        zero_ok = key == "smoothing_seconds"
        number = _check(key, value, errors, zero_ok=zero_ok)
        if number is None:
            continue
        try:
            duration = number * unit
        except OverflowError:
            errors.append(f"{key}: is too large")
            continue
        if not zero_ok and duration <= timedelta(0):
            errors.append(f"{key}: must be at least a microsecond")
            continue
        if key == "slot_minutes" and timedelta(hours=1) % duration:
            errors.append(f"{key}: must divide an hour")
            continue
        durations[name] = duration
    if (slot := durations.get("slot")) is not None:
        out["slot"] = slot
    if (horizon := durations.get("horizon")) is not None:
        out["horizon"] = horizon
    if (pv_max_age := durations.get("pv_max_age")) is not None:
        out["pv_max_age"] = pv_max_age
    if (load_max_age := durations.get("load_max_age")) is not None:
        out["load_max_age"] = load_max_age
    if (smoothing := durations.get("smoothing")) is not None:
        out["smoothing"] = smoothing
    if "price_max_age" in durations:
        out["price_max_age"] = durations["price_max_age"]

    if "soc_tolerance" in data:
        number = _check("soc_tolerance", data["soc_tolerance"], errors, zero_ok=True)
        if number is not None:
            out["soc_tolerance"] = number
    if "load_jump_kw" in data:
        number = _check("load_jump_kw", data["load_jump_kw"], errors, zero_ok=True)
        if number is not None:
            out["load_jump_kw"] = number

    if "observed" in data:
        observed = data["observed"]
        if not isinstance(observed, Mapping):
            errors.append("observed: must be a table")
        else:
            floors: dict[str, float] = {}
            for key, value in observed.items():
                path = f"observed.{key}"
                if key not in _NOISE:
                    errors.append(f"{path}: is not a field")
                    continue
                number = _check(path, value, errors, zero_ok=True)
                if number is not None:
                    floors[str(key)] = number
            defaults = {name: getattr(OBSERVED, name) for name in _NOISE}
            out["observed"] = Noise(**(defaults | floors))

    if errors:
        raise EngineConfigError(errors)
    return out


def write(values: Mapping[str, object]) -> dict[str, object]:
    """The ``engine/1`` mapping of ``EngineConfig``'s fields (the plant left out)."""
    if set(values) != FIELDS:
        raise ValueError(f"engine/1 does not write {sorted(set(values) ^ FIELDS)}")
    pricing, observed = values["pricing"], values["observed"]
    assert isinstance(pricing, Pricing) and isinstance(observed, Noise)
    out: dict[str, object] = {
        "schema": ENGINE_SCHEMA,
        "scenarios": pricing.scenarios,
        "battery_value": pricing.battery_value,
    }
    for key, (name, unit) in _DURATIONS.items():
        value = values[name]
        assert value is None or isinstance(value, timedelta)
        out[key] = None if value is None else value / unit
    for key in _NUMBERS:
        out[key] = values[key]
    out["observed"] = {name: getattr(observed, name) for name in _NOISE}
    return out
