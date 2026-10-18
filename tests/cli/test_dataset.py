"""The ``files`` source and the example dataset."""

import json
import subprocess
import sys
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from microgrid.cli.dataset import (
    BATTERY_FIRST,
    EXPORT_PRICE,
    IMPORT_PRICE,
    LOAD_FORECAST,
    PRICES,
    PV_FORECAST,
    SOC,
    DatasetError,
    Entry,
    read_dataset,
    write_dataset,
)
from microgrid.cli.inputs import currency
from microgrid.cli.inputs import inputs as dataset_inputs
from microgrid.cli.offline import dataset_lots, load_setup, with_recorded_prices
from microgrid.engine import Engine, EngineConfig
from microgrid.engine.series import Forecast, Interval, Price, Quantiles, Sample, Series, value_at
from microgrid.plant import Plant, PlantConfig

T0 = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)
HALF = timedelta(minutes=30)
REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "dataset"


def _forecast[V](values: list[V], issued: datetime) -> Forecast[V]:
    return Forecast(
        tuple(Interval(T0 + k * HALF, T0 + (k + 1) * HALF, v) for k, v in enumerate(values)),
        issued,
        "test",
    )


def _entries() -> dict[str, Entry]:
    end = T0 + timedelta(hours=2)
    return {
        "pv": Entry(
            "number",
            Series.of(
                [(T0, 1.5), (T0 + HALF, None), (T0 + 2 * HALF, 0.25)], known_until=end, unit="kW"
            ),
        ),
        SOC: Entry("number", Series.of([(T0, 42.0)], known_until=end, unit="%")),
        BATTERY_FIRST: Entry("bool", Series.of([(T0, True), (T0 + HALF, False)], known_until=end)),
        PRICES: Entry(
            "price",
            Series(
                (
                    Sample(T0, _forecast([Price(0.3, 0.07), Price(-0.01, 0.0)], T0)),
                    Sample(T0 + HALF, None),  # the source answered nothing
                ),
                end,
                "USD/kWh",
            ),
            forecasts=True,
        ),
        PV_FORECAST: Entry(
            "quantiles",
            Series.of(
                [
                    (
                        T0,
                        _forecast(
                            [Quantiles({0.1: 0.5, 0.5: 1.0, 0.9: 1.25}), Quantiles.point(2.0)], T0
                        ),
                    )
                ],
                known_until=end,
                unit="kW",
            ),
            forecasts=True,
        ),
        LOAD_FORECAST: Entry(
            "number",
            Series.of(
                [(T0 + timedelta(seconds=20), _forecast([0.5, 0.75], T0))],
                known_until=end,
                unit="kW",
            ),
            forecasts=True,
        ),
    }


def test_a_dataset_reads_back_as_written(tmp_path: Path) -> None:
    written = _entries()
    write_dataset(tmp_path, written)
    assert read_dataset(tmp_path).entries == written


def test_the_inputs_take_each_entry_by_its_name(tmp_path: Path) -> None:
    write_dataset(tmp_path, _entries())
    inputs = dataset_inputs(read_dataset(tmp_path))
    assert inputs.soc is not None and inputs.soc.samples[0].value == 0.42  # % as a fraction
    assert inputs.prices is not None and inputs.prices.samples[1].value is None
    assert inputs.dispatch_as_modelled is not None and inputs.dc_load is None


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize(
    ("manifest", "body", "named"),
    [
        ({"format": "dataset/2"}, None, "format must be 'dataset/1'"),
        (
            {"format": "dataset/1", "entries": {"x": {"file": "x.json", "kind": "text"}}},
            None,
            "kind must be",
        ),
        (None, {"known_until": "2026-10-25T02:00:00", "samples": []}, "has no offset"),
        (
            None,
            {"known_until": "2026-10-25T02:00:00Z", "samples": [["2026-10-25T00:00:00Z", "x"]]},
            r"samples\[0\]: not a finite number",
        ),
        (
            None,
            {
                "known_until": "2026-10-25T02:00:00Z",
                "samples": [["2026-10-25T01:00:00Z", 1], ["2026-10-25T00:00:00Z", 2]],
            },
            "out of time order",
        ),
        (
            None,
            {"known_until": "2026-10-25T00:00:00Z", "samples": [["2026-10-25T01:00:00Z", 1]]},
            "after known_until",
        ),
        (None, {"known_until": "2026-10-25T02:00:00Z"}, "has a list 'samples'"),
        (None, {"known_until": "2026-10-25T02:00:00Z", "samples": None}, "has a list 'samples'"),
        (None, {"known_until": "2026-10-25T02:00:00Z", "issues": []}, "has a list 'samples'"),
        (
            {"format": "dataset/1", "entries": {"x": {"file": "../x.json", "kind": "number"}}},
            None,
            "must be in the dataset's directory",
        ),
        (
            {"format": "dataset/1", "entries": {"../x": {"file": "x.json", "kind": "number"}}},
            None,
            "a name is letters",
        ),
    ],
)
def test_a_bad_dataset_is_named(
    tmp_path: Path, manifest: dict[str, object] | None, body: dict[str, object] | None, named: str
) -> None:
    _write(
        tmp_path / "manifest.json",
        manifest or {"format": "dataset/1", "entries": {"x": {"file": "x.json", "kind": "number"}}},
    )
    if body is not None:
        _write(tmp_path / "x.json", body)
    with pytest.raises(DatasetError, match=named):
        read_dataset(tmp_path)


def _issues(issue: dict[str, object]) -> dict[str, object]:
    return {"known_until": "2026-10-25T02:00:00Z", "issues": [issue]}


@pytest.mark.parametrize(
    ("body", "named"),
    [
        ({"known_until": "2026-10-25T02:00:00Z", "samples": []}, "has a list 'issues'"),
        (
            _issues({"received_at": "2026-10-25T00:00:00Z", "issued_at": "2026-10-25T00:00:00Z"}),
            r"issues\[0\]\.intervals: must be a list",
        ),
        (
            _issues(
                {
                    "received_at": "2026-10-25T00:00:00Z",
                    "issued_at": "2026-10-25T00:00:00Z",
                    "intervals": [["2026-10-25T01:00:00Z", "2026-10-25T00:30:00Z", 1.0]],
                }
            ),
            r"issues\[0\]\.intervals\[0\]: ",
        ),
    ],
)
def test_a_bad_forecasts_file_is_named(tmp_path: Path, body: dict[str, object], named: str) -> None:
    _write(
        tmp_path / "manifest.json",
        {
            "format": "dataset/1",
            "entries": {"x": {"file": "x.json", "kind": "number", "forecasts": True}},
        },
    )
    _write(tmp_path / "x.json", body)
    with pytest.raises(DatasetError, match=named):
        read_dataset(tmp_path)


def test_a_name_that_leaves_the_directory_is_not_written(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="a name is letters"):
        write_dataset(tmp_path / "d", {"../evil": _entries()[SOC]})
    assert not (tmp_path / "evil.json").exists() and not (tmp_path / "series").exists()


def test_the_inputs_are_in_the_engines_units(tmp_path: Path) -> None:
    entries = _entries()
    load = entries[LOAD_FORECAST].series
    entries[LOAD_FORECAST] = Entry(
        "number",
        Series(
            tuple(
                Sample(
                    s.at,
                    _forecast(
                        [1000 * i.value for i in cast("Forecast[float]", s.value).intervals], T0
                    ),
                )
                for s in load.samples
            ),
            load.known_until,
            "W",
        ),
        forecasts=True,
    )
    prices = entries[PRICES].series
    entries[PRICES] = Entry("price", Series(prices.samples, prices.known_until, "USD/MWh"), True)
    entries[SOC] = Entry("number", Series.of([(T0, 0.42)], known_until=T0 + HALF))  # no unit
    pv = entries[PV_FORECAST].series
    entries[PV_FORECAST] = Entry(
        "quantiles",
        Series(
            tuple(
                Sample(
                    s.at,
                    _forecast(
                        [
                            Quantiles({q: 1000 * kw for q, kw in i.value.kw.items()})
                            for i in cast("Forecast[Quantiles]", s.value).intervals
                        ],
                        T0,
                    ),
                )
                for s in pv.samples
            ),
            pv.known_until,
            "W",
        ),
        forecasts=True,
    )
    write_dataset(tmp_path, entries)
    inputs = dataset_inputs(read_dataset(tmp_path))
    assert inputs.load is not None and inputs.prices is not None and inputs.soc is not None
    load_read = inputs.load.samples[0].value
    assert load_read is not None and [i.value for i in load_read.intervals] == [0.5, 0.75]
    price = inputs.prices.samples[0].value
    assert price is not None
    assert price.intervals[0].value.p_import == pytest.approx(0.0003)
    assert price.intervals[0].value.p_export == pytest.approx(0.00007)
    assert inputs.soc.samples[0].value == 0.42  # no unit: a fraction already
    assert isinstance(inputs.pv, Series)
    pv_read = inputs.pv.samples[0].value
    assert pv_read is not None
    assert [i.value.kw for i in pv_read.intervals] == [{0.1: 0.5, 0.5: 1.0, 0.9: 1.25}, {0.5: 2.0}]


@pytest.mark.parametrize(
    ("name", "unit", "named"),
    [
        (LOAD_FORECAST, None, "not a power unit: None"),
        (PV_FORECAST, None, "not a power unit: None"),
        (PRICES, "kWh", "not a price unit"),
        (SOC, "kWh", "not a fraction unit"),
    ],
)
def test_a_unit_the_engine_cannot_use_is_refused(
    tmp_path: Path, name: str, unit: str | None, named: str
) -> None:
    entries = _entries()
    entry = entries[name]
    entries[name] = Entry(
        entry.kind, Series(entry.series.samples, entry.series.known_until, unit), entry.forecasts
    )
    write_dataset(tmp_path, entries)
    with pytest.raises(DatasetError, match=f"{name}: {named}"):
        dataset_inputs(read_dataset(tmp_path))


def _recorded(unit: str | None) -> Entry:
    return Entry("number", Series.of([(T0, 85.0)], known_until=T0 + HALF, unit=unit))


def test_recorded_prices_are_per_kwh_like_the_forecasts(tmp_path: Path) -> None:
    """A recorded price in a per-MWh unit is converted, as a forecast's is: read as
    written it would cost a kWh a thousand times over."""
    entries = _entries() | {
        IMPORT_PRICE: _recorded("USD/MWh"),
        EXPORT_PRICE: _recorded(None),  # no unit: per kWh, as given
    }
    write_dataset(tmp_path, entries)
    dataset = with_recorded_prices(read_dataset(tmp_path))
    imported = dataset.series(IMPORT_PRICE)
    exported = dataset.series(EXPORT_PRICE)
    assert imported is not None and exported is not None
    assert (imported.samples[0].value, imported.unit) == (0.085, "USD/kWh")
    assert (exported.samples[0].value, exported.unit) == (85.0, None)
    assert currency(dataset) == "USD"


@pytest.mark.parametrize(
    ("recorded", "named"),
    [("EUR/kWh", "more than one currency"), ("kWh", "import_price: not a price unit")],
)
def test_recorded_prices_in_another_currency_or_no_price_unit_are_refused(
    tmp_path: Path, recorded: str, named: str
) -> None:
    write_dataset(tmp_path, _entries() | {IMPORT_PRICE: _recorded(recorded)})
    with pytest.raises(DatasetError, match=named):
        with_recorded_prices(read_dataset(tmp_path))


def test_pv_per_array(tmp_path: Path) -> None:
    entries = _entries()
    pv = entries.pop(PV_FORECAST)
    write_dataset(tmp_path, entries | {f"{PV_FORECAST}.roof": pv, f"{PV_FORECAST}.shed": pv})
    inputs = dataset_inputs(read_dataset(tmp_path))
    assert isinstance(inputs.pv, dict) and sorted(inputs.pv) == ["roof", "shed"]
    write_dataset(tmp_path, {**entries, PV_FORECAST: pv, f"{PV_FORECAST}.roof": pv})
    with pytest.raises(DatasetError, match="for the site or per array, not both"):
        dataset_inputs(read_dataset(tmp_path))


def test_a_series_asked_for_as_forecasts_is_named(tmp_path: Path) -> None:
    write_dataset(tmp_path, _entries())
    dataset = read_dataset(tmp_path)
    with pytest.raises(DatasetError, match="not forecasts"):
        dataset.forecasts(SOC)
    with pytest.raises(DatasetError, match="not a series of values"):
        dataset.series(PRICES)


def test_the_example_dataset_is_its_scripts(tmp_path: Path) -> None:
    """The committed dataset is what ``make_example_dataset.py`` makes (deterministic)."""
    script = REPO / "scripts" / "make_example_dataset.py"
    subprocess.run(
        [sys.executable, str(script), "--out", str(tmp_path)], check=True, capture_output=True
    )
    made = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    # Not ``expected/``: the commands' outputs on it (``make_expected_outputs.py``).
    committed = sorted(
        p.relative_to(EXAMPLE)
        for p in EXAMPLE.rglob("*")
        if p.is_file() and p.relative_to(EXAMPLE).parts[0] != "expected"
    )
    assert made == committed
    for relative in made:
        assert (tmp_path / relative).read_bytes() == (EXAMPLE / relative).read_bytes(), relative


def _example_plant() -> Plant:
    data = tomllib.loads((EXAMPLE / "plant.toml").read_text(encoding="utf-8"))
    return Plant(PlantConfig.from_mapping(data, tz=ZoneInfo(data["timezone"])))


def test_the_example_meters_are_the_plants() -> None:
    """The example's meters read as its plant reads them: what they say was consumed is
    the house's load and the hybrid's draw, and each SOC step is what the battery's
    meter says it stored or drew (the byte test alone would hold a wrong sign in
    place)."""
    plant, dataset = _example_plant(), read_dataset(EXAMPLE)
    battery = plant.config.battery
    assert battery is not None
    part = battery.battery
    meters = {name: dataset.series(name) for name in plant.meters}
    house, soc = dataset.series("house"), dataset.series(SOC)
    assert house is not None and soc is not None
    hybrid = plant.config.hybrid
    assert hybrid is not None
    for k, sample in enumerate(house.samples):
        reading = {
            name: cast("float | None", s.samples[k].value)
            for name, s in meters.items()
            if s is not None
        }
        flows = plant.read_meters(reading)
        assert flows is not None
        draw = flows.consumed - cast("float", sample.value)
        # The hybrid's standby, and its working draw while the battery converts.
        assert hybrid.standby_kw - 1e-3 <= draw <= hybrid.standby_kw + hybrid.working_kw + 1e-3, (
            sample.at,
            draw,
        )
        metered = cast("float", reading["battery"])
        stored = (part.meter_charge if metered > 0 else part.meter_discharge) * metered / 6
        step = cast("float", soc.samples[k + 1].value) - cast("float", soc.samples[k].value)
        assert step / 100 * part.capacity_kwh == pytest.approx(stored, abs=1e-3), sample.at


@pytest.mark.parametrize(
    "at", ["2026-10-22T10:03:00+00:00", "2026-10-25T01:33:00+00:00", "2026-10-27T17:13:00+00:00"]
)
def test_the_example_dataset_values_the_week(at: str) -> None:
    """Across the week, the DST change included, the example plant values nominally."""
    plant = _example_plant()
    engine = Engine(
        EngineConfig.from_mapping({"schema": "engine/1", "price_max_age_minutes": 60}, plant=plant)
    )
    dataset = read_dataset(EXAMPLE)
    read = replace(dataset_inputs(dataset), lots=dataset_lots(load_setup(EXAMPLE), dataset))
    valuation = engine.value(read, at=datetime.fromisoformat(at))
    assert valuation is not None and valuation.kind.value == "stored_value_v1"
    assert valuation.degraded == () and len(valuation.slots) > 200


@pytest.mark.parametrize(
    "at", ["2026-10-21T06:03:00+00:00", "2026-10-22T11:03:00+00:00", "2026-10-24T19:47:00+00:00"]
)
def test_the_lots_a_valuation_reads_are_only_what_was_received_by_then(
    at: str, tmp_path: Path
) -> None:
    """``offline.dataset_lots`` over the whole dataset gives, at a moment, the lots the
    dataset cut at that moment gives: nothing later is read (the stored-cost price). Off
    a slot boundary: at one exactly, the recorder's half-open read (Home Assistant's,
    the service's tracker's) leaves the cut dataset a SOC sample stamped then, so it
    sees less, never more."""
    from microgrid.cli.dataset import Entry, write_dataset

    moment = datetime.fromisoformat(at)
    whole = read_dataset(EXAMPLE)
    entries = {}
    for name, entry in whole.entries.items():
        kept = tuple(s for s in entry.series.samples if s.at <= moment)
        entries[name] = Entry(entry.kind, Series(kept, moment, entry.series.unit), entry.forecasts)
    write_dataset(tmp_path, entries)
    for name in ("plant.toml", "engine.toml"):
        (tmp_path / name).write_bytes((EXAMPLE / name).read_bytes())
    setup = load_setup(EXAMPLE)
    full = dataset_lots(setup, whole)
    cut = dataset_lots(load_setup(tmp_path), read_dataset(tmp_path))
    assert full is not None and cut is not None
    then, now = value_at(full, moment), value_at(cut, moment)
    assert then is not None and now is not None and then.pool is not None and now.pool is not None
    assert then.as_of == now.as_of
    assert then.pool.unit == pytest.approx(now.pool.unit)
    assert then.pool.kwh == pytest.approx(now.pool.kwh)
