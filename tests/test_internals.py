"""The engine's own modules under test: what a client cannot reach.

Defensive fallbacks forced by patching the model's steps, and the surplus's
monotonicity in load on the live value's revalue. On the example site's plant, with its
inverter draw and conversion loss, so the steps the property guards are exercised.
"""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine_fixtures import (
    CONFIG,
    EXAMPLE_PLANT,
    EXPORT,
    NOW,
    inputs,
    live_reading,
    plant_of,
    reading,
)
from microgrid.engine import Cause, Engine, Regime
from microgrid.engine import _api as engine_api
from microgrid.engine._live import LiveSurplus, LiveValuation, revalue
from microgrid.engine.extend import forecast_surplus
from microgrid.engine.series import slot_floor

EXAMPLE = replace(CONFIG, plant=plant_of(EXAMPLE_PLANT))


def test_a_model_that_did_not_run_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: a surplus the plant model could not finish is no model
    (R-7)."""
    real = forecast_surplus

    def unfinished(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        return replace(result, surplus_kw=(None, *result.surplus_kw[1:]))

    monkeypatch.setattr(engine_api, "forecast_surplus", unfinished)
    valuation = Engine(EXAMPLE).value(inputs(), at=NOW)
    assert valuation is not None and valuation.model is None
    assert Cause.PLANT_MODEL in valuation.degraded
    assert all(slot.marginal_cost == slot.p_import for slot in valuation.slots)


def test_surplus_whose_worth_is_unknown_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never "surplus at the import price"."""
    real = revalue

    def unpriced(*args: Any, **kwargs: Any) -> LiveValuation:
        found = real(*args, **kwargs)
        surplus = LiveSurplus(kw=0.19, regime=Regime.EXPORT, value=None)
        return LiveValuation(energy=found.energy, surplus=surplus)

    monkeypatch.setattr(engine_api, "revalue", unpriced)
    engine = Engine(replace(EXAMPLE, smoothing=timedelta(0)))
    valuation = engine.value(inputs(), at=NOW)
    assert valuation is not None
    live = engine.live_session().update(live_reading(), valuation, NOW + timedelta(minutes=30))
    assert live is not None and live.surplus_kw == 0
    # The price is the measured slot's all the same: PV spilling, at the export.
    assert live.marginal_cost == pytest.approx(EXPORT)
    # The regime is the valuation's for the slot, not the unpriced export.
    slot = valuation.slots[valuation.starts.index(slot_floor(live.at))]
    assert live.regime is slot.regime


_HIGH_SOC = Engine(EXAMPLE).value(inputs(soc=reading(0.95, as_of=NOW)), at=NOW)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.floats(0, 12), st.floats(0, 4), st.floats(0, 5), st.floats(0, 1))
def test_more_load_never_raises_surplus(pv: float, load: float, draw: float, soc: float) -> None:
    """Surplus with a load's draw added is at most what it is without it."""
    assert _HIGH_SOC is not None and _HIGH_SOC.model is not None
    model = _HIGH_SOC.model
    index = _HIGH_SOC.starts.index(slot_floor(NOW))

    def found(load_now: float) -> float:
        return revalue(
            model, index, NOW, measured_pv_kw=pv, measured_load_kw=load_now, soc=soc
        ).surplus.kw

    assert found(load) >= found(load + draw) - 1e-9
