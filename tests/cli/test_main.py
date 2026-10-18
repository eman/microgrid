"""``microgrid``: the library run on a dataset."""

import argparse
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from microgrid.cli import offline
from microgrid.cli.dataset import (
    HEARTBEAT,
    LOAD_FORECAST,
    PRICES,
    DatasetError,
    DatasetHistory,
    Entry,
    read_dataset,
    write_dataset,
)
from microgrid.cli.history import Limits
from microgrid.cli.inputs import NO_DC_FORECAST, inputs
from microgrid.cli.main import duration, main, moment
from microgrid.cli.offline import (
    TELEMETRY,
    _floor,
    _vouched,
    dataset_bindings,
    engine_inputs,
    live,
    load_setup,
    value,
)
from microgrid.cli.toml_text import dumps
from microgrid.engine import Cause, EngineConfigError
from microgrid.engine.series import Series, alive

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "dataset"
BERLIN = ZoneInfo("Europe/Berlin")
HOUR = timedelta(hours=1)


def test_value_prints_the_valuation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["value", "--data", str(EXAMPLE), "--at", "2026-10-22T13:03", "--slots", "3"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("valued at 2026-10-22 13:03 CEST: stored_value_v1,")
    assert lines[0].endswith("degraded: none")
    assert lines[2].startswith("2026-10-22 13:00 CEST") and len(lines) == 5


def test_value_without_prices_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["value", "--data", str(EXAMPLE), "--at", "2026-10-20T12:00Z"]) == 1
    assert "no valuation" in capsys.readouterr().out


def test_live_across_the_dst_change(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["live", "--data", str(EXAMPLE), "--from", "2026-10-25T01:50", "--to"]
    assert main([*argv, "2026-10-25T03:10+01:00", "--every", "10m"]) == 0
    lines = capsys.readouterr().out.splitlines()[1:]
    # 01:50 CEST to 03:10 CET is two and a third hours: 14 readings, 02:00-02:50 twice.
    assert len(lines) == 14
    assert [line[:25].strip() for line in lines[1:3]] == [
        "2026-10-25 02:00:00 CEST",
        "2026-10-25 02:10:00 CEST",
    ]
    assert sum("02:00:00 CET " in line for line in lines) == 1
    # The dataset's readings before --from are read: no warm-up at the start.
    assert not lines[0].rstrip().endswith("smoothing")


def test_the_live_value_is_unknown_while_the_heartbeat_is_silent(tmp_path: Path) -> None:
    dataset = read_dataset(EXAMPLE)
    entries = dict(dataset.entries)
    beats = entries[HEARTBEAT].series
    gap = (datetime(2026, 10, 22, 12, 0, tzinfo=UTC), datetime(2026, 10, 22, 12, 30, tzinfo=UTC))
    kept = tuple(s for s in beats.samples if not gap[0] <= s.at < gap[1])
    entries[HEARTBEAT] = Entry("number", Series(kept, beats.known_until))
    write_dataset(tmp_path, entries)
    for name in ("plant.toml", "engine.toml"):
        (tmp_path / name).write_bytes((EXAMPLE / name).read_bytes())
    found = dict(
        live(
            load_setup(tmp_path),
            read_dataset(tmp_path),
            datetime(2026, 10, 22, 11, 0, tzinfo=UTC),
            datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            every=timedelta(minutes=10),
            issue_every=timedelta(minutes=5),
            valid_for=timedelta(minutes=30),
            limits=Limits(telemetry=timedelta(minutes=2)),
        )
    )
    silent = found[datetime(2026, 10, 22, 12, 10, tzinfo=UTC)]
    # The client's cause beside the engine's, which name what it could not read.
    assert silent.external == (TELEMETRY,) and Cause.PV_POWER in silent.degraded
    # Telemetry silent: the import price and no surplus (tariffkit#67 R-14).
    assert silent.marginal_cost == silent.p_import and silent.surplus_kw == 0.0
    alive = found[datetime(2026, 10, 22, 11, 50, tzinfo=UTC)]
    assert alive is not None and alive.degraded == ()


def test_the_engine_file_is_the_datasets_unless_named(tmp_path: Path) -> None:
    assert load_setup(EXAMPLE).engine.config.price_max_age == timedelta(minutes=60)
    other = tmp_path / "engine.toml"
    other.write_text('schema = "engine/1"\nprice_max_age_minutes = 5\n', encoding="utf-8")
    assert load_setup(EXAMPLE, engine=other).engine.config.price_max_age == timedelta(minutes=5)
    (tmp_path / "plant.toml").write_bytes((EXAMPLE / "plant.toml").read_bytes())
    (tmp_path / "engine.toml").unlink()
    assert load_setup(tmp_path).engine.config.price_max_age == timedelta(hours=26)  # the default


def test_a_bad_configuration_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = tmp_path / "engine.toml"
    engine.write_text('schema = "engine/1"\nhorizon_hours = -1\n', encoding="utf-8")
    assert (
        main(["value", "--data", str(EXAMPLE), "--engine", str(engine), "--at", "2026-10-22T12:00"])
        == 1
    )
    assert capsys.readouterr().err.startswith("error: ")


def test_durations_and_moments() -> None:
    assert duration("30s") == timedelta(seconds=30) and duration("1.5h") == timedelta(hours=1.5)
    for bad in ("0s", "5", "-1m", "1w"):
        with pytest.raises(argparse.ArgumentTypeError):
            duration(bad)
    assert moment("2026-10-22T12:00", BERLIN) == datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    assert moment("2026-10-22T12:00Z", BERLIN) == datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def test_config_check_and_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "check", "--data", str(EXAMPLE)]) == 0
    assert "ok" in capsys.readouterr().out
    assert main(["schema", "--write", str(tmp_path)]) == 0
    assert (tmp_path / "valuation-2.schema.json").exists()


def test_a_moment_the_clocks_skip_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    # 2026-10-25 02:30 in Berlin happens twice: the first (CEST); 2027-03-28 02:30 never.
    assert moment("2026-10-25T02:30", BERLIN) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert main(["value", "--data", str(EXAMPLE), "--at", "2027-03-28T02:30"]) == 1
    assert "does not exist in Europe/Berlin" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["live", "--data", str(EXAMPLE), "--from", "2026-10-22T12:00", "--to", "2026-10-22T11:00"],
        [
            "value",
            "--data",
            str(EXAMPLE),
            "--engine",
            "/nonexistent.toml",
            "--at",
            "2026-10-22T12:00",
        ],
    ],
)
def test_a_command_that_cannot_run_says_why(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_an_engine_file_that_is_not_toml_is_named(tmp_path: Path) -> None:
    engine = tmp_path / "engine.toml"
    engine.write_text("schema = \n", encoding="utf-8")
    with pytest.raises(EngineConfigError, match="is not TOML"):
        load_setup(EXAMPLE, engine=engine)


def _truncated(tmp_path: Path, at: datetime) -> Path:
    """The example dataset as known at ``at``: nothing received after it."""
    entries = {}
    for name, entry in read_dataset(EXAMPLE).entries.items():
        series = entry.series
        kept = tuple(s for s in series.samples if s.at <= at)
        entries[name] = Entry(entry.kind, Series(kept, at, series.unit), entry.forecasts)
    write_dataset(tmp_path, entries)
    for name in ("plant.toml", "engine.toml"):
        (tmp_path / name).write_bytes((EXAMPLE / name).read_bytes())
    return tmp_path


def test_value_reads_only_what_was_received_by_then(tmp_path: Path) -> None:
    at = datetime(2026, 10, 22, 11, 3, tzinfo=UTC)
    whole = value(load_setup(EXAMPLE), read_dataset(EXAMPLE), at)
    known = _truncated(tmp_path, at)
    then = value(load_setup(known), read_dataset(known), at)
    assert whole is not None and then is not None
    assert whole.slots == then.slots and whole.degraded == then.degraded


def test_live_keeps_the_last_valuation_when_none_comes(tmp_path: Path) -> None:
    # The last prices fetched 11:00:05: valuations past their hour (from 12:05) come to
    # nothing, and the last one (12:00) stands, as the service keeps its last issue,
    # until it expires (12:30).
    known = _truncated(tmp_path, datetime(2026, 10, 22, 11, 1, tzinfo=UTC))
    entries = dict(read_dataset(EXAMPLE).entries)
    entries[PRICES] = read_dataset(known).entries[PRICES]
    write_dataset(known, entries)
    found = dict(
        live(
            load_setup(known),
            read_dataset(known),
            datetime(2026, 10, 22, 11, 30, tzinfo=UTC),
            datetime(2026, 10, 22, 13, 0, tzinfo=UTC),
            every=timedelta(minutes=10),
            issue_every=timedelta(minutes=5),
            valid_for=timedelta(minutes=30),
            limits=Limits(telemetry=timedelta(minutes=3)),
        )
    )
    first = found[datetime(2026, 10, 22, 11, 30, tzinfo=UTC)]
    assert first is not None  # valued from prices 30 minutes old
    held = found[datetime(2026, 10, 22, 12, 20, tzinfo=UTC)]
    assert held is not None and "issue" not in held.degraded  # the 12:00 one, still valid
    later = found[datetime(2026, 10, 22, 12, 40, tzinfo=UTC)]
    assert later is not None and "issue" in later.degraded  # the last one, expired


def test_pv_and_the_house_load_are_read_at_zero_at_least() -> None:
    at = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)
    floored = _floor(
        Series.of([(at, -0.2), (at + HOUR, None), (at + 2 * HOUR, 1.5)], known_until=at + 2 * HOUR)
    )
    assert [s.value for s in floored.samples] == [0.0, None, 1.5]


def test_a_silent_heartbeat_is_no_value_until_it_beats_again() -> None:
    at = datetime(2026, 10, 22, 2, 0, tzinfo=UTC)
    minute = timedelta(minutes=1)
    meter = Series.of([(at - minute, 2.0), (at + 2 * minute, 3.0)], known_until=at + HOUR)
    beats: Series[None] = Series.of([(at, None), (at + 10 * minute, None)], known_until=at + HOUR)
    vouched = _vouched(meter, alive(beats, 3 * minute))
    assert [(s.at - at, s.value) for s in vouched.samples] == [
        (timedelta(0), 2.0),  # from the first beat
        (2 * minute, 3.0),
        (3 * minute, None),  # silent from the limit on
        (10 * minute, 3.0),  # beating again: the value held
        (13 * minute, None),
    ]


def test_a_dc_bus_load_without_a_forecast_takes_none_as_the_service_does(tmp_path: Path) -> None:
    mapping = tomllib.loads((EXAMPLE / "plant.toml").read_text(encoding="utf-8"))
    # A folded battery sharing its DC bus takes its own DC limits and the hybrid's rating.
    mapping["battery"] |= {"dc_charge_kw": 6.0, "dc_discharge_kw": 6.0}
    mapping["hybrid"] |= {"ac_rating_kw": 6.0}
    mapping["loads"] = [
        {"name": "house", "connection": "ac"},
        {"name": "ev", "connection": "dc_bus"},
    ]
    plant = tmp_path / "plant.toml"
    plant.write_text(dumps(mapping), encoding="utf-8")
    setup = load_setup(EXAMPLE, plant=plant)
    assert setup.plant.has_dc_loads
    at = datetime(2026, 10, 22, 11, 3, tzinfo=UTC)
    read = engine_inputs(setup, inputs(read_dataset(EXAMPLE), has_dc_loads=True), at)
    assert read.dc_load is not None
    forecast = read.dc_load.samples[0].value
    assert forecast is not None and forecast.source == NO_DC_FORECAST
    assert {i.value for i in forecast.intervals} == {0.0}


def test_live_revalues_on_a_fixed_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the first reading in each 5 minutes from the start, whatever the readings'
    period: 0, 6, 10, 16, 20 minutes for readings every 2."""
    valued: list[datetime] = []
    real = offline.engine_inputs

    def spy(setup: Any, read: Any, at: datetime, lots: Any = None) -> Any:
        valued.append(at)
        return real(setup, read, at, lots)

    monkeypatch.setattr(offline, "engine_inputs", spy)
    start = datetime(2026, 10, 22, 11, 0, tzinfo=UTC)
    list(
        live(
            load_setup(EXAMPLE),
            read_dataset(EXAMPLE),
            start,
            start + timedelta(minutes=22),
            every=timedelta(minutes=2),
            issue_every=timedelta(minutes=5),
            valid_for=timedelta(minutes=30),
            limits=Limits(telemetry=timedelta(minutes=3)),
        )
    )
    assert [(at - start) // timedelta(minutes=1) for at in valued] == [0, 6, 10, 16, 20]


@pytest.mark.asyncio
async def test_a_dataset_reads_as_homedbs_history() -> None:
    history = DatasetHistory(read_dataset(EXAMPLE))
    start = datetime(2026, 10, 22, 12, 5, tzinfo=UTC)  # between two of the meters' samples
    end = start + HOUR
    pv = await history.power_series("dataset.pv", start, end, carry_in=timedelta(days=1))
    # The value in force at the start (12:00's), then the window's own; known to its end.
    assert pv.samples[0].at == datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    assert all(start <= s.at < end for s in pv.samples[1:]) and pv.known_until == end
    assert pv.unit == "kW"
    short = await history.power_series("dataset.pv", start, end, carry_in=timedelta(minutes=1))
    assert short.samples[0].at > start  # nothing carried in from further back than asked
    soc = await history.numeric_series("dataset.soc", start, end, carry_in=timedelta(days=1))
    assert soc.unit is None and soc.samples[0].value is not None and soc.samples[0].value > 1
    beats = await history.sample_times("dataset.heartbeat", start, end, carry_in=HOUR)
    assert len(beats) == 61  # one a minute, and the one in force at the start
    with pytest.raises(DatasetError, match="not an entry"):
        await history.power_series("sensor.pv", start, end, carry_in=HOUR)
    with pytest.raises(DatasetError, match="known until"):
        await history.power_series(
            "dataset.pv", start, datetime(2026, 11, 1, tzinfo=UTC), carry_in=HOUR
        )


def test_a_datasets_bindings_name_its_entries() -> None:
    setup, dataset = load_setup(EXAMPLE), read_dataset(EXAMPLE)
    bindings = dataset_bindings(setup, dataset)
    assert (bindings.pv, bindings.house, bindings.soc) == (
        "dataset.pv",
        "dataset.house",
        "dataset.soc",
    )
    assert (bindings.import_price, bindings.export_price) == (
        "dataset.import_price",
        "dataset.export_price",
    )
    assert bindings.inverter == "dataset.inverter" and bindings.soc_unit == "%"
    assert bindings.heartbeat == "dataset.heartbeat"
    assert bindings.plant == setup.plant and bindings.dc_load is None


def test_ledger_on_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["ledger", "--data", str(EXAMPLE), "--from", "2026-10-21", "--to", "2026-10-23"]
    assert main(argv) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("the battery's lots, 2026-10-21 00:00 CEST to 2026-10-23")
    rows = {line.split()[0]: line.split() for line in lines[2:5]}
    assert set(rows) == {"unknown", "pv", "grid"}
    assert rows["unknown"][-1] == "-"  # energy of unknown origin has no known cost
    assert lines[-1].startswith("unexplained: ")


def test_check_plant_on_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["check-plant", "--data", str(EXAMPLE)]
    assert main([*argv, "--from", "2026-10-22", "--to", "2026-10-23"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("plant model against the meters, 2026-10-22 .. 2026-10-23")
    assert "over 2 days without a telemetry gap" in out
    assert "import kWh/day" in out and "SOC by hours ahead" in out
    with pytest.raises(SystemExit, match="is after"):
        main([*argv, "--from", "2026-10-23", "--to", "2026-10-22"])


def test_fit_battery_on_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["fit-battery", "--data", str(EXAMPLE), "--days", "6"]
    assert main(argv) == 1  # a dataset has no now
    assert "--end is needed" in capsys.readouterr().err
    assert main([*argv, "--end", "2026-10-28"]) == 0
    out = capsys.readouterr().out
    # The example's battery loses 0.003 per kW each way (plant.examples), fitted back.
    assert "loss_charge_per_kw       0.00300" in out
    assert "loss_discharge_per_kw    0.00300" in out


def test_replay_on_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["replay", "--data", str(EXAMPLE), "--from", "2026-10-22", "--to", "2026-10-23"]
    assert main([*argv, "--every", "3h"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("8 valuations every 3:00:00 scored,")
    surplus = out.index("bucket       n     bias     MAE   p10 n   p10 met")
    assert [line.split()[0] for line in out[surplus + 1 : surplus + 5]] == [
        "0-1h",
        "1-3h",
        "3-12h",
        "12-48h",
    ]
    assert any(line.startswith("regime against the metered regime") for line in out)


def test_replay_leaves_a_fallback_unscored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Without a load forecast every valuation falls back: counted, not scored
    # (tariffkit#67 R-14).
    entries = {k: v for k, v in read_dataset(EXAMPLE).entries.items() if k != LOAD_FORECAST}
    write_dataset(tmp_path, entries)
    for name in ("plant.toml", "engine.toml"):
        (tmp_path / name).write_bytes((EXAMPLE / name).read_bytes())
    argv = ["replay", "--data", str(tmp_path), "--from", "2026-10-22", "--to", "2026-10-23"]
    assert main([*argv, "--every", "6h"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("0 valuations every 6:00:00 scored, 0 slots")
    assert "fell back (not scored): 4, load_forecast 4" in first


def test_check_plant_runs_on_a_dataset_whose_prices_are_its_forecasts_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``dataset/1``'s recorded prices are optional, the ledger's prices then the
    forecasts' in force; check-plant and fit-battery read them so."""
    from microgrid.cli.dataset import write_dataset

    whole = read_dataset(EXAMPLE)
    write_dataset(
        tmp_path,
        {n: e for n, e in whole.entries.items() if n not in ("import_price", "export_price")},
    )
    for name in ("plant.toml", "engine.toml"):
        (tmp_path / name).write_bytes((EXAMPLE / name).read_bytes())
    argv = ["check-plant", "--data", str(tmp_path), "--from", "2026-10-22", "--to", "2026-10-23"]
    assert main(argv) == 0
    assert capsys.readouterr().out.startswith("plant model against the meters")
    fit = ["fit-battery", "--data", str(tmp_path), "--days", "3", "--end", "2026-10-24"]
    assert main(fit) == 0
