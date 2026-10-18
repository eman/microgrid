"""Units converted once, at the edge."""

import math
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from microgrid.engine import series as ts
from microgrid.engine import units

T0 = datetime(2026, 3, 29, tzinfo=UTC)


def _series(unit: str | None, *values: float | None) -> ts.Series[float]:
    return ts.Series.of(
        ((T0 + timedelta(minutes=i), v) for i, v in enumerate(values)),
        known_until=T0 + timedelta(hours=1),
        unit=unit,
    )


@pytest.mark.parametrize(
    ("unit", "quantity", "value", "expected", "named"),
    [
        ("W", "power", 1500.0, 1.5, "kW"),
        ("kW", "power", 1.5, 1.5, "kW"),
        ("MW", "power", 0.002, 2.0, "kW"),
        ("Wh", "energy", 250.0, 0.25, "kWh"),
        ("MWh", "energy", 1.0, 1000.0, "kWh"),
        ("%", "fraction", 62.0, 0.62, None),
        (None, "fraction", 0.62, 0.62, None),
        ("", "fraction", 0.62, 0.62, None),
        ("USD/kWh", "price", 0.3, 0.3, "USD/kWh"),
        ("EUR/MWh", "price", 85.0, 0.085, "EUR/kWh"),
        ("$/Wh", "price", 0.0003, 0.3, "$/kWh"),
    ],
)
def test_each_unit_converts_to_the_engines(
    unit: str | None, quantity: units.Quantity, value: float, expected: float, named: str | None
) -> None:
    converted = units.normalise(_series(unit, value), quantity)
    assert converted.unit == named
    (sample,) = converted.samples
    assert sample.value == pytest.approx(expected)
    assert units.convert(value, unit, quantity) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("unit", "quantity"),
    [
        ("kWh", "power"),
        (None, "power"),
        ("kW", "energy"),
        ("V", "fraction"),
        ("USD", "price"),
        ("/kWh", "price"),
        ("USD/kW", "price"),
        (None, "price"),
    ],
)
def test_a_unit_that_is_not_the_quantitys_is_refused(
    unit: str | None, quantity: units.Quantity
) -> None:
    with pytest.raises(units.UnitError, match=quantity):
        units.normalise(_series(unit, 1.0), quantity)


def test_no_value_stays_no_value_and_the_series_keeps_its_times() -> None:
    converted = units.normalise(_series("W", 1000.0, None, math.nan, math.inf), "power")
    assert [s.value for s in converted.samples] == [1.0, None, None, None]
    assert converted.known_until == T0 + timedelta(hours=1)
    assert converted.times == _series("W", 1, 2, 3, 4).times


def test_a_percentage_is_divided_exactly() -> None:
    """42 % is 42 / 100, as a client dividing would have it: 0.42, not 0.42000000000000004."""
    assert units.convert(42.0, "%", "fraction") == 42.0 / 100 == 0.42
    assert units.convert(85.0, "EUR/MWh", "price") == 85.0 / 1000
    assert units.FRACTION["%"] == 0.01  # a factor, as POWER_KW and ENERGY_KWH are


finite = st.floats(allow_nan=False, allow_infinity=False, min_value=-1e12, max_value=1e12)


@given(finite)
def test_each_conversion_is_the_exact_one(x: float) -> None:
    """A division only where it is the exact form; a multiplication
    where that is (a per-Wh price by 1000.0; power by its table, as history has)."""
    assert units.convert(x, "%", "fraction") == x / 100
    assert units.convert(x, "USD/Wh", "price") == x * 1000
    assert units.convert(x, "USD/MWh", "price") == x / 1000
    assert units.convert(x, "USD/kWh", "price") == x
    assert units.convert(x, "W", "power") == x * 0.001
    assert units.convert(x, "MWh", "energy") == x * 1000


def test_convert_takes_one_finite_value() -> None:
    with pytest.raises(ValueError, match="not finite"):
        units.convert(math.nan, "W", "power")


def test_a_price_unit_has_one_currency_per_energy_unit() -> None:
    with pytest.raises(units.UnitError):
        units.normalise(_series("USD/kWh/kWh", 1.0), "price")


def test_a_factor_divided_must_have_a_whole_reciprocal() -> None:
    """A factor below 1 is applied as a division by its reciprocal, so a
    factor without a whole one (0.4: 2.5) is refused, not rounded; 1/3's is 3."""
    assert dict(units._whole_reciprocals(0.01, 0.001, 1.0, 1000.0)) == {0.01: 100, 0.001: 1000}
    assert dict(units._whole_reciprocals(1 / 3)) == {1 / 3: 3}
    with pytest.raises(AssertionError, match="no whole reciprocal"):
        units._whole_reciprocals(0.4)
