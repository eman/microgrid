"""Whether a valuation can rest on its inputs (R-7)."""

from datetime import UTC, datetime

from microgrid.engine import Assessment, Cause, InputState, ValuationKind
from microgrid.engine._assess import assess

T0 = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
NOMINAL = Assessment(ValuationKind.STORED_VALUE_V1, ())


def test_assess_names_every_unusable_input() -> None:
    ok = InputState(as_of=T0, usable=True)
    bad = InputState(as_of=None, usable=False)
    assert assess({Cause.PRICES: ok, Cause.PV_FORECAST: ok}) == NOMINAL
    assert assess({Cause.PV_FORECAST: bad, Cause.BATTERY_SOC: bad, Cause.PRICES: ok}) == Assessment(
        ValuationKind.FALLBACK_IMPORT, ("battery_soc", "pv_forecast")
    )
