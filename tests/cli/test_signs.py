"""Each meter's sign, told from a synthetic day's records: PV, a house, a battery that
fills at noon and empties in the evening, and the grid closing the balance."""

import math

from microgrid.cli.signs import meter_signs
from microgrid.plant import Meter

HOURS = 1.0
METERS = (Meter("pv", "pv"), Meter("grid", "grid"), Meter("battery", "battery"))


def _day() -> tuple[dict[str, list[float | None]], list[float | None], list[float | None]]:
    """Hourly means over two days, in the plant's convention (grid positive importing,
    battery positive charging), the SOC (a fraction) and the house."""
    pv: list[float | None] = []
    house: list[float | None] = []
    battery: list[float | None] = []
    grid: list[float | None] = []
    soc: list[float | None] = []
    stored = 0.3
    for hour in range(48):
        h = hour % 24
        made = max(0.0, 4.0 * math.sin(math.pi * (h - 6) / 12)) if 6 <= h <= 18 else 0.0
        drew = 0.6 + (1.5 if 18 <= h < 21 else 0.0)
        charge = 0.0
        if made > drew and stored < 0.95:
            charge = min(made - drew, 3.0)
        elif made < drew and h >= 17 and stored > 0.2:
            charge = -min(drew - made, 2.0)
        stored = min(1.0, max(0.0, stored + charge / 10.0))
        pv.append(made)
        house.append(drew)
        battery.append(charge)
        grid.append(drew + charge - made)
        soc.append(stored)
    return {"pv": pv, "grid": grid, "battery": battery}, soc, house


def _flip(values: list[float | None]) -> list[float | None]:
    return [None if v is None else -v for v in values]


def test_meters_in_the_plants_convention_keep_their_signs() -> None:
    readings, soc, house = _day()
    signs = meter_signs(METERS, readings, hours=HOURS, soc=soc, house=house)
    assert signs.found == {"pv": 1.0, "battery": 1.0, "grid": 1.0}
    assert signs.notes == ()


def test_a_meter_read_backwards_is_found() -> None:
    readings, soc, house = _day()
    readings["grid"] = _flip(readings["grid"])
    readings["battery"] = _flip(readings["battery"])  # positive discharging, as HA's
    signs = meter_signs(METERS, readings, hours=HOURS, soc=soc, house=house)
    assert signs.found == {"pv": 1.0, "battery": -1.0, "grid": -1.0}
    assert any("grid" in note for note in signs.notes)


def test_the_grid_is_told_without_a_house_meter() -> None:
    readings, soc, _ = _day()
    readings["grid"] = _flip(readings["grid"])
    signs = meter_signs(METERS, readings, hours=HOURS, soc=soc)
    assert signs.found["grid"] == -1.0


def test_a_battery_that_held_still_is_left_undecided() -> None:
    readings, _, house = _day()
    soc: list[float | None] = [0.5] * len(readings["pv"])
    signs = meter_signs(METERS, readings, hours=HOURS, soc=soc, house=house)
    assert "battery" not in signs.found
    assert any("battery" in note and "too little" in note for note in signs.notes)


def test_too_little_recorded_says_nothing() -> None:
    quiet: dict[str, list[float | None]] = {"pv": [0.0] * 24, "grid": [None] * 24}
    signs = meter_signs(METERS, quiet, hours=HOURS)
    assert signs.found == {}
    assert len(signs.notes) == 2
