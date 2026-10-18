"""A load forecast Predbat publishes (``from = "predbat"``).

Predbat publishes today's predicted load as an entity (``predbat.load_energy_predicted``
by default) whose ``results`` attribute maps times to the energy predicted from local
midnight, cumulative, in kWh: ``{"2026-10-17T00:00:00+0000": 0.12, ...}``. Each value
includes Predbat's step starting at its time (5 minutes), and a value equal to its
neighbours is dropped. So the cumulative energy is a step function of time, and the load
over each step is the energy it adds: the value at the step's start less the value one
step before, as a mean power. The forecast is on Predbat's own 5-minute steps from its
first time, so it tiles the engine's slots; the first time's own step is not read (the
energy before it is unknown).

It reaches local midnight: a valuation's horizon ends there, as it does at the end of
any load forecast (a forecast that ends is not ``horizon_cut``: that is a gap with
forecast after it). The forecast is issued at the state's
``last_updated``.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime, timedelta

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.config import Predbat
from microgrid.cli.sources.home_assistant import HaClient
from microgrid.engine.series import Forecast, Interval

__all__ = [
    "SOURCE",
    "PredbatError",
    "fetch_load_forecast",
    "parse_load_forecast",
]

SOURCE = "predbat"


class PredbatError(SourceError):
    """No usable load forecast in Predbat's entity."""


def _time(value: object) -> datetime:
    text = str(value)
    if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":  # +0000: Python wants +00:00
        text = f"{text[:-2]}:{text[-2:]}"
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError(f"no offset: {value!r}")
    return moment.astimezone(UTC)


def parse_load_forecast(state: object, config: Predbat, *, now: datetime) -> Forecast[float]:
    """The load from ``now`` to the end of Predbat's day, kW, from the state's cumulative
    energy (module docstring)."""
    entity = config.entity
    if not isinstance(state, dict) or not isinstance(state.get("attributes"), dict):
        raise PredbatError(f"{entity}: no attributes")
    results = state["attributes"].get(config.attribute)
    if not isinstance(results, dict) or len(results) < 2:
        raise PredbatError(f"{entity}: no {config.attribute} to read a forecast from")
    try:
        issued = _time(state.get("last_updated"))
        points = sorted((_time(at), float(kwh)) for at, kwh in results.items())
    except (TypeError, ValueError) as err:
        raise PredbatError(f"{entity}: {config.attribute} cannot be read: {err}") from err
    step = config.step
    times = [at for at, _ in points]
    values = [kwh for _, kwh in points]

    def cumulative(moment: datetime) -> float:
        # The value in force: the last time at or before ``moment``.
        index = bisect_right(times, moment) - 1
        return values[index] if index >= 0 else 0.0

    hours = step / timedelta(hours=1)
    intervals = []
    # The first time's own step is not read: the energy before it is unknown (Predbat
    # writes from local midnight, where it is 0, but a reader cannot tell midnight from
    # a mid-day start, and taking the day so far as one step would be badly wrong).
    start = times[0] + step
    while start <= times[-1]:
        if start + step > now:
            added = cumulative(start) - cumulative(start - step)
            intervals.append(Interval(start, start + step, max(added, 0.0) / hours))
        start += step
    if not intervals:
        raise PredbatError(f"{entity}: nothing forecast after {now.isoformat()}")
    try:
        return Forecast(tuple(intervals), issued, SOURCE)
    except ValueError as err:
        raise PredbatError(f"{entity}: {err}") from err


async def fetch_load_forecast(
    client: HaClient, config: Predbat, *, now: datetime
) -> Forecast[float]:
    """Read Predbat's entity and parse it (:func:`parse_load_forecast`)."""
    return parse_load_forecast(await client.get_state(config.entity), config, now=now)
