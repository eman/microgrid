"""Rows of a published forecast as the library's intervals: what every reader of a list of
``{start, end, value}`` rows shares.

A row's times come as Home Assistant gives them: an ISO 8601 string (``T`` or a space
between date and time) over its REST API, a ``datetime`` inside Home Assistant. A time
needs an offset, unless the reader names the zone its source publishes in (``tz``). A
row's end is its own end key, else a fixed period from its start, else the next row's
start (the last row then lasts as long as the one before it). A row that cannot be read
is an error naming it, not skipped.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from microgrid.cli.sources import SourceError
from microgrid.engine.series import Forecast, Interval

__all__ = [
    "RowError",
    "number",
    "on_slots",
    "rows_forecast",
    "time",
]

#: Where the engine's slots are counted from (every slot length divides an hour).
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class RowError(SourceError):
    """A row, or a time or value in it, cannot be read."""


def time(value: object, where: str, *, tz: ZoneInfo | None = None) -> datetime:
    """``value`` as a UTC time: a ``datetime`` or an ISO 8601 string, with an offset
    (else in ``tz``, where the source publishes local times without one)."""
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError as err:
            raise RowError(f"{where}: not a time: {value!r}") from err
    if moment.tzinfo is None:
        if tz is None:
            raise RowError(f"{where}: a time without an offset: {value!r}")
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(UTC)


def number(value: object, where: str) -> float:
    """``value`` as a finite number."""
    if isinstance(value, bool):
        raise RowError(f"{where}: not a number: {value!r}")
    try:
        found = float(str(value))
    except ValueError as err:
        raise RowError(f"{where}: not a number: {value!r}") from err
    if not math.isfinite(found):
        raise RowError(f"{where}: not finite: {value!r}")
    return found


def on_slots[V](forecast: Forecast[V], slot: timedelta) -> Forecast[V]:
    """``forecast`` with each interval that starts or ends inside an engine slot of
    ``slot`` split where the slots begin, so the engine averages its pieces within each
    slot (a quarter hour, at 10-minute slots). A value is a mean over its interval, so
    each piece keeps it. An interval on the slots' boundaries is kept whole."""

    def on_grid(moment: datetime) -> bool:
        return (moment - _EPOCH) % slot == timedelta(0)

    pieces: list[Interval[V]] = []
    for interval in forecast.intervals:
        if on_grid(interval.start) and on_grid(interval.end):
            pieces.append(interval)
            continue
        start = interval.start
        boundary = _EPOCH + ((start - _EPOCH) // slot + 1) * slot
        while boundary < interval.end:
            pieces.append(Interval(start, boundary, interval.value))
            start, boundary = boundary, boundary + slot
        pieces.append(Interval(start, interval.end, interval.value))
    if len(pieces) == len(forecast.intervals):
        return forecast
    return Forecast(tuple(pieces), forecast.issued_at, forecast.source)


def rows_forecast(
    rows: Sequence[object],
    *,
    start_key: str,
    value_key: str,
    issued_at: datetime,
    source: str,
    where: str,
    end_key: str | None = None,
    period: timedelta | None = None,
    scale: Callable[[float], float] = lambda value: value,
    tz: ZoneInfo | None = None,
) -> Forecast[float]:
    """``rows`` as a forecast of one value each (``value_key``, through ``scale``), in
    time order. Rows with the same start and value are one (a source's days may share
    an edge row); with the same start and another value, an error."""
    found: dict[datetime, tuple[datetime | None, float]] = {}
    for index, row in enumerate(rows):
        at = f"{where}[{index}]"
        if not isinstance(row, dict):
            raise RowError(f"{at}: not an object")
        if start_key not in row:
            raise RowError(f"{at}: no {start_key!r}")
        if value_key not in row:
            raise RowError(f"{at}: no {value_key!r}")
        start = time(row[start_key], at, tz=tz)
        end: datetime | None = None
        if end_key is not None:
            if end_key not in row:
                raise RowError(f"{at}: no {end_key!r}")
            end = time(row[end_key], at, tz=tz)
        elif period is not None:
            end = start + period
        value = scale(number(row[value_key], f"{at} {value_key}"))
        if start in found and found[start] != (end, value):
            raise RowError(f"{at}: a second, different row from {start.isoformat()}")
        found[start] = (end, value)
    if not found:
        raise RowError(f"{where}: no rows")
    starts = sorted(found)
    intervals: list[Interval[float]] = []
    for index, start in enumerate(starts):
        end, value = found[start]
        if end is None:  # the next row's start, else as long as the row before
            if index + 1 < len(starts):
                end = starts[index + 1]
            elif index > 0:
                end = start + (start - starts[index - 1])
            else:
                raise RowError(f"{where}: one row without an end cannot say how long it lasts")
        try:
            intervals.append(Interval(start, end, value))
        except ValueError as err:
            raise RowError(f"{where} from {start.isoformat()}: {err}") from err
    try:
        return Forecast(tuple(intervals), issued_at, source)
    except ValueError as err:
        raise RowError(f"{where}: {err}") from err
