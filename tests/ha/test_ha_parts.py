"""The integration's parts on their own: when a valuation is published, the measured
series, the battery's lots against the library's offline ledger, and the recorder read
as recorded history."""

from datetime import UTC, datetime, timedelta

import pytest
from custom_components.microgrid.issuing import Basis, IssuePolicy, LastIssue, Reason, decide
from custom_components.microgrid.lots import SETTLE, LotsTracker
from custom_components.microgrid.recorder_history import RecorderHistory, run_in_recorder
from custom_components.microgrid.telemetry import Telemetry
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from ha_fixtures import REPO
from microgrid.cli.dataset import DatasetHistory, read_dataset
from microgrid.cli.offline import (
    dataset_bindings,
    dataset_lots_async,
    load_setup,
    with_recorded_prices,
)
from microgrid.cli.sources.config import Measured
from microgrid.engine import Assessment, ValuationKind
from microgrid.engine.series import slot_floor

T0 = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def _basis(price: float = 0.3, pv: datetime | None = T0) -> Basis:
    return Basis(pv_issued_at=pv, prices={T0: (price, 0.05)}, load_issued_at=T0)


OK = Assessment(ValuationKind.STORED_VALUE_V1, ())


def test_a_valuation_is_published_on_a_new_basis_or_its_timer() -> None:
    policy = IssuePolicy()
    assert decide(policy, T0, None, _basis(), OK) is Reason.FORECAST_BASIS
    last = LastIssue(T0, T0 + policy.valid_for, _basis(), OK)
    assert decide(policy, T0 + 30 * timedelta(seconds=1), last, _basis(0.4), OK) is None
    assert decide(policy, T0 + 2 * MINUTE, last, _basis(), OK) is None
    assert decide(policy, T0 + 2 * MINUTE, last, _basis(0.4), OK) is Reason.FORECAST_BASIS
    assert decide(policy, T0 + 2 * MINUTE, last, _basis(pv=T0 + MINUTE), OK) is (
        Reason.FORECAST_BASIS
    )
    fallback = Assessment(ValuationKind.FALLBACK_IMPORT, ("battery_soc",))
    assert decide(policy, T0 + 2 * MINUTE, last, _basis(), fallback) is Reason.FORECAST_BASIS
    assert decide(policy, T0 + 10 * MINUTE, last, _basis(), OK) is Reason.LIVE
    assert decide(policy, T0 + 30 * MINUTE, last, _basis(), OK) is Reason.FORECAST_BASIS
    with pytest.raises(ValueError, match="min_interval"):
        IssuePolicy(refresh=timedelta(minutes=40))


def test_measured_states_become_series_in_the_engines_units() -> None:
    measured = Measured(
        soc="sensor.soc",
        meters={"pv": "sensor.pv"},
        house="sensor.house",
        dispatch="select.mode",
        modelled_modes=("Self use",),
    )
    telemetry = Telemetry(measured, {"pv": "pv", "grid": "grid"})
    assert set(telemetry.meters) == {"pv"}  # the grid named no entity
    telemetry.add("sensor.pv", ("1500", "W", T0))
    telemetry.add("sensor.pv", ("unavailable", None, T0 + MINUTE))
    telemetry.add("sensor.soc", ("55", "%", T0))
    telemetry.add("sensor.house", ("0.8", "kW", T0))
    telemetry.add("select.mode", ("Self use", None, T0))
    telemetry.add("select.mode", ("Backup", None, T0 + MINUTE))
    now = T0 + 2 * MINUTE
    pv = telemetry.meters["pv"].series(now)
    assert pv is not None and [s.value for s in pv.samples] == [1.5, None]
    soc = telemetry.soc.series(now) if telemetry.soc is not None else None
    assert soc is not None and soc.samples[0].value == pytest.approx(0.55)
    dispatch = telemetry.dispatch_series(now)
    assert dispatch is not None and [s.value for s in dispatch.samples] == [True, False]
    telemetry.prune(now, timedelta(seconds=30))
    assert [s.value for s in telemetry.meters["pv"].samples] == [None]  # the one in force
    always = Telemetry(Measured(soc="sensor.soc", dispatch_as_modelled=True), {})
    found = always.dispatch_series(now)
    assert found is not None and found.samples[0].value is True


async def test_the_tracker_keeps_the_lots_the_offline_ledger_gives() -> None:
    """Run in over the example dataset, the tracker holds what the library's offline
    ledger holds at the same slot."""
    setup = load_setup(REPO / "examples" / "dataset")
    dataset = with_recorded_prices(read_dataset(REPO / "examples" / "dataset"))
    expected = await dataset_lots_async(setup, dataset)
    assert expected is not None
    slot = setup.engine.config.slot
    first, last = expected.samples[0].at - slot, expected.samples[-1].at
    tracker = LotsTracker(
        DatasetHistory(dataset), dataset_bindings(setup, dataset), slot, run_in=last - first
    )
    await tracker.update(last + SETTLE)
    assert tracker.through == last
    held = tracker.current(last + SETTLE)
    want = expected.samples[-1].value
    assert held is not None and want is not None
    assert {s: round(a.kwh, 9) for s, a in held.held.items()} == {
        s: round(a.kwh, 9) for s, a in want.held.items()
    }
    assert tracker.current(last + 4 * slot) is None  # stale after three slots
    # Due once the slot under way has ended and settled, whatever second a refresh runs.
    assert tracker.due(last + slot) == last
    assert tracker.due(last + slot + SETTLE) == last + slot


async def test_the_recorder_reads_as_recorded_history(
    recorder_mock: object, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(T0)
    hass.states.async_set("sensor.pv", "1000", {"unit_of_measurement": "W"})
    await async_wait_recording_done(hass)
    freezer.move_to(T0 + 5 * MINUTE)
    hass.states.async_set("sensor.pv", "2000", {"unit_of_measurement": "W"})
    await async_wait_recording_done(hass)
    freezer.move_to(T0 + 10 * MINUTE)
    hass.states.async_set("sensor.pv", "unavailable", {"unit_of_measurement": "W"})
    await async_wait_recording_done(hass)
    history = RecorderHistory(hass, {"sensor.pv": "W"})
    start, end = T0 + MINUTE, T0 + 20 * MINUTE
    # As the coordinator reads it: off the event loop, in the recorder's executor.
    power = await run_in_recorder(
        hass, lambda: history.power_series("sensor.pv", start, end, carry_in=timedelta(days=1))
    )
    assert [(s.at, s.value) for s in power.samples] == [
        (start, 1.0),  # the state in force at the start
        (T0 + 5 * MINUTE, 2.0),
        (T0 + 10 * MINUTE, None),
    ]
    assert power.known_until == end
    beats = await history.sample_times("sensor.pv", start, end, carry_in=MINUTE)
    assert beats == (T0 + 5 * MINUTE, T0 + 10 * MINUTE)
    assert slot_floor(T0 + 9 * MINUTE, timedelta(minutes=10)) == T0
