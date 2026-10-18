"""Core value types."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from microgrid.engine.series import Forecast, Interval, Price, Quantiles
from microgrid.plant._battery import Battery, Grid

T0 = datetime(2026, 9, 23, 3, tzinfo=UTC)


def _point(start: datetime, hours: int = 1) -> Interval[Price]:
    return Interval(start, start + timedelta(hours=hours), Price(0.4, 0.1))


@pytest.mark.parametrize(
    "moment",
    [datetime(2026, 9, 23, 3), datetime(2026, 9, 22, 20, tzinfo=ZoneInfo("America/Los_Angeles"))],
)
def test_core_times_must_be_utc(moment: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        _point(moment)


def test_interval_must_move_forward() -> None:
    with pytest.raises(ValueError, match="ends before"):
        _point(T0, 0)


def test_a_forecast_rejects_overlap() -> None:
    with pytest.raises(ValueError, match="overlap"):
        Forecast((_point(T0, 2), _point(T0 + timedelta(hours=1))), T0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capacity_kwh": 0},
        {"eta_charge": 1.2},
        {"reserve_soc": 0.2, "saturation_soc": 0.1},
    ],
)
def test_battery_validation(kwargs: dict[str, float]) -> None:
    base = {"capacity_kwh": 12.0, "charge_kw": 6.0, "discharge_kw": 6.0}
    with pytest.raises(ValueError):
        Battery(**(base | kwargs))


def test_grid_validation() -> None:
    with pytest.raises(ValueError):
        Grid(export_limit_kw=-1)
    assert Grid().export_limit_kw is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_are_rejected(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        Price(bad, 0.1)
    with pytest.raises(ValueError, match="finite"):
        Quantiles({0.1: 0.0, 0.5: bad, 0.9: 0.0})
    with pytest.raises(ValueError, match="finite"):
        Battery(capacity_kwh=bad, charge_kw=6.0, discharge_kw=6.0)
    with pytest.raises(ValueError, match="finite"):
        Grid(export_limit_kw=bad)


def test_battery_wear_is_finite_and_not_negative() -> None:
    assert Battery(10.0, 5.0, 5.0).wear_per_delivered_kwh == 0.0
    worn = Battery(10.0, 5.0, 5.0, wear_charge_per_kwh=0.02, wear_discharge_per_kwh=0.01)
    assert worn.wear_per_delivered_kwh == pytest.approx(0.02 / 0.94**2 + 0.01)
    for name in ("wear_charge_per_kwh", "wear_discharge_per_kwh"):
        with pytest.raises(ValueError, match="wear"):
            Battery(10.0, 5.0, 5.0, **{name: -0.01})
        with pytest.raises(ValueError, match="finite"):
            Battery(10.0, 5.0, 5.0, **{name: float("nan")})


def test_the_dead_band_and_the_minimum_spread() -> None:
    """The dead band's worked figures (docs/methodology/stored-value.md): at 0.94 each
    way and 0.02 a direction, grid charged at the 0.373 off-peak needs a use worth about
    0.47; PV at the 0.092 export about 0.15."""
    battery = Battery(10.0, 5.0, 5.0, wear_charge_per_kwh=0.02, wear_discharge_per_kwh=0.02)
    assert battery.round_trip == pytest.approx(0.8836)
    assert battery.minimum_spread(0.373) == pytest.approx(0.393 / 0.8836 + 0.02)
    assert round(battery.minimum_spread(0.373), 2) == 0.46  # "about 0.47" with eta_rt 0.88
    assert round(battery.minimum_spread(0.092), 2) == 0.15
    low, high = battery.dead_band(0.30)
    assert (low, high) == pytest.approx((0.94 * 0.30 - 0.02, 0.30 / 0.94 + 0.02))
    assert high - low == pytest.approx(0.30 * (1 / 0.94 - 0.94) + 0.04)  # the band's width
    # No same-price round trip: nothing both charges below and discharges above one price.
    assert low < 0.30 < high


def test_the_inverter_has_a_peak() -> None:
    """With the loss growing with power, discharging has a peak; past it, no."""
    with pytest.raises(ValueError, match="discharge_kw"):
        Battery(10.0, 5.0, 5.0, eta_discharge=0.9, loss_discharge_per_kw=0.1)
    battery = Battery(10.0, 5.0, 1.0, eta_discharge=0.9, loss_discharge_per_kw=0.1)
    peak = 0.9**2 / (4 * 0.1)  # k^2 / 4q
    assert battery.delivered_by(100.0) == pytest.approx(peak)
    for asking in (battery.drawn_by, battery.eta_discharge_at):
        with pytest.raises(ValueError, match="cannot deliver"):
            asking(peak + 0.1)
