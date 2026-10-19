"""Units: a series in the unit its source gave, converted to the engine's.

The engine works in kW, kWh, fractions (an SOC of 0.62) and prices per kWh. A source
gives what it gives: Home Assistant's ``unit_of_measurement`` may be ``W`` or
``kW``, an SOC is usually ``%``, a wholesale price may be per MWh. A client passes the
unit with its series (:attr:`microgrid.engine.series.Series.unit`), and
:func:`normalise` converts it once, at the edge, so no client assumes a unit.

A unit this module does not know raises :class:`UnitError`: a reading in an unknown
unit is not used. A currency is never converted: a price passes through in the
currency it was written in, so a client must give prices in the currency it means
(``¢/kWh`` passes as a currency named ``¢``: the numbers stay in cents).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from microgrid.engine.series import Sample, Series

__all__ = [
    "ENERGY_KWH",
    "FRACTION",
    "POWER_KW",
    "Quantity",
    "UnitError",
    "convert",
    "normalise",
]

#: Power units, as kW per unit.
POWER_KW: Final[Mapping[str, float]] = MappingProxyType({"W": 0.001, "kW": 1.0, "MW": 1000.0})
#: Energy units, as kWh per unit.
ENERGY_KWH: Final[Mapping[str, float]] = MappingProxyType({"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0})
#: Fraction units, as the fraction per unit. No unit (None or ``""``) is a fraction.
FRACTION: Final[Mapping[str, float]] = MappingProxyType({"%": 0.01, "": 1.0})


def _whole_reciprocals(*factors: float) -> Mapping[float, int]:
    """Each factor below 1 by its reciprocal, which must be whole: a factor without one
    (0.4: 2.5) would be divided by a rounded number, so it is refused here, at import."""
    divisors: dict[float, int] = {}
    for factor in factors:
        if 0 < factor < 1:
            divisor = round(1 / factor)
            if divisor * factor != 1:
                raise AssertionError(f"no whole reciprocal: {factor}")
            divisors[factor] = divisor
    return MappingProxyType(divisors)


#: The divisor of each fraction's or price's factor below 1 (:func:`_apply`).
_DIVISORS: Final = _whole_reciprocals(*FRACTION.values(), *(1 / kwh for kwh in ENERGY_KWH.values()))

#: What a series measures.
type Quantity = Literal["power", "energy", "fraction", "price"]


class UnitError(ValueError):
    """A unit that is not one of the quantity's."""


def _scale(quantity: Quantity, unit: str | None) -> tuple[float, bool, str | None]:
    """The factor to the engine's unit, whether a factor below 1 is applied as a
    division (:func:`_apply`), and the engine's unit's name."""
    match quantity:
        case "power":
            if unit is not None and unit in POWER_KW:
                return POWER_KW[unit], False, "kW"
        case "energy":
            if unit is not None and unit in ENERGY_KWH:
                return ENERGY_KWH[unit], False, "kWh"
        case "fraction":
            if (unit or "") in FRACTION:
                return FRACTION[unit or ""], True, None
        case "price":
            # <currency>/<energy unit>: the currency passes through.
            currency, slash, per = (unit or "").rpartition("/")
            if slash and currency and "/" not in currency and per in ENERGY_KWH:
                return 1 / ENERGY_KWH[per], True, f"{currency}/kWh"
    raise UnitError(f"not a {quantity} unit: {unit!r}")


def _apply(value: float, factor: float, divide: bool) -> float:
    """``value`` times ``factor``. A fraction's or a price's factor below 1 is applied
    as a division by its whole reciprocal, exactly as a client dividing would: 42 % is
    ``42 / 100``, 0.42, where ``42 * 0.01`` is 0.42000000000000004; 85 per MWh is
    ``85 / 1000``. A factor of 1 or more multiplies, as it is exact (per Wh: 1000.0).
    Power and energy multiply (W: ``* 0.001``)."""
    if divide and factor < 1:
        return value / _DIVISORS[factor]
    return value * factor


def convert(value: float, unit: str | None, quantity: Quantity) -> float:
    """``value`` in ``unit`` as the engine's unit for ``quantity``. One finite value:
    raises ``ValueError`` for one that is not (:func:`normalise` makes it None)."""
    if not math.isfinite(value):
        raise ValueError(f"a value that is not finite: {value}")
    scale, divide, _ = _scale(quantity, unit)
    return _apply(value, scale, divide)


def normalise(series: Series[float], quantity: Quantity) -> Series[float]:
    """``series`` in the engine's unit for ``quantity``: kW, kWh, a fraction (no unit),
    or the same currency per kWh.

    A sample that is not finite is already None, no value (:class:`Sample`). Raises
    :class:`UnitError` for a unit that is not ``quantity``'s.
    """
    scale, divide, unit = _scale(quantity, series.unit)
    return Series(
        tuple(
            Sample(sample.at, None if sample.value is None else _apply(sample.value, scale, divide))
            for sample in series.samples
        ),
        series.known_until,
        unit,
    )
