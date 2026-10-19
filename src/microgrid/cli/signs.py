"""Each meter's sign, from what it recorded: whether the plant's convention fits a meter
as it reads, or its opposite does.

A meter's sign (``plant/2``'s ``[[meters]]``) is its convention: 1 where positive is PV
made, importing, charging or drawing. Set wrong, every reading is taken backwards and
nothing says so. What the meters recorded says which way each reads:

- **PV**, a **DC-bus load** and **consumption** are positive over any span: their sum
  says.
- **The battery** charging raises its SOC: its readings against the SOC's change say.
- **The grid** closes the balance. What the house drew is PV plus the grid less the
  battery's charge and the DC-bus loads (losses aside): with a house meter, the sign
  that brings the two closer; without one, the sign under which the house rarely
  gives energy back.

A sign is found only where the two choices are told apart clearly; elsewhere the meter
is named as undecided, and its configured sign stands. Pure: the caller reads the
meters (the means of each slot, :func:`microgrid.engine.series.means`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from microgrid.plant import Meter

__all__ = ["Signs", "meter_signs"]

Slots = Sequence[float | None]

#: The SOC must move this much (a fraction, summed over the span) to say which way the
#: battery reads: a battery that held still says nothing.
_SOC_MOVED: Final = 0.05
#: A meter whose readings sum to less than this (kWh over the span, at kW means of
#: ``hours`` each) says nothing of its sign.
_ENERGY_MIN: Final = 0.5
#: The better sign must fit at least this much better than the other (its residual
#: this fraction of the other's, or less) to be taken.
_CLEARLY: Final = 0.5
#: A derived house load below this (kW) is energy given back: a sign of the grid's
#: being backwards, where no house meter says.
_GIVES_BACK: Final = -0.2
#: What a meter does when it reads positive, for the notes.
_POSITIVE: Final = {"pv": "makes power", "dc_load": "draws", "consumption": "draws"}


@dataclass(frozen=True, slots=True)
class Signs:
    """What the meters' records say of their signs."""

    #: Each meter's sign where it was told apart (1 or -1), by the meter's name.
    found: Mapping[str, float]
    #: For a person, by the meter's name: what its records show, for each sign found.
    why: Mapping[str, str]
    #: For a person: the meters whose records told nothing, and why.
    undecided: tuple[str, ...]


def _energy(values: Slots, hours: float) -> float:
    return math.fsum(v for v in values if v is not None) * hours


def _mean_abs(values: Sequence[float]) -> float:
    return math.fsum(abs(v) for v in values) / len(values) if values else math.inf


def meter_signs(
    meters: Sequence[Meter],
    readings: Mapping[str, Slots],
    *,
    hours: float,
    soc: Slots | None = None,
    house: Slots | None = None,
) -> Signs:
    """Each meter's sign from its slot means (``readings``, by meter name, each slot
    ``hours`` long), the SOC's slot means (a fraction) and the house load's where
    metered. A meter without readings is left out."""
    found: dict[str, float] = {}
    why: dict[str, str] = {}
    notes: list[str] = []
    by_kind: dict[str, list[Meter]] = {}
    for meter in meters:
        if meter.name in readings:
            by_kind.setdefault(meter.measures, []).append(meter)

    for kind in ("pv", "dc_load", "consumption"):
        for meter in by_kind.get(kind, []):
            energy = _energy(readings[meter.name], hours)
            if abs(energy) < _ENERGY_MIN:
                notes.append(f"{meter.name}: too little recorded to tell its sign")
                continue
            found[meter.name] = 1.0 if energy > 0 else -1.0
            reads = "positive" if energy > 0 else "negative"
            why[meter.name] = f"{meter.name} reads {reads} when it {_POSITIVE[kind]}"

    batteries = by_kind.get("battery", [])
    battery = batteries[0] if batteries else None
    if battery is not None:
        sign = _battery_sign(readings[battery.name], soc)
        if sign is None:
            notes.append(f"{battery.name}: the SOC moved too little to tell its sign")
        else:
            found[battery.name] = sign
            reads = "positive" if sign > 0 else "negative"
            why[battery.name] = f"{battery.name} reads {reads} while the SOC rises"

    grids = by_kind.get("grid", [])
    if grids:
        grid = grids[0]
        sign = _grid_sign(by_kind, readings, found, battery, house)
        if sign is None:
            notes.append(f"{grid.name}: the balance does not tell its sign")
        else:
            found[grid.name] = sign
            reads = "positive" if sign > 0 else "negative"
            why[grid.name] = f"{grid.name} reads {reads} while importing (the balance)"
    return Signs(found, why, tuple(notes))


def _battery_sign(values: Slots, soc: Slots | None) -> float | None:
    """1 where the readings rise with the SOC (positive charging), -1 where they fall."""
    if soc is None:
        return None
    agree = 0.0
    moved = 0.0
    for i in range(1, len(values) - 1):
        kw, before, after = values[i], soc[i - 1], soc[i + 1]
        if kw is None or before is None or after is None:
            continue
        change = after - before
        moved += abs(change)
        agree += kw * change
    if moved < _SOC_MOVED or agree == 0:
        return None
    return 1.0 if agree > 0 else -1.0


def _grid_sign(
    by_kind: Mapping[str, list[Meter]],
    readings: Mapping[str, Slots],
    found: Mapping[str, float],
    battery: Meter | None,
    house: Slots | None,
) -> float | None:
    """The grid's sign that closes the balance, the other meters' signs as found (as
    configured where undecided)."""
    grid = by_kind["grid"][0]

    def signed(meter: Meter) -> Slots:
        sign = found.get(meter.name, meter.sign)
        return [None if v is None else v * sign for v in readings[meter.name]]

    parts: list[tuple[float, Slots]] = [(1.0, signed(m)) for m in by_kind.get("pv", [])]
    if battery is not None:
        parts.append((-1.0, signed(battery)))
    parts += [(-1.0, signed(m)) for m in by_kind.get("dc_load", [])]
    grid_values = readings[grid.name]
    scores: dict[float, float] = {}
    for sign in (1.0, -1.0):
        derived: list[float] = []
        measured: list[float] = []
        for i, kw in enumerate(grid_values):
            values = [(weight, part[i]) for weight, part in parts]
            drew = None if house is None else house[i]
            if kw is None or any(v is None for _, v in values):
                continue
            if house is not None and drew is None:
                continue
            total = sign * kw + math.fsum(weight * v for weight, v in values if v is not None)
            if drew is not None:
                measured.append(total - drew)
            else:
                derived.append(total)
        if house is not None:
            scores[sign] = _mean_abs(measured)
        elif derived:
            scores[sign] = sum(1 for v in derived if v < _GIVES_BACK) / len(derived)
    if len(scores) < 2:
        return None
    plus, minus = scores[1.0], scores[-1.0]
    if not math.isfinite(plus) or not math.isfinite(minus) or plus == minus:
        return None
    if plus <= _CLEARLY * minus:
        return 1.0
    if minus <= _CLEARLY * plus:
        return -1.0
    return None
