"""The engine's contract, used as a client would: no site TOML, no service.

Only names from ``microgrid.engine`` itself: what a client imports. The tests that
reach into the engine's modules are in ``test_internals.py``.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from engine_fixtures import (
    CONFIG,
    EXPORT,
    IMPORT,
    NOW,
    inputs,
    live_reading,
    load,
    load_forecast,
    load_starts,
    price_forecast,
    pv,
    pv_forecast,
    reading,
    received,
)
from microgrid.engine import (
    ENGINE_CONTRACT,
    MODEL_VERSION,
    Cause,
    Engine,
    LiveValue,
    Regime,
    ValuationKind,
)
from microgrid.engine._api import SOC_TOLERANCE
from microgrid.engine.series import Series


def test_a_nominal_valuation_is_the_price_curve_and_surplus_by_slot() -> None:
    valuation = Engine(CONFIG).value(inputs(), at=NOW)
    assert valuation is not None
    assert (ENGINE_CONTRACT, valuation.model_version) == ("engine-api/4", MODEL_VERSION)
    assert MODEL_VERSION == "pricing/1.3"
    assert valuation.kind is ValuationKind.STORED_VALUE_V1 and valuation.degraded == ()
    assert len(valuation.slots) == 288 and valuation.slots[0].start == datetime(
        2026, 9, 23, 19, 0, tzinfo=UTC
    )
    assert any(slot.surplus_kw > 0 for slot in valuation.slots)
    assert all(slot.marginal_cost >= 0 for slot in valuation.slots)
    assert valuation.inputs[Cause.BATTERY_SOC].as_of == NOW - timedelta(minutes=2)


@pytest.mark.parametrize(
    ("overrides", "cause"),
    [
        ({"pv": None}, Cause.PV_FORECAST),
        ({"pv": pv(polled=NOW - timedelta(hours=21))}, Cause.PV_FORECAST),
        ({"load": None}, Cause.LOAD_FORECAST),
        ({"load": load(kw=None)}, Cause.LOAD_FORECAST),
        ({"soc": reading(None, as_of=NOW - timedelta(hours=1))}, Cause.BATTERY_SOC),
        ({"soc": reading(None)}, Cause.BATTERY_SOC),
        ({"dispatch_as_modelled": reading(False, as_of=NOW)}, Cause.WORK_MODE),
        ({"dispatch_as_modelled": reading(None)}, Cause.WORK_MODE),
    ],
)
def test_an_input_that_cannot_be_used_is_the_fallback(
    overrides: dict[str, object], cause: str
) -> None:
    """tariffkit#67 R-14: the client passes what it has; the engine names what it could
    not use, and every slot costs the import, with no surplus offered."""
    valuation = Engine(CONFIG).value(inputs(**overrides), at=NOW)
    assert valuation is not None
    assert valuation.kind is ValuationKind.FALLBACK_IMPORT and valuation.degraded == (cause,)
    assert all(slot.marginal_cost == slot.p_import == IMPORT for slot in valuation.slots)
    assert all(slot.surplus_kw == 0 and slot.regime is Regime.IMPORT for slot in valuation.slots)
    assert valuation.model is None


def test_a_load_forecast_ends_the_horizon_at_its_first_unknown_slot() -> None:
    """context-v1.5: a published load forecast reaches a fixed span from when it was
    made, so a later valuation reaches past it: the horizon ends there, as at PV's
    coverage (``_covered_prefix``). A slot without a value inside it ends it there too,
    and is named in ``degraded`` (a missing input is named) while the slots before it
    are valued; unknown at the slot under way, it is the fallback (tariffkit#67 R-14)."""
    starts, kw = load_starts(), [0.7] * 288
    short = received(load_forecast(starts[:-3], kw[:-3]))
    valuation = Engine(CONFIG).value(inputs(load=short), at=NOW)
    assert valuation is not None and valuation.kind is ValuationKind.STORED_VALUE_V1
    assert valuation.starts == tuple(starts[:-3]) and valuation.degraded == ()  # an end
    gap = received(load_forecast(starts[:-3], [*kw[:100], None, *kw[101:-3]]))
    cut = Engine(CONFIG).value(inputs(load=gap), at=NOW)
    assert cut is not None and cut.kind is ValuationKind.STORED_VALUE_V1
    # Not a cause: the horizon is cut.
    assert cut.starts == tuple(starts[:100]) and cut.degraded == () and cut.horizon_cut
    assert not valuation.horizon_cut
    head = received(load_forecast(starts, [None, *kw[1:]]))
    fallback = Engine(CONFIG).value(inputs(load=head), at=NOW)
    assert fallback is not None and fallback.degraded == (Cause.LOAD_FORECAST,)
    assert len(fallback.slots) == 288  # the fallback covers the whole horizon
    # A gap past PV's coverage: the issue ends with PV before it, so it is not named.
    sky = pv_forecast()
    short_pv = received(replace(sky, intervals=sky.intervals[:72]))  # to 19:00Z tomorrow
    beyond = received(load_forecast(starts, [*kw[:200], None, *kw[201:]]))
    clipped = Engine(CONFIG).value(inputs(pv=short_pv, load=beyond), at=NOW)
    assert clipped is not None and clipped.kind is ValuationKind.STORED_VALUE_V1
    assert len(clipped.slots) == 144 and clipped.degraded == ()
    # With another input unusable, the issue is the fallback for it, and the gap is not
    # named: the load forecast was usable (``inputs``), and only a cause is named.
    both = Engine(CONFIG).value(inputs(load=gap, soc=None), at=NOW)
    assert both is not None and both.kind is ValuationKind.FALLBACK_IMPORT
    assert both.degraded == (Cause.BATTERY_SOC,) and both.inputs[Cause.LOAD_FORECAST].usable
    assert both.horizon_cut and len(both.slots) == 100
    # A forecast that ended before the horizon starts covers nothing: the fallback.
    past = load(now=NOW - timedelta(days=3))
    gone = Engine(CONFIG).value(inputs(load=past), at=NOW)
    assert gone is not None and gone.degraded == (Cause.LOAD_FORECAST,) and len(gone.slots) == 288


def test_an_unusable_reading_still_reports_when_it_was_known() -> None:
    known = NOW - timedelta(hours=1)
    valuation = Engine(CONFIG).value(inputs(soc=reading(None, as_of=known)), at=NOW)
    assert valuation is not None
    assert valuation.inputs[Cause.BATTERY_SOC].as_of == known
    assert not valuation.inputs[Cause.BATTERY_SOC].usable


def test_no_price_for_the_slot_under_way_is_an_unpriced_valuation() -> None:
    engine = Engine(CONFIG)
    none = engine.value(inputs(prices=None), at=NOW)
    ahead = replace(price_forecast(NOW + timedelta(hours=2)), issued_at=NOW)
    later = engine.value(inputs(prices=received(ahead)), at=NOW)
    for unpriced in (none, later):
        assert unpriced.kind is ValuationKind.UNPRICED and unpriced.slots == ()
        assert unpriced.degraded == (Cause.PRICES,) and not unpriced.inputs[Cause.PRICES].usable
    assert none.inputs[Cause.PRICES].as_of is None
    assert later.inputs[Cause.PRICES].as_of is not None


def test_now_is_taken_to_whole_seconds() -> None:
    valuation = Engine(CONFIG).value(inputs(), at=NOW + timedelta(seconds=7, microseconds=9))
    assert valuation is not None and valuation.now == NOW + timedelta(seconds=7)
    assert valuation.inputs[Cause.PLANT_MODEL].as_of == valuation.now


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValueError, match="UTC"):
        Engine(CONFIG).value(inputs(), at=NOW.replace(tzinfo=None))


def test_the_live_value_runs_the_valuation_again_from_the_meters() -> None:
    engine = Engine(replace(CONFIG, smoothing=timedelta(0)))
    valuation = engine.value(inputs(), at=NOW)
    live = engine.live_session().update(live_reading(), valuation, NOW + timedelta(minutes=30))
    assert live is not None and live.degraded == ()
    assert live.regime is Regime.EXPORT and live.surplus_kw > 4
    assert live.marginal_cost == pytest.approx(EXPORT)


def test_the_live_value_warms_up_before_it_offers_surplus() -> None:
    engine = Engine(CONFIG)
    valuation = engine.value(inputs(), at=NOW)
    session = engine.live_session()
    first = session.update(live_reading(), valuation, NOW + timedelta(minutes=30))
    assert first is not None and first.degraded == (Cause.SMOOTHING,) and first.surplus_kw == 0


def test_a_warm_up_with_an_input_failed_since_it_began_is_the_fallback() -> None:
    """The work mode or the SOC unusable once since the warm-up began ends the warm-up
    that takes the valuation's price, back or not."""
    engine = Engine(CONFIG)
    valuation = engine.value(inputs(), at=NOW)
    until = NOW + timedelta(minutes=30)

    def after(**failed: object) -> LiveValue:
        session = engine.live_session()
        start = NOW + timedelta(seconds=20)
        session.update(live_reading(at=start), valuation, until)
        session.update(live_reading(at=start + timedelta(seconds=30), **failed), valuation, until)
        return session.update(live_reading(at=start + timedelta(seconds=60)), valuation, until)

    steady = after()
    assert steady.degraded == (Cause.SMOOTHING,) and steady.marginal_cost != steady.p_import
    for failed in ({"dispatch_as_modelled": False}, {"soc": None}):
        live = after(**failed)
        assert live.kind is ValuationKind.FALLBACK_IMPORT, failed
        assert live.marginal_cost == live.p_import


def test_the_live_work_mode_is_blamed_only_when_known_wrong_or_unexplained() -> None:
    engine = Engine(replace(CONFIG, smoothing=timedelta(0)))
    valuation = engine.value(inputs(), at=NOW)
    until = NOW + timedelta(minutes=30)
    wrong = engine.live_session().update(live_reading(dispatch_as_modelled=False), valuation, until)
    assert wrong is not None and wrong.degraded == (Cause.WORK_MODE,)
    unexplained = engine.live_session().update(
        live_reading(dispatch_as_modelled=None), valuation, until
    )
    assert unexplained is not None and unexplained.degraded == (Cause.WORK_MODE,)
    # A client's own cause is kept beside the engine's, never in place of them.
    explained = engine.live_session().update(
        live_reading(dispatch_as_modelled=None, external=("telemetry",)), valuation, until
    )
    assert explained.degraded == (Cause.WORK_MODE,) and explained.external == ("telemetry",)
    assert explained.marginal_cost == IMPORT
    assert explained.kind is ValuationKind.FALLBACK_IMPORT
    # Alone, it still makes the value the fallback.
    alone = engine.live_session().update(live_reading(external=("meter",)), valuation, until)
    assert alone.degraded == () and alone.external == ("meter",)
    assert alone.kind is ValuationKind.FALLBACK_IMPORT and alone.marginal_cost == IMPORT


def test_no_valuation_is_an_unpriced_live_value() -> None:
    value = Engine(CONFIG).live_session().update(live_reading(), None, None)
    assert value.kind is ValuationKind.UNPRICED and value.degraded == (Cause.PRICES,)
    assert value.marginal_cost is None and value.surplus_kw == 0


def test_a_live_value_needs_its_valuations_valid_until() -> None:
    """Without it, a valuation of any age would run again as current."""
    engine = Engine(replace(CONFIG, smoothing=timedelta(0)))
    valuation = engine.value(inputs(), at=NOW)
    with pytest.raises(ValueError, match="valid_until"):
        engine.live_session().update(live_reading(), valuation, None)
    with pytest.raises(ValueError, match="UTC"):
        engine.live_session().update(live_reading(), valuation, NOW.replace(tzinfo=None))


def test_a_lapsed_valuation_is_the_fallback() -> None:
    engine = Engine(replace(CONFIG, smoothing=timedelta(0)))
    valuation = engine.value(inputs(), at=NOW)
    late = engine.live_session().update(live_reading(), valuation, NOW)
    assert late is not None and late.degraded == (Cause.ISSUE,) and late.marginal_cost == IMPORT


def test_a_naive_reading_time_is_refused() -> None:
    with pytest.raises(ValueError, match="UTC"):
        Series.of([(NOW.replace(tzinfo=None), 0.4)], known_until=NOW)


def test_an_soc_just_outside_0_1_is_clamped_and_beyond_it_is_unusable() -> None:
    """SOC_TOLERANCE: rounding (100.4 %) is clamped; a percent passed as a fraction is a
    mistake, and the fallback names it rather than valuing a full battery."""
    engine = Engine(CONFIG)

    def slots(soc: float) -> object:
        valuation = engine.value(inputs(soc=reading(soc, as_of=NOW)), at=NOW)
        assert valuation is not None
        return valuation.kind, valuation.slots

    assert slots(1.0 + SOC_TOLERANCE / 2) == slots(1.0)
    assert slots(-SOC_TOLERANCE / 2) == slots(0.0)
    wrong = engine.value(inputs(soc=reading(85.0, as_of=NOW)), at=NOW)
    assert wrong is not None and wrong.degraded == (Cause.BATTERY_SOC,)
    assert (
        not wrong.inputs[Cause.BATTERY_SOC].usable and wrong.inputs[Cause.BATTERY_SOC].as_of == NOW
    )

    session = Engine(replace(CONFIG, smoothing=timedelta(0)))
    valuation = session.value(inputs(), at=NOW)
    until = NOW + timedelta(minutes=30)
    rounded = session.live_session().update(live_reading(soc=1.004), valuation, until)
    assert rounded == session.live_session().update(live_reading(soc=1.0), valuation, until)
    mistaken = session.live_session().update(live_reading(soc=85.0), valuation, until)
    assert mistaken is not None and mistaken.degraded == (Cause.BATTERY_SOC,)


def test_results_are_read_only() -> None:
    """A frozen result holds none of the engine's own dicts."""
    engine = Engine(replace(CONFIG, smoothing=timedelta(0)))
    valuation = engine.value(inputs(), at=NOW)
    assert valuation is not None
    with pytest.raises(TypeError):
        valuation.inputs[Cause.BATTERY_SOC] = valuation.inputs[Cause.BATTERY_SOC]  # type: ignore[index]


def test_a_warm_up_on_a_valuation_without_lots_names_them() -> None:
    """The warm-up publishes the valuation's prices, so its missing lots with them."""
    engine = Engine(CONFIG)
    valuation = engine.value(inputs(lots=None), at=NOW)
    assert valuation is not None and Cause.BATTERY_LOTS in valuation.degraded
    first = engine.live_session().update(live_reading(), valuation, NOW + timedelta(minutes=30))
    assert first is not None and first.degraded == (Cause.SMOOTHING, Cause.BATTERY_LOTS)
