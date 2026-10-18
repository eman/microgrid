"""``engine/2``: the engine's configuration as plain data."""

from dataclasses import replace
from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from engine_fixtures import CONFIG, NOW, inputs, live_reading, reading
from microgrid.engine import (
    ENGINE_SCHEMA,
    Engine,
    EngineConfig,
    EngineConfigError,
    Pricing,
)
from microgrid.engine._api import LOAD_JUMP_KW, PRICE_MAX_AGE, SOC_TOLERANCE
from microgrid.engine._live import importing_now
from microgrid.engine.extend import OBSERVED, Noise

PLANT = CONFIG.plant
TODAY = {
    "schema": "engine/2",
    "scenarios": 10,
    "battery_value": "stored_cost",
    "slot_minutes": 10.0,
    "horizon_hours": 48.0,
    "pv_max_age_hours": 20.0,
    "load_max_age_minutes": 90.0,
    "price_max_age_minutes": 1560.0,
    "smoothing_seconds": 300.0,
    "soc_tolerance": 0.01,
    "load_jump_kw": 0.5,
    "observed": {
        "spill_kw": 0.12,
        "short_kw": 0.12,
        "charge_kw": 0.12,
        "topup_kw": 0.5,
        "need_kw": 0.12,
        "soc": 0.005,
        "regulation_kw": 0.3,
    },
}


def test_every_field_defaults_to_how_the_engine_runs_today() -> None:
    assert ENGINE_SCHEMA == "engine/2"
    assert EngineConfig.from_mapping({"schema": "engine/2"}, plant=PLANT) == EngineConfig(PLANT)
    assert EngineConfig(PLANT).to_mapping() == TODAY
    defaults = EngineConfig(PLANT)
    assert (defaults.observed, defaults.soc_tolerance, defaults.load_jump_kw) == (
        OBSERVED,
        SOC_TOLERANCE,
        LOAD_JUMP_KW,
    )


def test_a_mapping_reads_back_as_written() -> None:
    config = EngineConfig(
        PLANT,
        pricing=Pricing(3),
        slot=timedelta(minutes=5),
        horizon=timedelta(hours=24),
        pv_max_age=timedelta(hours=6),
        load_max_age=timedelta(minutes=45),
        price_max_age=timedelta(minutes=60),
        smoothing=timedelta(0),
        observed=replace(OBSERVED, topup_kw=0.8),
        soc_tolerance=0.02,
        load_jump_kw=1.0,
    )
    assert EngineConfig.from_mapping(config.to_mapping(), plant=PLANT) == config


def test_a_floor_left_out_keeps_its_measured_value() -> None:
    read = EngineConfig.from_mapping(
        {"schema": "engine/2", "observed": {"topup_kw": 0.8}}, plant=PLANT
    )
    assert read.observed == replace(OBSERVED, topup_kw=0.8)


def test_every_problem_is_named_with_its_path() -> None:
    with pytest.raises(EngineConfigError) as raised:
        EngineConfig.from_mapping(
            {
                "schema": "engine/3",
                "slot_minutes": 7,
                "scenarios": 0,
                "horizon_hours": 0,
                "pv_max_age_hours": "20",
                "load_max_age_minutes": True,
                "smoothing_seconds": -1,
                "soc_tolerance": float("nan"),
                "load_jump_kw": -0.5,
                "observed": {"spill_kw": -1, "noise_kw": 0.1, "soc": 1.0},
            },
            plant=PLANT,
        )
    assert raised.value.errors == (
        "schema: must be 'engine/2' (or the older 'engine/1')",
        "scenarios: must be in [1, 50]",
        "slot_minutes: must divide an hour",
        "horizon_hours: must be more than 0",
        "pv_max_age_hours: must be a number",
        "load_max_age_minutes: must be a number",
        "smoothing_seconds: must be 0 or more",
        "soc_tolerance: must be a number",
        "load_jump_kw: must be 0 or more",
        "observed.spill_kw: must be 0 or more",
        "observed.noise_kw: is not a field",
        "observed.soc: must be under 1 (a fraction of the battery)",
    )
    with pytest.raises(EngineConfigError, match="must be a table"):
        EngineConfig.from_mapping(["engine/2"], plant=PLANT)


def test_too_large_too_small_and_null_are_named() -> None:
    """Overflow, a duration under a microsecond, a whole-battery SOC tolerance and a null
    other than the price age are each a problem, not a crash or a silent default."""
    with pytest.raises(EngineConfigError) as raised:
        EngineConfig.from_mapping(
            {
                "schema": "engine/2",
                "horizon_hours": 1e20,
                "pv_max_age_hours": 10**400,
                "load_max_age_minutes": 1e-12,
                "soc_tolerance": 5,
                "scenarios": None,
                "smoothing_seconds": None,
                "observed": None,
            },
            plant=PLANT,
        )
    assert raised.value.errors == (
        "scenarios: must be a whole number",
        "horizon_hours: is too large",
        "pv_max_age_hours: must be a number",
        "load_max_age_minutes: must be at least a microsecond",
        "smoothing_seconds: must be a number",
        "soc_tolerance: must be under 1 (a fraction of the battery)",
        "observed: must be a table",
    )


def test_engine_1_writes_every_field_of_the_configuration() -> None:
    """A field added to ``EngineConfig`` and not to ``engine/2`` fails."""
    from dataclasses import fields

    from microgrid.engine._engine_config import FIELDS

    assert {f.name for f in fields(EngineConfig)} - {"plant"} == FIELDS


def test_the_slot_divides_an_hour() -> None:
    for minutes in (5, 15, 30, 60):
        read = EngineConfig.from_mapping(
            {"schema": "engine/2", "slot_minutes": minutes}, plant=PLANT
        )
        assert read.slot == timedelta(minutes=minutes)
    with pytest.raises(ValueError, match="divide an hour"):
        EngineConfig(PLANT, slot=timedelta(minutes=7))


def test_no_price_age_limit_is_null() -> None:
    read = EngineConfig.from_mapping(
        {"schema": "engine/2", "price_max_age_minutes": None}, plant=PLANT
    )
    assert read.price_max_age is None


def test_the_soc_tolerance_is_the_configured_one() -> None:
    """An SOC of 1.04 is a mistake at today's 0.01, and rounding at 0.05."""

    def slots(engine: Engine, soc: float) -> object:
        valuation = engine.value(inputs(soc=reading(soc, as_of=NOW)), at=NOW)
        assert valuation is not None
        return valuation.kind, valuation.slots, valuation.degraded

    today, loose = Engine(CONFIG), Engine(replace(CONFIG, soc_tolerance=0.05))
    assert slots(today, 1.04) != slots(today, 1.0)
    assert slots(loose, 1.04) == slots(loose, 1.0)


def test_the_load_jump_is_the_configured_one() -> None:
    """A 0.8 kW step seen once is held back at today's 0.5 kW and taken at 1 kW: taken,
    it weighs the 30 s it held."""

    def after_one_step(jump_kw: float) -> float:
        engine = Engine(replace(CONFIG, load_jump_kw=jump_kw))
        valuation = engine.value(inputs(), at=NOW)
        session = engine.live_session()
        until = NOW + timedelta(minutes=30)
        start = NOW + timedelta(seconds=5)
        for n in range(12):
            reading = live_reading(at=start + timedelta(seconds=30 * n), load_kw=0.7)
            session.update(reading, valuation, until)
        for n, kw in enumerate((1.5, 0.7)):  # a blip, back 30 s later
            reading = live_reading(at=start + timedelta(seconds=360 + 30 * n), load_kw=kw)
            live = session.update(reading, valuation, until)
        assert live is not None and live.degraded == ()
        return live.surplus_kw

    steady = after_one_step(100.0)  # any step taken: the baseline moves
    assert after_one_step(LOAD_JUMP_KW) > steady + 0.05  # held: the blip did not count
    assert after_one_step(1.0) == pytest.approx(steady)


def test_the_live_import_reads_the_configured_floors() -> None:
    """0.2 kW from the grid with the battery able to serve is regulation at today's
    0.3 kW floor, and the house importing at 0.1 kW."""
    meters = {"pv": 0.0, "grid": 0.2, "battery": 0.0}
    assert not importing_now(PLANT, 0.6, now=NOW, meters=meters)
    tight = replace(OBSERVED, regulation_kw=0.1)
    assert importing_now(PLANT, 0.6, now=NOW, meters=meters, noise=tight)


_floors = st.fixed_dictionaries(
    {},
    optional={
        **{
            name: st.floats(0, 10, allow_nan=False)
            for name in ("spill_kw", "short_kw", "charge_kw", "topup_kw", "need_kw")
        },
        "soc": st.floats(0, 0.99, allow_nan=False),
    },
)


@given(
    scenarios=st.integers(1, 50),
    hours=st.floats(0.5, 96, allow_nan=False),
    smoothing=st.floats(0, 3600, allow_nan=False),
    price=st.none() | st.floats(1, 600, allow_nan=False),
    floors=_floors,
)
def test_any_valid_mapping_round_trips(
    scenarios: int, hours: float, smoothing: float, price: float | None, floors: dict[str, float]
) -> None:
    mapping = {
        "schema": "engine/2",
        "scenarios": scenarios,
        "horizon_hours": hours,
        "smoothing_seconds": smoothing,
        "price_max_age_minutes": price,
        "observed": floors,
    }
    config = EngineConfig.from_mapping(mapping, plant=PLANT)
    assert EngineConfig.from_mapping(config.to_mapping(), plant=PLANT) == config
    assert isinstance(config.observed, Noise)


def test_the_soc_tolerance_reaches_the_series_and_the_live_value() -> None:
    """``Engine.value(inputs, at=)`` and a live session read the configured tolerance
    too, not the constant."""
    from microgrid.engine import series as ts
    from test_inputs import NOW as AT
    from test_inputs import _config, _inputs

    def degraded(config: EngineConfig, soc: float) -> tuple[str, ...]:
        inputs = _inputs(soc=ts.Series.of([(AT, soc)], known_until=AT))
        valuation = Engine(config).value(inputs, at=AT)
        assert valuation is not None
        return valuation.degraded

    today = _config()
    assert degraded(today, 1.04) == ("battery_soc",)
    assert degraded(replace(today, soc_tolerance=0.05), 1.04) == ()

    def live(config: EngineConfig) -> object:
        engine = Engine(replace(config, smoothing=timedelta(0)))
        valuation = engine.value(inputs(), at=NOW)
        reading = live_reading(soc=1.04)
        value = engine.live_session().update(reading, valuation, NOW + timedelta(minutes=30))
        assert value is not None
        return value.degraded

    assert live(CONFIG) != live(replace(CONFIG, soc_tolerance=0.05))
    assert live(replace(CONFIG, soc_tolerance=0.05)) == ()


def test_a_held_load_jump_is_confirmed_by_the_configured_step() -> None:
    """The second reading confirms a held jump within the configured step. 2.0 kW after
    0.7 is held at either step; 2.8 kW next is 0.8 kW from the held 2.0: confirmed at
    1 kW, held again at today's 0.5 kW."""

    def after(jump_kw: float) -> float:
        engine = Engine(replace(CONFIG, load_jump_kw=jump_kw))
        valuation = engine.value(inputs(), at=NOW)
        session = engine.live_session()
        until = NOW + timedelta(minutes=30)
        start = NOW + timedelta(seconds=5)
        for n in range(12):
            reading = live_reading(at=start + timedelta(seconds=30 * n), load_kw=0.7)
            session.update(reading, valuation, until)
        for n, kw in enumerate((2.0, 2.8, 2.8)):
            reading = live_reading(at=start + timedelta(seconds=360 + 30 * n), load_kw=kw)
            live = session.update(reading, valuation, until)
        assert live is not None and live.degraded == ()
        return live.surplus_kw

    assert after(1.0) < after(LOAD_JUMP_KW) - 0.05  # taken: more load, less surplus


def test_the_live_session_reads_the_configured_floors() -> None:
    """The live session passes ``observed`` to its import check. With PV gone and the
    battery able to serve, 0.2 kW from the grid is regulation at today's 0.3 kW floor
    and the house importing at 0.1 kW."""

    def live(config: EngineConfig) -> object:
        engine = Engine(replace(config, smoothing=timedelta(0)))
        valuation = engine.value(inputs(), at=NOW)
        reading = live_reading(pv_kw=0.0, grid_kw=0.2, battery_kw=0.0, soc=0.6, load_kw=0.2)
        value = engine.live_session().update(reading, valuation, NOW + timedelta(minutes=30))
        assert value is not None
        return value.regime, value.marginal_cost

    tight = replace(CONFIG, observed=replace(OBSERVED, regulation_kw=0.1))
    assert live(CONFIG) != live(tight)


def test_the_battery_value_rule_is_read_and_refused_by_name() -> None:
    """``battery_value``: the stored cost by default, the make-up rule by name."""
    assert EngineConfig.from_mapping(
        {"schema": "engine/2", "battery_value": "replacement"}, plant=PLANT
    ).pricing == Pricing(battery_value="replacement")
    with pytest.raises(EngineConfigError, match="battery_value: must be one of"):
        EngineConfig.from_mapping({"schema": "engine/2", "battery_value": "cheapest"}, plant=PLANT)


def test_an_engine_1_file_is_read_as_it_was() -> None:
    """``engine/2`` defaults the prices' age limit; an ``engine/1`` file
    without one still means no limit, and with one, its own."""
    assert (
        EngineConfig.from_mapping({"schema": "engine/2"}, plant=PLANT).price_max_age
        == PRICE_MAX_AGE
    )
    assert EngineConfig.from_mapping({"schema": "engine/1"}, plant=PLANT).price_max_age is None
    given = {"schema": "engine/1", "price_max_age_minutes": 60}
    assert EngineConfig.from_mapping(given, plant=PLANT).price_max_age == timedelta(minutes=60)
    with pytest.raises(EngineConfigError, match="schema"):
        EngineConfig.from_mapping({"schema": "engine/3"}, plant=PLANT)
