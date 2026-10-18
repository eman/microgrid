"""What was measured over a span, read from Home Assistant's history and written as a
dataset (``microgrid fetch``), for the commands that score against the meters
(``check-plant``, ``ledger``, ``fit-battery``).

From the ``[measured]`` section: the plant's meters by the plant's names, the SOC, the
house meter, the inverter's AC meter, the recorded import and export prices, and the
plant's mode. Each is read as Home Assistant's recorder kept it (its changes; ten days
by default) and in the unit its entity states now; recorded prices are written per
kWh. The heartbeat, the series whose samples prove the readings were flowing, is the
``heartbeat`` entity's, else the house meter's (one or the other is required): choose
one that reports every few seconds, as a silence longer than the telemetry limit marks
the meters unknown.

Forecasts are not fetched: Home Assistant does not keep what a forecaster published in a
form that says what was known when. A dataset for ``value``, ``live`` or ``replay``
records its forecasts as they are received (``microgrid now --save``).
"""

from __future__ import annotations

from datetime import datetime

from microgrid.cli.dataset import (
    BATTERY_FIRST,
    EXPORT_PRICE,
    HEARTBEAT,
    HOUSE,
    IMPORT_PRICE,
    INVERTER,
    SOC,
    Entry,
)
from microgrid.cli.sources import SourceError
from microgrid.cli.sources.config import Measured
from microgrid.cli.sources.home_assistant import HaClient
from microgrid.engine.series import Sample, Series
from microgrid.engine.units import UnitError, convert

__all__ = [
    "fetch_measured",
]


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None  # unavailable, unknown: no value from here


async def fetch_measured(
    client: HaClient, measured: Measured, start: datetime, end: datetime
) -> dict[str, Entry]:
    """The measured entities over ``[start, end)`` as a dataset's entries."""
    numbers: dict[str, str] = dict(measured.meters)
    for name, entity in (
        (HOUSE, measured.house),
        (SOC, measured.soc),
        (INVERTER, measured.inverter),
        (IMPORT_PRICE, measured.import_price),
        (EXPORT_PRICE, measured.export_price),
    ):
        if entity is not None:
            numbers[name] = entity
    if not numbers:
        raise SourceError("[measured] names nothing to fetch")
    # A meter that only writes changes (PV at 0 all night) would mark the record silent:
    # the heartbeat is the one named, else the house meter, never a guess among meters.
    beat = measured.heartbeat or measured.house
    if beat is None:
        raise SourceError(
            "[measured] needs a heartbeat (an entity that reports every few seconds) or a"
            " house meter to stand for one"
        )
    wanted = [*numbers.values(), *([measured.dispatch] if measured.dispatch else [])]
    if beat is not None:
        wanted.append(beat)
    history = await client.history(list(dict.fromkeys(wanted)), start, end)
    entries: dict[str, Entry] = {}
    for name, entity in numbers.items():
        rows = history.get(entity, [])
        state = await client.get_state(entity)
        unit = state.get("attributes", {}).get("unit_of_measurement")
        if name == SOC and not unit:
            unit = measured.soc_unit
        samples = tuple(Sample(max(at, start), _number(value)) for at, value in rows)
        if name in (IMPORT_PRICE, EXPORT_PRICE):
            # Recorded prices are read per kWh as written: convert them here, in the
            # entity's unit (a currency per kWh or per MWh; none: per kWh).
            samples, unit = _per_kwh(samples, unit, entity)
        entries[name] = Entry("number", Series(_ordered(samples), end, unit or None))
    if measured.dispatch is not None:
        rows = history.get(measured.dispatch, [])
        modes = set(measured.modelled_modes)
        samples_b = tuple(Sample(max(at, start), value in modes) for at, value in rows)
        entries[BATTERY_FIRST] = Entry("bool", Series(_ordered(samples_b), end))
    if beat is not None:
        beats = tuple(Sample(max(at, start), 1.0) for at, _ in history.get(beat, []))
        entries[HEARTBEAT] = Entry("number", Series(_ordered(beats), end))
    return entries


def _per_kwh(
    samples: tuple[Sample[float], ...], unit: str | None, entity: str
) -> tuple[tuple[Sample[float], ...], str | None]:
    if not unit:
        return samples, None
    try:
        converted = tuple(
            Sample(s.at, None if s.value is None else convert(s.value, unit, "price"))
            for s in samples
        )
    except UnitError as err:
        raise SourceError(f"{entity}: {err}") from err
    return converted, f"{unit.rpartition('/')[0]}/kWh"


def _ordered[T](samples: tuple[Sample[T], ...]) -> tuple[Sample[T], ...]:
    return tuple(sorted(samples, key=lambda sample: sample.at))
