"""The series toolkit: what it does beyond the code it replaces.

``tests/property/test_series_oracles.py`` holds each operation to the function it
replaces. These cover what none of those did: samples with no value, ``known_until``,
sources that tile a slot or cross it, no look-ahead, and the types' own checks.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from microgrid.engine import series as ts

T0 = datetime(2026, 3, 29, tzinfo=UTC)  # Europe's spring DST change: grids are UTC
MIN = timedelta(minutes=1)
SLOT = timedelta(minutes=10)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _series(*points: tuple[float, float | None], known: float = 600) -> ts.Series[float]:
    return ts.Series.of(((_at(m), v) for m, v in points), known_until=_at(known))


def _forecast(*parts: tuple[float, float, float], issued: float = 0) -> ts.Forecast[float]:
    return ts.Forecast(
        tuple(ts.Interval(_at(a), _at(b), v) for a, b, v in parts), _at(issued), "test"
    )


def _grid(first: float, count: int, step: timedelta = SLOT) -> list[datetime]:
    return [_at(first) + i * step for i in range(count)]


# -- the types --------------------------------------------------------------------------


def test_a_series_is_in_time_order_and_known_through_its_samples() -> None:
    with pytest.raises(ValueError, match="out of time order"):
        _series((5, 1.0), (4, 1.0))
    with pytest.raises(ValueError, match="after known_until"):
        _series((5, 1.0), known=4)
    with pytest.raises(ValueError, match="UTC"):
        ts.Series.of([], known_until=datetime(2026, 3, 29, tzinfo=ZoneInfo("Europe/Berlin")))
    with pytest.raises(ValueError, match="UTC"):
        ts.Sample(datetime(2026, 3, 29), 1.0)  # naive
    same_instant = _series((5, 1.0), (5, 2.0))
    assert same_instant.times == (_at(5), _at(5))
    assert ts.value_at(same_instant, _at(5)) == 2.0  # the last at an instant is in force


def test_a_number_that_is_not_finite_is_no_value_in_a_sample_and_refused_in_a_forecast() -> None:
    assert ts.Sample(T0, float("nan")).value is None
    assert ts.Sample(T0, float("-inf")).value is None
    assert ts.Sample(T0, Fraction(1, 3)).value == Fraction(1, 3)  # any real number, if finite
    assert ts.means(_series((0, 1.0), (3, float("nan"))), _grid(0, 1), SLOT) == (None,)
    with pytest.raises(ValueError, match="not finite"):
        ts.Interval(_at(0), _at(30), float("nan"))


def test_a_forecast_may_have_gaps_but_not_overlaps() -> None:
    gappy = _forecast((0, 30, 1.0), (60, 90, 2.0))
    assert (gappy.start, gappy.end) == (_at(0), _at(90))
    with pytest.raises(ValueError, match="overlap"):
        _forecast((0, 30, 1.0), (20, 50, 2.0))
    with pytest.raises(ValueError, match="ends before"):
        ts.Interval(_at(5), _at(5), 1.0)
    empty = ts.Forecast[float]((), T0)
    assert (empty.start, empty.end) == (None, None)


# -- value_at and means -----------------------------------------------------------------


def test_value_at_is_unknown_past_known_until_and_where_there_is_no_value() -> None:
    series = _series((0, 1.0), (10, None), (20, 3.0), known=30)
    assert ts.value_at(series, _at(-1)) is None
    assert ts.value_at(series, _at(5)) == 1.0
    assert ts.value_at(series, _at(15)) is None  # unavailable
    assert ts.value_at(series, _at(30)) == 3.0
    assert ts.value_at(series, _at(31)) is None  # past what the client vouches for


def test_a_slot_with_no_value_in_force_is_unknown() -> None:
    series = _series((0, 2.0), (15, None), (18, 4.0), known=60)
    assert ts.means(series, _grid(0, 3), SLOT) == (2.0, None, 4.0)


def test_a_no_value_replaced_at_the_same_instant_leaves_no_gap() -> None:
    series = _series((0, 2.0), (5, None), (5, 4.0), known=60)
    assert ts.means(series, _grid(0, 1), SLOT) == (3.0,)


def test_means_need_a_positive_step_and_a_grid_in_order() -> None:
    series = _series((0, 1.0))
    with pytest.raises(ValueError, match="positive"):
        ts.means(series, _grid(0, 2), timedelta(0))
    with pytest.raises(ValueError, match="closer than"):
        ts.means(series, [_at(0), _at(5)], SLOT)


def test_signed_means_keep_no_value_unknown() -> None:
    series = _series((0, 3.0), (5, -1.0), (10, None), known=60)
    assert ts.signed_means(series, _grid(0, 2), SLOT) == ((1.5, None), (0.5, None))


def test_a_held_value_fills_the_slot() -> None:
    assert ts.means(_series((-600, 0.001), known=60), [T0], SLOT) == pytest.approx((0.001,))


def test_means_weigh_time_not_rows() -> None:
    # 0.45 kW for 1 minute with rows every 6 s, then 1 W for 9 minutes.
    rows = [(m / 10, 0.45) for m in range(10)] + [(1.0, 0.001)]
    (mean,) = ts.means(_series(*rows, known=60), [T0], SLOT)
    assert mean == pytest.approx(0.45 * 0.1 + 0.001 * 0.9)
    assert (0.45 * 10 + 0.001) / 11 > 0.4  # what a plain average of the rows would say


def test_slots_split_at_a_change() -> None:
    assert ts.means(_series((0, 1.0), (5, 3.0), known=60), _grid(0, 2), SLOT) == pytest.approx(
        (2.0, 3.0)
    )


def test_nothing_in_force_or_not_yet_known_is_unknown() -> None:
    assert ts.means(_series((5, 1.0), known=60), [T0], SLOT) == (None,)  # starts before data
    assert ts.means(_series((0, 1.0), known=9), [T0], SLOT) == (None,)  # not known to the end


def test_a_silence_longer_than_max_gap_is_unknown() -> None:
    series = _series((0, 1.0), (4, 1.1), known=20)  # then silent to known_until
    gap = timedelta(minutes=3)
    assert ts.means(series, _grid(0, 2), SLOT, max_gap=gap) == (None, None)
    assert ts.means(series, _grid(0, 2), SLOT) == pytest.approx((1.06, 1.1))  # it holds
    spanning = _series((-30, 1.0), (5, 1.0), known=60)
    assert ts.means(spanning, [T0], SLOT, max_gap=gap) == (None,)


def test_signed_means_keep_import_and_export_apart() -> None:
    # Importing 2 kW for the first half of the slot, exporting 1 kW for the second.
    imported, exported = ts.signed_means(_series((0, 2.0), (5, -1.0), known=10), [T0], SLOT)
    assert imported == pytest.approx((1.0,))
    assert exported == pytest.approx((0.5,))


def test_a_reading_other_than_0_silent_too_long_is_unknown_and_0_holds() -> None:
    # A meter that writes often while it draws and once at 0 when it stops: a draw
    # silent past the limit lost its 0.
    stale = timedelta(minutes=5)
    grid = _grid(0, 4)
    stuck = _series((-120, 7.0), known=40)
    assert ts.means(stuck, grid, SLOT) == (7.0,) * 4
    assert ts.means(stuck, grid, SLOT, stale_nonzero=stale) == (None,) * 4
    idle = _series((-5 * 24 * 60, 0.0), known=40)
    assert ts.means(idle, grid, SLOT, stale_nonzero=stale) == (0.0,) * 4
    writing = _series(*((k / 12, 3.0) for k in range(4 * 120)), known=40)
    assert ts.means(writing, grid, SLOT, stale_nonzero=stale) == pytest.approx((3.0,) * 4)


#: Sorted times in seconds, duplicates allowed (a recorder can hold two rows at one
#: instant), and their values.
_points = st.lists(
    st.tuples(st.integers(0, 3600), st.floats(0, 10, allow_nan=False)), min_size=1, max_size=40
).map(sorted)


def _held(points: Sequence[tuple[int, float]], second: int) -> float | None:
    """The step function by brute force: the last value at or before ``second``."""
    held = [value for at, value in points if at <= second]
    return held[-1] if held else None


@given(_points)
def test_means_match_a_brute_force_integral(points: list[tuple[int, float]]) -> None:
    """An independent check: the step function averaged second by second."""
    series = ts.Series.of(((T0 + timedelta(seconds=s), v) for s, v in points), known_until=_at(120))
    starts = _grid(0, 6)
    for start, mean in zip(starts, ts.means(series, starts, SLOT), strict=True):
        first = int((start - T0).total_seconds())
        values = [_held(points, first + i) for i in range(600)]
        if values[0] is None:
            assert mean is None
        else:
            assert mean is not None
            assert abs(mean - sum(v for v in values if v is not None) / 600) < 1e-9


@given(_points, st.integers(30, 900))
def test_a_slot_is_unknown_exactly_when_a_long_silence_overlaps_it(
    points: list[tuple[int, float]], gap_s: int
) -> None:
    gap = timedelta(seconds=gap_s)
    series = ts.Series.of(((T0 + timedelta(seconds=s), v) for s, v in points), known_until=_at(60))
    starts = _grid(0, 6)
    times = [*series.times, _at(60)]
    silences = [(a, b) for a, b in pairwise(times) if b - a > gap]
    for start, mean in zip(starts, ts.means(series, starts, SLOT, max_gap=gap), strict=True):
        in_force = any(at <= start for at in times[:-1])
        overlaps = any(a < start + SLOT and b > start for a, b in silences)
        assert (mean is None) == (not in_force or overlaps)


# -- segments and alive -----------------------------------------------------------------


def test_segments_cut_at_every_change_and_mark_what_is_unknown() -> None:
    pv = _series((-5, 4.0), (3, 2.0), known=8)
    load = _series((1, 1.0), (6, 0.0))
    beats = _series((-5, 1.0), (0, 1.0))
    spans = ts.alive(beats, 4 * MIN)  # alive from -5 to 4
    got = [
        (s.start, s.end, dict(s.values), s.alive)
        for s in ts.segments({"pv": pv, "load": load}, [_at(0), _at(10)], alive_spans=spans)
    ]
    assert got == [
        (_at(0), _at(1), {"pv": 4.0, "load": None}, True),
        (_at(1), _at(3), {"pv": 4.0, "load": 1.0}, True),
        (_at(3), _at(4), {"pv": 2.0, "load": 1.0}, True),
        (_at(4), _at(6), {"pv": 2.0, "load": 1.0}, False),
        (_at(6), _at(8), {"pv": 2.0, "load": 0.0}, False),
        (_at(8), _at(10), {"pv": None, "load": 0.0}, False),  # pv known only to 8
    ]


def test_a_nonzero_value_silent_too_long_goes_stale_and_a_zero_never_does() -> None:
    draw = _series((0, 7.0), (12, 0.0), known=40)
    got = [
        (s.start, s.values["draw"])
        for s in ts.segments({"draw": draw}, [_at(0), _at(40)], stale_nonzero={"draw": 5 * MIN})
    ]
    assert got == [(_at(0), 7.0), (_at(5), None), (_at(12), 0.0)]


def test_segments_refuse_cuts_out_of_order_and_unknown_stale_names() -> None:
    with pytest.raises(ValueError, match="unsorted"):
        ts.segments({}, [_at(5), _at(0)])  # at the call, not at the first segment
    with pytest.raises(ValueError, match="no series"):
        ts.segments({}, [_at(0), _at(5)], stale_nonzero={"x": MIN})
    assert list(ts.segments({"a": _series((0, 1.0))}, [_at(0)])) == []


def test_every_heartbeat_counts_whatever_its_value() -> None:
    beats = ts.Series.of([(_at(0), None), (_at(2), "on")], known_until=_at(10))
    assert ts.alive(beats, 3 * MIN) == ((_at(0), _at(5)),)


# -- counters ---------------------------------------------------------------------------


def test_of_two_counter_readings_at_one_instant_the_later_in_the_series_counts() -> None:
    meter = _series((0, 7.0), (0, 3.0), (5, 8.0))
    assert ts.counter_delta(meter, _at(0)).total == 5.0  # from 3.0, not from 7.0


def test_a_counter_skips_readings_with_no_value_and_holds_over_a_drop() -> None:
    meter = _series((0, 100.0), (10, None), (20, 0.0), (30, 101.0), (40, 103.5))
    delta = ts.counter_delta(meter, _at(5))
    assert (delta.total, delta.falls, delta.last_at) == (3.5, 1, _at(40))
    with pytest.raises(ValueError, match="no reading"):
        ts.counter_delta(meter, _at(-1))


# -- align ------------------------------------------------------------------------------


def test_an_interval_that_holds_the_slot_gives_its_value() -> None:
    hourly = _forecast((0, 60, 0.30), (60, 120, 0.40))
    assert ts.align(hourly, _grid(0, 12), SLOT) == (0.30,) * 6 + (0.40,) * 6


def test_intervals_that_tile_the_slot_give_their_weighted_mean() -> None:
    five_minute = _forecast((0, 5, 1.0), (5, 10, 2.0), (10, 15, 4.0), (15, 20, 4.0))
    assert ts.align(five_minute, _grid(0, 2), SLOT) == (1.5, 4.0)
    uneven = _forecast((0, 2, 10.0), (2, 10, 0.0))
    assert ts.align(uneven, _grid(0, 1), SLOT) == (2.0,)


def test_a_slot_not_wholly_covered_is_unknown() -> None:
    gappy = _forecast((0, 5, 1.0), (10, 20, 2.0), (25, 30, 3.0))
    assert ts.align(gappy, _grid(0, 3), SLOT) == (None, 2.0, None)
    assert ts.align(gappy, _grid(-10, 1), SLOT) == (None,)  # before it starts
    assert ts.align(gappy, _grid(30, 1), SLOT) == (None,)  # past its end


def test_a_source_that_crosses_the_slots_edges_is_refused() -> None:
    quarter_hours = _forecast((0, 15, 1.0), (15, 30, 2.0))
    with pytest.raises(ts.MisalignedError) as raised:
        ts.align(quarter_hours, _grid(0, 3), SLOT)
    assert raised.value.at == _at(10) and raised.value.step == SLOT
    assert ts.align(quarter_hours, _grid(0, 2, timedelta(minutes=15)), timedelta(minutes=15)) == (
        1.0,
        2.0,
    )


def test_a_crossing_interval_is_refused_even_where_the_slot_is_not_covered() -> None:
    gappy_quarters = _forecast((0, 15, 1.0), (30, 45, 2.0))
    with pytest.raises(ts.MisalignedError) as raised:
        ts.align(gappy_quarters, _grid(0, 6), SLOT)
    assert raised.value.at == _at(10)


def test_values_that_are_not_numbers_need_a_mean_where_a_slot_is_tiled() -> None:
    pairs = ts.Forecast(
        (ts.Interval(_at(0), _at(5), (0.2, 0.1)), ts.Interval(_at(5), _at(10), (0.4, 0.1))),
        T0,
    )
    with pytest.raises(TypeError, match="need a mean"):
        ts.align(pairs, _grid(0, 1), SLOT)  # type: ignore[arg-type]  # mypy refuses it too


def test_values_that_are_not_numbers_take_the_clients_mean() -> None:
    def pair_mean(pieces: Sequence[tuple[float, tuple[float, float]]]) -> tuple[float, float]:
        return (sum(w * v[0] for w, v in pieces), sum(w * v[1] for w, v in pieces))

    prices = ts.Forecast(
        (
            ts.Interval(_at(0), _at(5), (0.2, 0.1)),
            ts.Interval(_at(5), _at(10), (0.4, 0.1)),
        ),
        T0,
    )
    (value,) = ts.align(prices, _grid(0, 1), SLOT, mean=pair_mean)
    assert value == pytest.approx((0.3, 0.1))


@given(
    st.lists(st.floats(-5, 5, allow_nan=False), min_size=1, max_size=24),
    st.sampled_from([1, 2, 5, 10]),
)
def test_a_source_that_tiles_the_slot_keeps_its_energy(values: list[float], minutes: int) -> None:
    """A tiling source's slot means hold the same energy as the source."""
    step = timedelta(minutes=minutes)
    count = len(values) - len(values) % (10 // minutes) or 0
    values = values[:count]
    if not values:
        return
    forecast = ts.Forecast(
        tuple(ts.Interval(T0 + i * step, T0 + (i + 1) * step, v) for i, v in enumerate(values)),
        T0,
    )
    slots = count * minutes // 10
    aligned = ts.align(forecast, _grid(0, slots), SLOT)
    assert all(value is not None for value in aligned)
    assert sum(v for v in aligned if v is not None) * 10 == pytest.approx(sum(values) * minutes)


# -- compose and prune ------------------------------------------------------------------


def test_compose_checks_its_grid_whatever_the_forecasts() -> None:
    with pytest.raises(ValueError, match="closer than"):
        ts.compose([], [_at(0), _at(5)], SLOT, at=_at(0), max_age=MIN)


def test_compose_never_reads_a_forecast_issued_after_the_moment() -> None:
    old = _forecast((0, 60, 1.0), issued=-10)
    later = _forecast((0, 60, 9.0), issued=20)
    composed = ts.compose([old, later], _grid(0, 3), SLOT, at=_at(15), max_age=60 * MIN)
    assert composed.values == (1.0, 1.0, 1.0)
    assert (composed.issued_at, composed.source) == (_at(-10), "test")
    nothing = ts.compose([later], _grid(0, 2), SLOT, at=_at(15), max_age=60 * MIN)
    assert nothing == ts.Composed((None, None), None, None)


def test_compose_fills_the_slot_under_way_from_an_earlier_forecast() -> None:
    earlier = _forecast((0, 30, 1.0), (30, 60, 1.5), issued=-2)
    newest = _forecast((30, 60, 2.0), (60, 90, 3.0), issued=28)  # publishes from the next half hour
    composed = ts.compose([earlier, newest], _grid(20, 5), SLOT, at=_at(28), max_age=60 * MIN)
    assert composed.values == (1.0, 2.0, 2.0, 2.0, 3.0)
    assert composed.issued_at == _at(28)


def test_compose_reuses_nothing_once_the_newest_is_stale() -> None:
    stale = _forecast((0, 120, 1.0), issued=0)
    composed = ts.compose([stale], _grid(100, 2), SLOT, at=_at(100), max_age=90 * MIN)
    assert composed.values == (None, None)
    assert composed.issued_at == _at(0)


def test_a_forecast_is_stale_by_its_issue_or_its_first_interval() -> None:
    assert ts.is_stale(ts.Forecast[float]((), T0), T0, MIN)
    republished = _forecast((0, 300, 1.0), issued=200)  # an old prediction, a new issue time
    assert ts.is_stale(republished, _at(200), 90 * MIN)
    assert not ts.is_stale(republished, _at(200), 240 * MIN)


def test_prune_keeps_one_per_issue_and_the_newest_even_stale() -> None:
    first = _forecast((0, 30, 1.0), issued=0)
    again = _forecast((0, 30, 5.0), issued=0)  # the same issue, read twice
    newest = _forecast((0, 30, 2.0), issued=10)
    assert ts.prune([first, again, newest], _at(20), 60 * MIN) == (first, newest)
    assert ts.prune([first, newest], _at(500), 60 * MIN) == (newest,)


# -- to_forecast ------------------------------------------------------------------------


def test_a_recorded_sensor_applies_from_its_interval_and_the_later_reading_wins() -> None:
    prices = _series((0.1, 0.30), (30, 0.10), (31, 0.35), (60.2, None), (130, 0.20), known=200)
    hourly = ts.to_forecast(prices, _at(0), _at(180), 60 * MIN, issued_at=_at(200), source="tk")
    assert [(i.start, i.end, i.value) for i in hourly.intervals] == [
        (_at(0), _at(60), 0.35),  # of two in the hour, the later
        (_at(120), _at(180), 0.20),  # 60-120 had no value
    ]
    assert (hourly.issued_at, hourly.source) == (_at(200), "tk")


def test_the_interval_under_way_at_known_until_takes_the_value_in_force_then() -> None:
    prices = _series((0, 0.30), known=70)
    (_, under_way) = ts.to_forecast(prices, _at(0), _at(120), 60 * MIN, issued_at=_at(70)).intervals
    assert (under_way.start, under_way.end, under_way.value) == (_at(60), _at(120), 0.30)


def test_to_forecast_stops_at_known_until_and_counts_from_its_origin() -> None:
    prices = _series((0, 0.30), known=70)
    assert len(ts.to_forecast(prices, _at(0), _at(180), 60 * MIN, issued_at=_at(70)).intervals) == 2
    half_hour_zone = ts.to_forecast(
        prices, _at(0), _at(60), 60 * MIN, issued_at=_at(70), origin=_at(30)
    )
    assert [i.start for i in half_hour_zone.intervals] == [_at(-30), _at(30)]
    with pytest.raises(ValueError, match="positive"):
        ts.to_forecast(prices, _at(0), _at(60), timedelta(0), issued_at=_at(70))


# -- prices and quantiles ---------------------------------------------------------------


def test_a_price_is_finite_and_may_be_negative() -> None:
    assert ts.Price(-0.05, -0.02).p_import == -0.05
    with pytest.raises(ValueError, match="not finite"):
        ts.Price(float("nan"), 0.1)


def test_quantiles_take_any_levels_with_the_median() -> None:
    """Any levels in (0, 1) with the median, not Solcast's three alone; between levels,
    linear; beyond, the outermost segment on a side with a level, else the median."""
    three = ts.Quantiles({0.1: 1.0, 0.5: 2.0, 0.9: 3.0})
    assert (three.at(0.1), three.median, three.at(0.9)) == (1.0, 2.0, 3.0)
    assert three.has_lower and three.at(0.3) == pytest.approx(1.5)
    assert three.at(0.05) == pytest.approx(0.875)  # extended below the lowest level
    point = ts.Quantiles.point(2.5)
    assert (point.at(0.1), point.median, point.at(0.9)) == (2.5, 2.5, 2.5)
    assert not point.has_lower
    quartiles = ts.Quantiles({0.25: 1.0, 0.5: 2.0, 0.75: 3.0})
    assert quartiles.at(0.1) == pytest.approx(0.4) and quartiles.has_lower
    upper = ts.Quantiles({0.5: 2.0, 0.9: 3.0})
    assert not upper.has_lower and upper.at(0.1) == 2.0  # nothing known below: the median
    with pytest.raises(ValueError, match="median"):
        ts.Quantiles({0.1: 1.0, 0.9: 3.0})
    with pytest.raises(ValueError, match=r"\(0, 1\)"):
        ts.Quantiles({0.5: 1.0, 1.0: 3.0})
    with pytest.raises(ValueError, match="not finite"):
        ts.Quantiles({0.5: float("inf")})


def test_prices_and_quantiles_that_tile_a_slot_take_their_weighted_mean() -> None:
    price = ts.price_mean([(0.5, ts.Price(0.2, 0.1)), (0.5, ts.Price(0.4, 0.0))])
    assert (price.p_import, price.p_export) == pytest.approx((0.3, 0.05))
    mean = ts.quantiles_mean([(0.25, ts.Quantiles.point(4.0)), (0.75, ts.Quantiles.point(0.0))])
    assert mean == ts.Quantiles.point(1.0)
    with pytest.raises(ValueError, match="same quantiles"):
        ts.quantiles_mean(
            [(0.5, ts.Quantiles.point(1.0)), (0.5, ts.Quantiles({0.1: 0.0, 0.5: 1.0, 0.9: 2.0}))]
        )


def test_a_forecast_reaching_back_to_its_day_is_judged_by_its_issue_alone() -> None:
    from_midnight = _forecast((-600, 900, 1.0), issued=-10)
    assert ts.is_stale(from_midnight, _at(0), 90 * MIN)  # by its first interval too
    assert not ts.is_stale(from_midnight, _at(0), 90 * MIN, by_start=False)
    assert ts.is_stale(from_midnight, _at(100), 90 * MIN, by_start=False)  # issued too long ago
    composed = ts.compose(
        [from_midnight], _grid(0, 2), SLOT, at=_at(0), max_age=90 * MIN, by_start=False
    )
    assert composed.values == (1.0, 1.0)
