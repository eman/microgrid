"""Time series: the form a client hands its data in, and the operations every client needs.

Apart from the model's setup, everything a client knows arrives as a time series: a
meter's power, the battery's SOC, a price sensor, each forecast as it was issued. This
module holds the types and the operations on them: holding, staleness and alignment.

**A value holds until the next sample.** That is how Home Assistant records state,
and how most recorders store it: a value that does not change sends nothing. So the
age of a value says nothing about whether it is current. What can go stale is the
path the samples come by, and :attr:`Series.known_until` says how far the client
vouches for it: up to then, no change went unreported. A stretch the client cannot
vouch for in the middle is a sample whose value is None, from its start (Home
Assistant's ``unavailable`` and ``unknown`` read the same way).

**A forecast** is a set of intervals with values, and when it was issued
(:class:`Forecast`). A forecast that changes over time, as one held in a Home
Assistant entity's attributes does, is a sequence of forecasts; :func:`compose`
reads them as they stood at a moment.

Every time is UTC, timezone-aware: Python compares and subtracts two aware datetimes
that share a ``tzinfo`` by wall clock, which is wrong across a DST change, so a
client converts at its edge. Intervals are half-open, ``[start, end)``.

The operations:

- :func:`value_at`: the value in force at a moment.
- :func:`means`, :func:`signed_means`: time-weighted means over a grid of slots.
- :func:`segments`: several series swept together, cut wherever any of them changes.
- :func:`alive`: where a heartbeat shows the path was up.
- :func:`counter_delta`: what a cumulative counter counted, across its resets.
- :func:`align`: a forecast's values on a grid of slots.
- :func:`compose`: the newest fresh forecast for each slot, of several issued.
- :func:`is_stale`, :func:`prune`: a forecast's freshness, and which to keep.
- :func:`to_forecast`: a recorded sensor, such as a price, as a forecast's intervals.

The engine's slot grid: the default slot (:data:`SLOT`, 10 minutes;
``EngineConfig.slot``, ``engine/2``'s ``slot_minutes``), :func:`slot_floor`,
:func:`slot_starts`, a horizon's slots (:func:`horizon_slots`) and the UTC check every
time passes (:func:`require_utc`). Every operation takes the slot as an argument.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from numbers import Real
from types import MappingProxyType
from typing import cast, overload

from microgrid._checks import require_utc
from microgrid.engine._timeline import SLOT, horizon_slots, slot_floor, slot_starts

__all__ = [
    "EPOCH",
    "SLOT",
    "Composed",
    "CounterDelta",
    "Forecast",
    "Interval",
    "MisalignedError",
    "Price",
    "Quantiles",
    "Sample",
    "Segment",
    "Series",
    "align",
    "alive",
    "compose",
    "counter_delta",
    "horizon_slots",
    "is_stale",
    "means",
    "price_mean",
    "prune",
    "quantiles_mean",
    "require_utc",
    "segments",
    "signed_means",
    "slot_floor",
    "slot_starts",
    "to_forecast",
    "value_at",
]

#: Where grids are counted from when a client gives no other origin.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _require_finite(value: object, at: datetime) -> None:
    """A forecast's number must be finite: unlike a sample's, it has no "no value"."""
    if isinstance(value, Real) and not math.isfinite(value):
        raise ValueError(f"a value that is not finite at {at.isoformat()}: {value}")


@dataclass(frozen=True, slots=True)
class Sample[T]:
    """One value and when it took effect (Home Assistant's ``last_changed``).

    None: no value from ``at`` until the next sample (``unavailable``, ``unknown``, or
    a stretch the client cannot vouch for). A number that is not finite (a sensor's
    ``nan``) is read as no value, too: held, it would spread through every mean.
    """

    at: datetime
    value: T | None

    def __post_init__(self) -> None:
        require_utc(self.at)
        if isinstance(self.value, Real) and not math.isfinite(self.value):
            object.__setattr__(self, "value", None)  # frozen: set once, here


@dataclass(frozen=True, slots=True)
class Series[T]:
    """Samples in time order, each value held until the next, known up to ``known_until``.

    Equal times are allowed (a recorder can hold two rows at one instant); the last of
    them is the one in force. A sample after ``known_until`` is refused: the client
    cannot have seen it and not vouch for the time before it.
    """

    samples: tuple[Sample[T], ...]
    #: Up to this moment the client vouches that no change went unreported.
    known_until: datetime
    #: The values' unit as the source gave it (``W``, ``kW``, ``%``, ``¢/kWh``);
    #: :mod:`microgrid.engine.units` converts it.
    unit: str | None = None

    def __post_init__(self) -> None:
        require_utc(self.known_until)
        for earlier, later in pairwise(self.samples):
            if later.at < earlier.at:
                raise ValueError(f"samples are out of time order at {later.at.isoformat()}")
        if self.samples and self.samples[-1].at > self.known_until:
            raise ValueError(
                f"a sample at {self.samples[-1].at.isoformat()} is after known_until "
                f"{self.known_until.isoformat()}"
            )

    @classmethod
    def of(
        cls,
        points: Iterable[tuple[datetime, T | None]],
        *,
        known_until: datetime,
        unit: str | None = None,
    ) -> Series[T]:
        """A series from ``(time, value)`` pairs, in time order."""
        return cls(tuple(Sample(at, value) for at, value in points), known_until, unit)

    @property
    def times(self) -> tuple[datetime, ...]:
        """Each sample's time."""
        return tuple(sample.at for sample in self.samples)


@dataclass(frozen=True, slots=True)
class Interval[V]:
    """A value over ``[start, end)``: a forecast's mean power, a price."""

    start: datetime
    end: datetime
    value: V

    def __post_init__(self) -> None:
        require_utc(self.start, self.end)
        if self.end <= self.start:
            raise ValueError(f"interval ends before it starts: {self.start} .. {self.end}")
        _require_finite(self.value, self.start)


@dataclass(frozen=True, slots=True)
class Forecast[V]:
    """Intervals in time order, not overlapping, and when the forecast was issued.

    A gap between intervals is unknown, never an error, as is time past the last.
    """

    intervals: tuple[Interval[V], ...]
    issued_at: datetime
    #: Where it came from, for the record (``solcast``, ``emhass:sensor.x``).
    source: str = ""

    def __post_init__(self) -> None:
        require_utc(self.issued_at)
        for earlier, later in pairwise(self.intervals):
            if later.start < earlier.end:
                raise ValueError(f"intervals overlap or are unsorted at {later.start}")

    @property
    def start(self) -> datetime | None:
        """The first interval's start; None when there are no intervals."""
        return self.intervals[0].start if self.intervals else None

    @property
    def end(self) -> datetime | None:
        """The last interval's end; None when there are no intervals."""
        return self.intervals[-1].end if self.intervals else None


@dataclass(frozen=True, slots=True)
class Price:
    """What one more kWh imported costs, and one more exported earns, over an interval:
    the client's currency per kWh (:mod:`microgrid.engine.units` converts per-MWh
    prices). Either may be negative; neither may be other than finite."""

    p_import: float
    p_export: float

    def __post_init__(self) -> None:
        for value in (self.p_import, self.p_export):
            if not math.isfinite(value):
                raise ValueError(f"a price that is not finite: {value}")


@dataclass(frozen=True, slots=True)
class Quantiles:
    """A forecast's values at its quantiles, as ``{quantile: value}``: any levels in
    (0, 1) that include the median, such as Solcast's ``{0.1: kW, 0.5: kW, 0.9: kW}``, a
    forecaster's deciles, or a point forecast's ``{0.5: kW}``.

    Between its levels a value is read linearly (:meth:`at`); beyond the outermost, the
    outermost segment extends, on a side where the forecast has a level other than the
    median; on a side where it has none, the median holds, as nothing is known of the
    spread there. A forecast with no level below the median (:attr:`has_lower`) leaves the
    pessimistic case at the median, and a valuation on it names ``pv_spread``.
    """

    kw: Mapping[float, float]

    def __post_init__(self) -> None:
        # A read-only copy: the values checked are the values kept.
        object.__setattr__(self, "kw", MappingProxyType(dict(sorted(self.kw.items()))))
        if 0.5 not in self.kw:
            raise ValueError(f"quantiles must include the median, 0.5: {sorted(self.kw)}")
        for quantile, value in self.kw.items():
            if not 0 < quantile < 1:
                raise ValueError(f"a quantile must be in (0, 1): {quantile}")
            if not math.isfinite(value):
                raise ValueError(f"a value that is not finite at quantile {quantile}: {value}")

    def __hash__(self) -> int:
        return hash(tuple(self.kw.items()))

    def __reduce__(self) -> tuple[type[Quantiles], tuple[dict[float, float]]]:
        # The read-only view does not pickle or deep-copy; its items rebuild it.
        return Quantiles, (dict(self.kw),)

    @classmethod
    def point(cls, kw: float) -> Quantiles:
        """A point forecast."""
        return cls({0.5: kw})

    @property
    def median(self) -> float:
        """The 0.5 quantile."""
        return self.kw[0.5]

    @property
    def has_lower(self) -> bool:
        """Whether a level below the median says something of the downside."""
        return next(iter(self.kw)) < 0.5

    def at(self, quantile: float) -> float:
        """The value at ``quantile``: a level's own value; between levels, linear in the
        values sorted (a forecaster's quantiles can cross); beyond them, the outermost
        segment extended where the forecast has a level on that side of the median, else
        the median (the class's docstring)."""
        if quantile in self.kw:
            return self.kw[quantile]
        return _interpolate(tuple(self.kw), tuple(sorted(self.kw.values())), quantile)


def _interpolate(levels: Sequence[float], values: Sequence[float], q: float) -> float:
    """``q``'s value on the piecewise-linear line through ``(levels, values)``, both
    ascending: the first segment that reaches ``q``, or the last one; with one level, its
    value. Beyond the median on a side with no other level, the median."""
    if len(levels) == 1:
        return values[0]
    median = levels.index(0.5)
    if (q < 0.5 and median == 0) or (q > 0.5 and median == len(levels) - 1):
        return values[median]  # no level on that side: nothing known of the spread there
    segment = next((i for i in range(len(levels) - 1) if q <= levels[i + 1]), len(levels) - 2)
    low, high = levels[segment], levels[segment + 1]
    v_low, v_high = values[segment], values[segment + 1]
    # Written as the three-level cut has always been: a bit-identical value for it.
    return v_low + (v_high - v_low) * (q - low) / (high - low)


def price_mean(pieces: Sequence[tuple[float, Price]]) -> Price:
    """The weighted mean of prices that tile a slot (:func:`align`'s ``mean``)."""
    return Price(
        sum(weight * price.p_import for weight, price in pieces),
        sum(weight * price.p_export for weight, price in pieces),
    )


def quantiles_mean(pieces: Sequence[tuple[float, Quantiles]]) -> Quantiles:
    """The weighted mean, quantile by quantile, of forecasts that tile a slot. Each must
    have the same quantiles. (A mean of quantiles is not the quantile of a mean; over
    the minutes of one slot the difference is small.)"""
    keys = set(pieces[0][1].kw)
    if any(set(q.kw) != keys for _, q in pieces):
        raise ValueError("forecasts that tile a slot must have the same quantiles")
    return Quantiles({key: sum(weight * q.kw[key] for weight, q in pieces) for key in keys})


class MisalignedError(ValueError):
    """A forecast's interval crosses a slot's edge without holding the whole slot: the
    source neither holds over whole slots nor tiles them, so no value can be given
    without guessing. Such a source needs another slot length."""

    def __init__(self, at: datetime, step: timedelta) -> None:
        super().__init__(f"the source's intervals cross the {step} slot at {at.isoformat()}")
        self.at = at
        self.step = step


def _require_grid(starts: Sequence[datetime], step: timedelta) -> None:
    if step <= timedelta(0):
        raise ValueError("step must be positive")
    for earlier, later in pairwise(starts):
        if later - earlier < step:
            raise ValueError(f"slot starts are unsorted or closer than {step} at {later}")


def value_at[T](series: Series[T], at: datetime) -> T | None:
    """The value in force at ``at``: the last sample at or before it. None when there is
    none, when it is None, or when ``at`` is past ``known_until``."""
    require_utc(at)
    if at > series.known_until:
        return None
    index = bisect_right(series.samples, at, key=lambda sample: sample.at)
    return series.samples[index - 1].value if index else None


def means(
    series: Series[float],
    starts: Sequence[datetime],
    step: timedelta,
    *,
    max_gap: timedelta | None = None,
    stale_nonzero: timedelta | None = None,
) -> tuple[float | None, ...]:
    """The time-weighted mean of each slot ``[start, start + step)``.

    A plain average of samples over-weights busy stretches; a series that writes only
    on change (a meter at 0 for days) is badly biased by one. So each slot integrates
    the held values over time.

    A slot is None when:

    - no sample is in force at its start, or it ends after ``known_until``;
    - a sample with no value is in force within it;
    - with ``max_gap``, a silence longer than that overlaps it (a path that went down
      without saying so);
    - with ``stale_nonzero``, a value other than 0 stays silent longer than that: for a
      meter that writes often while it draws and once at 0 when it stops, a lost 0
      must not hold a draw.
    """
    _require_grid(starts, step)
    samples = series.samples
    known_until = series.known_until
    times = [sample.at for sample in samples]
    result: list[float | None] = []
    for start in starts:
        end = start + step
        first = bisect_right(times, start) - 1  # the sample in force at start
        if first < 0 or end > known_until:
            result.append(None)
            continue
        energy = 0.0
        gap_found = False
        index = first
        while index < len(samples) and samples[index].at < end:
            sample = samples[index]
            held_from = max(sample.at, start)
            next_at = samples[index + 1].at if index + 1 < len(samples) else known_until
            held_to = min(next_at, end)
            if sample.value is None:
                if held_to > held_from:
                    gap_found = True
                    break
                index += 1  # superseded at the same instant
                continue
            if max_gap is not None and next_at - sample.at > max_gap:
                gap_found = True
                break
            silent = next_at - sample.at
            if stale_nonzero is not None and sample.value != 0 and silent > stale_nonzero:
                gap_found = True
                break
            energy += sample.value * (held_to - held_from).total_seconds()
            index += 1
        result.append(None if gap_found else energy / step.total_seconds())
    return tuple(result)


def signed_means(
    series: Series[float], starts: Sequence[datetime], step: timedelta
) -> tuple[tuple[float | None, ...], tuple[float | None, ...]]:
    """Each slot's time-weighted mean of the positive part of a signed series, and of
    its negative part as a positive number: a grid meter's import and export, kept
    apart where one slot holds both."""
    positive = Series(
        tuple(Sample(s.at, None if s.value is None else max(s.value, 0.0)) for s in series.samples),
        series.known_until,
        series.unit,
    )
    negative = Series(
        tuple(
            Sample(s.at, None if s.value is None else max(-s.value, 0.0)) for s in series.samples
        ),
        series.known_until,
        series.unit,
    )
    return means(positive, starts, step), means(negative, starts, step)


def alive[T](heartbeat: Series[T], limit: timedelta) -> tuple[tuple[datetime, datetime], ...]:
    """Where a heartbeat shows the path alive: within ``limit`` after each of its samples,
    merged. Every sample counts, whatever its value: it arrived, so the path was up."""
    spans: list[tuple[datetime, datetime]] = []
    for sample in heartbeat.samples:
        beat = sample.at
        if spans and beat <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], beat + limit))
        else:
            spans.append((beat, beat + limit))
    return tuple(spans)


@dataclass(frozen=True, slots=True)
class Segment:
    """A stretch over which no series changed: each one's value (None where unknown),
    and whether the path was alive."""

    start: datetime
    end: datetime
    values: Mapping[str, float | None]
    alive: bool


def segments(
    series: Mapping[str, Series[float]],
    cuts: Sequence[datetime],
    *,
    alive_spans: Sequence[tuple[datetime, datetime]] | None = None,
    stale_nonzero: Mapping[str, timedelta] | None = None,
) -> Iterator[Segment]:
    """Sweep several series together from ``cuts[0]`` to ``cuts[-1]``, one segment per
    stretch over which none of them changes.

    A segment ends at every cut (a slot's edge, say), at every sample, at each edge of
    ``alive_spans`` and at each series' ``known_until``. A series' value is the sample
    in force at the segment's start; None before its first sample, from its
    ``known_until`` on, or, for a name in ``stale_nonzero``, once a value other than 0
    has been silent that long. Without ``alive_spans`` the path is taken as alive.

    Segments come one at a time, so a consumer that sums them (a ledger over a year)
    keeps none; the series themselves, and their sample times, are held. The
    arguments are checked when it is called, not when the first segment is taken.
    """
    for earlier, later in pairwise(cuts):
        if later <= earlier:
            raise ValueError(f"cuts are unsorted at {later}")
    stale = stale_nonzero or {}
    if unknown := set(stale) - set(series):
        raise ValueError(f"no series named {sorted(unknown)}")
    return _sweep(series, cuts, alive_spans, stale)


def _sweep(
    series: Mapping[str, Series[float]],
    cuts: Sequence[datetime],
    alive_spans: Sequence[tuple[datetime, datetime]] | None,
    stale: Mapping[str, timedelta],
) -> Iterator[Segment]:
    if len(cuts) < 2:
        return
    low_edge, high_edge = cuts[0], cuts[-1]
    names = list(series)
    points = set(cuts)
    for one in series.values():
        points.update(s.at for s in one.samples if low_edge < s.at < high_edge)
        if low_edge < one.known_until < high_edge:
            points.add(one.known_until)
    spans = list(alive_spans) if alive_spans is not None else None
    for low, high in spans or ():
        points.update(at for at in (low, high) if low_edge < at < high_edge)
    for name, limit in stale.items():
        stale_at = (s.at + limit for s in series[name].samples if s.value not in (0, None))
        points.update(at for at in stale_at if low_edge < at < high_edge)
    cursors = dict.fromkeys(names, 0)
    span = 0
    for low, high in pairwise(sorted(points)):
        values: dict[str, float | None] = {}
        for name in names:
            samples = series[name].samples
            cursor = cursors[name]
            while cursor < len(samples) and samples[cursor].at <= low:
                cursor += 1
            cursors[name] = cursor
            value = samples[cursor - 1].value if cursor else None
            if low >= series[name].known_until:
                value = None
            elif (
                name in stale
                and value is not None
                and value != 0
                and low - samples[cursor - 1].at >= stale[name]
            ):
                value = None  # stale from that moment on
            values[name] = value
        if spans is None:
            is_alive = True
        else:
            while span < len(spans) and spans[span][1] <= low:
                span += 1
            is_alive = span < len(spans) and spans[span][0] <= low
        yield Segment(low, high, values, is_alive)


@dataclass(frozen=True, slots=True)
class CounterDelta:
    """What a cumulative counter counted since a start."""

    total: float
    #: Readings below the highest so far: a counter's drop, held over, not counted from.
    falls: int
    #: The last reading's time.
    last_at: datetime


def counter_delta(series: Series[float], start: datetime) -> CounterDelta:
    """What a cumulative counter (an energy meter's total) counted since ``start``: its
    rises above the highest reading so far, from the last reading at or before
    ``start``. A reading below that is held over, not counted from, so a counter that
    drops to zero and recovers counts nothing twice. Samples without a value are
    skipped. Of two readings at one instant, the later in the series is the later
    reading. Raises when no reading is at or before ``start``."""
    require_utc(start)
    readings = [(s.at, s.value) for s in series.samples if s.value is not None]
    before = [value for at, value in readings if at <= start]
    if not before:
        raise ValueError("no reading at or before the start")
    total, high, falls = 0.0, before[-1], 0
    for at, value in readings:
        if at <= start:
            continue
        if value < high:
            falls += 1
            continue
        total += value - high
        high = value
    return CounterDelta(total=total, falls=falls, last_at=readings[-1][0])


def _float_mean[V](pieces: Sequence[tuple[float, V]]) -> V:
    total = 0.0
    for weight, value in pieces:
        if not isinstance(value, Real):
            raise TypeError(f"values that are not numbers need a mean: {value!r}")
        total += weight * float(value)
    return cast(V, total)


type Mean[V] = Callable[[Sequence[tuple[float, V]]], V]


@overload
def align(
    forecast: Forecast[float], starts: Sequence[datetime], step: timedelta, *, mean: None = None
) -> tuple[float | None, ...]: ...


@overload
def align[V](
    forecast: Forecast[V], starts: Sequence[datetime], step: timedelta, *, mean: Mean[V]
) -> tuple[V | None, ...]: ...


def align[V](
    forecast: Forecast[V],
    starts: Sequence[datetime],
    step: timedelta,
    *,
    mean: Mean[V] | None = None,
) -> tuple[V | None, ...]:
    """The forecast's value in each slot ``[start, start + step)``.

    - **An interval that holds the whole slot** gives its value: an hourly price, or a
      half-hourly forecast, over 10-minute slots.
    - **Intervals that tile the slot** (each wholly inside it, together covering it)
      give their mean, weighted by length: 5-minute prices over 10-minute slots. Values
      that are not numbers need ``mean``, given ``(weight, value)`` pairs whose weights
      sum to 1.
    - **A slot not wholly covered** (before the first interval, in a gap, past the end)
      is None: unknown, never guessed.
    - **An interval that crosses the slot's edge without holding the whole slot**
      (15-minute prices over 10-minute slots) raises :class:`MisalignedError`, whether
      or not the slot is otherwise covered: that source needs another slot length.
    """
    return _align(forecast, starts, step, mean)


def _align[V](
    forecast: Forecast[V], starts: Sequence[datetime], step: timedelta, mean: Mean[V] | None
) -> tuple[V | None, ...]:
    _require_grid(starts, step)
    intervals = forecast.intervals
    combine = mean if mean is not None else _float_mean
    whole = step.total_seconds()
    result: list[V | None] = []
    index = 0
    for start in starts:
        end = start + step
        while index < len(intervals) and intervals[index].end <= start:
            index += 1
        overlapping = []
        cursor = index
        while cursor < len(intervals) and intervals[cursor].start < end:
            overlapping.append(intervals[cursor])
            cursor += 1
        if len(overlapping) == 1 and overlapping[0].start <= start and overlapping[0].end >= end:
            result.append(overlapping[0].value)
            continue
        if any(part.start < start or part.end > end for part in overlapping):
            raise MisalignedError(start, step)
        covered = (
            bool(overlapping)
            and overlapping[0].start == start
            and overlapping[-1].end == end
            and all(a.end == b.start for a, b in pairwise(overlapping))
        )
        if not covered:
            result.append(None)  # before the first interval, in a gap, or past the end
            continue
        result.append(
            combine([((p.end - p.start).total_seconds() / whole, p.value) for p in overlapping])
        )
    return tuple(result)


def is_stale[V](
    forecast: Forecast[V], at: datetime, max_age: timedelta, *, by_start: bool = True
) -> bool:
    """Whether ``forecast`` is too old to use at ``at``: issued more than ``max_age``
    before it, or empty. With ``by_start``, also when its first interval starts more
    than ``max_age`` before it: a source that republishes an old prediction keeps its
    issue time fresh. Without, for a source whose forecast reaches back to the start of
    its day (Solcast's), only the issue time counts."""
    require_utc(at)
    if not forecast.intervals:
        return True
    if at - forecast.issued_at > max_age:
        return True
    return by_start and at - forecast.intervals[0].start > max_age


@dataclass(frozen=True, slots=True)
class Composed[V]:
    """Each slot's value from the forecasts read, and the forecast in force: the newest."""

    values: tuple[V | None, ...]
    #: The newest forecast's issue time and source; None when none was issued by then.
    issued_at: datetime | None
    source: str | None


@overload
def compose(
    issues: Sequence[Forecast[float]],
    starts: Sequence[datetime],
    step: timedelta,
    *,
    at: datetime,
    max_age: timedelta,
    by_start: bool = True,
    issued_by_at: bool = True,
    mean: None = None,
) -> Composed[float]: ...


@overload
def compose[V](
    issues: Sequence[Forecast[V]],
    starts: Sequence[datetime],
    step: timedelta,
    *,
    at: datetime,
    max_age: timedelta,
    by_start: bool = True,
    issued_by_at: bool = True,
    mean: Mean[V],
) -> Composed[V]: ...


def compose[V](
    issues: Sequence[Forecast[V]],
    starts: Sequence[datetime],
    step: timedelta,
    *,
    at: datetime,
    max_age: timedelta,
    by_start: bool = True,
    issued_by_at: bool = True,
    mean: Mean[V] | None = None,
) -> Composed[V]:
    """The forecasts issued by ``at``, on the slots, as they stood at ``at``: **each slot
    takes the newest forecast that covers it, of those not stale** (:func:`is_stale`,
    with ``by_start``).

    A source whose newest forecast starts after the slot under way (one that publishes
    from the next half hour) still covers that slot from an earlier forecast. A slot
    none covers is None. When the newest is stale, every slot is None: a stale input is
    never reused. A forecast issued after ``at`` is never read, so a replay cannot look
    ahead; with none issued by then, every slot is None. A caller that has already kept
    only the forecasts it had received by ``at`` passes ``issued_by_at=False``: a
    forecast received by then counts, though its source's clock stamps it a moment
    later.
    """
    return _compose(issues, starts, step, at, max_age, by_start, issued_by_at, mean)


def _compose[V](
    issues: Sequence[Forecast[V]],
    starts: Sequence[datetime],
    step: timedelta,
    at: datetime,
    max_age: timedelta,
    by_start: bool,
    issued_by_at: bool,
    mean: Mean[V] | None,
) -> Composed[V]:
    require_utc(at)
    _require_grid(starts, step)
    known = [f for f in issues if f.issued_at <= at] if issued_by_at else list(issues)
    if not known:
        return Composed((None,) * len(starts), None, None)
    by_age = sorted(known, key=lambda forecast: forecast.issued_at, reverse=True)
    newest = by_age[0]
    values: tuple[V | None, ...]
    if is_stale(newest, at, max_age, by_start=by_start):
        values = (None,) * len(starts)
    else:
        fresh = [f for f in by_age if not is_stale(f, at, max_age, by_start=by_start)]
        columns = []
        for forecast in fresh:  # a loop, not a comprehension, for mypy's inference of V
            aligned = _align(forecast, starts, step, mean)
            columns.append(aligned)
        values = tuple(
            next((value for value in column if value is not None), None)
            for column in zip(*columns, strict=True)
        )
    return Composed(values, newest.issued_at, newest.source)


def prune[V](
    issues: Sequence[Forecast[V]], at: datetime, max_age: timedelta
) -> tuple[Forecast[V], ...]:
    """The forecasts worth keeping at ``at``, oldest first: one per issue time (the first
    given: one read again does not replace it), less every stale one but the newest,
    whose staleness a valuation must still report (:func:`compose`). Bounded by the
    age limit."""
    require_utc(at)
    by_issue: dict[datetime, Forecast[V]] = {}
    for forecast in issues:
        by_issue.setdefault(forecast.issued_at, forecast)
    ordered = [by_issue[issued] for issued in sorted(by_issue)]
    return tuple(f for f in ordered[:-1] if not is_stale(f, at, max_age)) + tuple(ordered[-1:])


def to_forecast(
    series: Series[float],
    start: datetime,
    end: datetime,
    step: timedelta,
    *,
    issued_at: datetime,
    source: str = "",
    origin: datetime = EPOCH,
) -> Forecast[float]:
    """A recorded sensor (a price, say) as intervals of ``step``, counted from ``origin``,
    over the intervals that overlap ``[start, end)``.

    A sample applies from the start of the interval it falls in: a price recorded a few
    seconds after the hour is that hour's price. Of several in one interval, the last
    in time wins. A value holds until the next. An interval with no value in force, or
    starting at or after ``known_until``, is left out: a gap. An interval under way at
    ``known_until`` is given the value in force by then; a later sample within it, not
    yet seen, would replace it.
    """
    require_utc(start, end, issued_at, origin)
    if step <= timedelta(0):
        raise ValueError("step must be positive")

    def floor(moment: datetime) -> datetime:
        return origin + ((moment - origin) // step) * step

    samples = series.samples
    intervals: list[Interval[float]] = []
    index = 0
    current: float | None = None
    slot = floor(start)
    while slot < end:
        while index < len(samples) and floor(samples[index].at) <= slot:
            current = samples[index].value
            index += 1
        if current is not None and slot < series.known_until:
            intervals.append(Interval(slot, slot + step, current))
        slot += step
    return Forecast(tuple(intervals), issued_at, source)
