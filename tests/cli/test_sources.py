"""The configurable sources (``sources/1``): the file, the readers, ``now`` and ``fetch``."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from microgrid.cli.dataset import write_dataset
from microgrid.cli.history import Limits
from microgrid.cli.main import main
from microgrid.cli.offline import load_setup
from microgrid.cli.sources.config import (
    Emhass,
    EntityForecast,
    Predbat,
    SourcesConfigError,
    read_sources,
)
from microgrid.cli.sources.entity import EntityError, parse_forecast
from microgrid.cli.sources.fetch import fetch_measured
from microgrid.cli.sources.home_assistant import HaClient
from microgrid.cli.sources.predbat import parse_load_forecast
from microgrid.cli.sources.read import read_now
from microgrid.engine import ValuationKind
from microgrid.engine.series import Price, Quantiles

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "dataset"
NOW = datetime(2026, 10, 22, 10, 3, tzinfo=UTC)


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / "plant.toml").write_bytes((EXAMPLE / "plant.toml").read_bytes())
    path = tmp_path / "sources.toml"
    path.write_text(text, encoding="utf-8")
    return path


# -- the file -----------------------------------------------------------------------------


def test_a_sources_file_names_each_inputs_source(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """schema = "sources/1"
plant = "plant.toml"
[home_assistant]
url = "http://ha.local:8123"
[prices]
from = "entity"
entity = "sensor.rates"
attribute = "rates"
keys = { import = "buy", export = "sell" }
[load_forecast]
from = "emhass"
[pv_forecast]
from = "entity"
entity = "sensor.pv_forecast"
attribute = "forecast"
period_minutes = 30
keys = { value = "watts" }
unit = "W"
""",
    )
    sources = read_sources(path)
    assert sources.plant == tmp_path / "plant.toml" and sources.engine is None
    assert sources.prices == EntityForecast(
        "sensor.rates", "rates", keys={"import": "buy", "export": "sell"}
    )
    assert sources.load_forecast == Emhass()  # EMHASS's own names, by default
    pv = sources.pv_forecast
    assert isinstance(pv, EntityForecast) and pv.end_key is None
    assert pv.period == timedelta(minutes=30) and pv.keys == {"value": "watts"}


def test_a_bad_sources_file_names_every_problem(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """schema = "sources/9"
plant = "plant.toml"
colour = "blue"
[prices]
from = "the_utility"
[load_forecast]
from = "entity"
entity = "sensor.load"
[measured]
from = "home_assistant"
dispatch = "select.mode"
""",
    )
    with pytest.raises(SourcesConfigError) as caught:
        read_sources(path)
    errors = "\n".join(caught.value.errors)
    for expected in (
        "schema",
        "colour: is not a setting",
        "prices.from: must be one of tariffkit, nordpool, amber, tibber, octopus, entsoe,"
        " openei, entity, action",
        "load_forecast.attribute: is required",
        "load_forecast.keys",
        "measured.modelled_modes: is required with a dispatch entity",
        "home_assistant: is required",
    ):
        assert expected in errors, expected


# -- the readers --------------------------------------------------------------------------


def test_predbat_cumulative_energy_becomes_power_on_its_steps() -> None:
    """Each value includes the 5-minute step from its time; a value equal to its
    neighbours is dropped (10:10 here), so the energy is a step function. The slot under
    way is covered, and every step tiles the engine's slots."""
    state = {
        "last_updated": "2026-10-22T10:00:00+00:00",
        "attributes": {
            "results": {
                "2026-10-22T00:00:00+0000": 0.0,
                "2026-10-22T09:55:00+0000": 0.95,
                "2026-10-22T10:00:00+0000": 1.0,
                "2026-10-22T10:05:00+0000": 1.1,
                "2026-10-22T10:15:00+0000": 1.1,
                "2026-10-22T10:20:00+0000": 1.3,
            }
        },
    }
    forecast = parse_load_forecast(state, Predbat(), now=NOW)
    starts = [i.start.strftime("%H:%M") for i in forecast.intervals]
    assert starts == ["10:00", "10:05", "10:10", "10:15", "10:20"]
    assert [round(i.value, 6) for i in forecast.intervals] == [0.6, 1.2, 0.0, 0.0, 2.4]
    assert forecast.source == "predbat"
    # Results that start mid-day: the first time's step is not read, so the day so far
    # is never taken as one step.
    late = {
        "last_updated": "2026-10-22T10:00:00+00:00",
        "attributes": {
            "results": {
                "2026-10-22T10:00:00+0000": 5.05,
                "2026-10-22T10:05:00+0000": 5.10,
                "2026-10-22T10:10:00+0000": 5.15,
            }
        },
    }
    found = parse_load_forecast(late, Predbat(), now=NOW)
    assert [round(i.value, 6) for i in found.intervals] == [0.6, 0.6]


def test_an_entitys_rows_become_a_forecast_in_its_unit() -> None:
    config = EntityForecast(
        "sensor.prices",
        "rates",
        keys={"import": "buy", "export": "sell"},
        unit="EUR/MWh",
    )
    state = {
        "last_updated": "2026-10-22T10:00:00+00:00",
        "attributes": {
            "rates": [
                {
                    "start": "2026-10-22T11:00:00+01:00",
                    "end": "2026-10-22T12:00:00+01:00",
                    "buy": 250,
                    "sell": 80,
                }
            ]
        },
    }
    forecast = parse_forecast(state, config, "prices")
    (interval,) = forecast.intervals
    assert interval.value == Price(0.25, 0.08)
    assert interval.start == datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    pv = EntityForecast(
        "sensor.pv", "f", keys={"value": "w"}, unit="W", end_key=None, period=timedelta(minutes=30)
    )
    rows = {
        "last_updated": "2026-10-22T10:00:00+00:00",
        "attributes": {"f": [{"start": "2026-10-22T10:00:00+00:00", "w": 1500}]},
    }
    (slot,) = parse_forecast(rows, pv, "pv_forecast").intervals
    assert slot.value == Quantiles.point(1.5) and slot.end - slot.start == timedelta(minutes=30)
    with pytest.raises(EntityError, match="no unit"):
        parse_forecast(
            rows,
            EntityForecast(
                "sensor.pv", "f", keys={"value": "w"}, end_key=None, period=timedelta(minutes=30)
            ),
            "load_forecast",
        )


# -- now and fetch, against a Home Assistant that answers --------------------------------

STATES: dict[str, dict[str, Any]] = {
    "sensor.rates": {
        "state": "0.30",
        "last_updated": "2026-10-22T09:40:00+00:00",
        "attributes": {
            "rates": [
                {
                    "start": (NOW.replace(minute=0) + timedelta(hours=hour)).isoformat(),
                    "end": (NOW.replace(minute=0) + timedelta(hours=hour + 1)).isoformat(),
                    "buy": 0.30,
                    "sell": 0.07,
                }
                for hour in range(-1, 49)
            ],
        },
    },
    "sensor.p_load_forecast": {
        "state": "500",
        "last_updated": "2026-10-22T09:50:00+00:00",
        "attributes": {
            "unit_of_measurement": "W",
            "forecasts": [
                {
                    "date": (NOW.replace(minute=0) + timedelta(minutes=30 * i)).isoformat(),
                    "p_load_forecast": "600",
                }
                for i in range(100)
            ],
        },
    },
    "sensor.battery_soc": {
        "state": "55",
        "last_changed": "2026-10-22T09:58:00+00:00",
        "attributes": {"unit_of_measurement": "%"},
    },
    "sensor.grid_power": {"state": "1.0", "attributes": {"unit_of_measurement": "kW"}},
    "sensor.house": {"state": "0.5", "attributes": {"unit_of_measurement": "kW"}},
    "sensor.pv": {"state": "1.0", "attributes": {"unit_of_measurement": "kW"}},
    "sensor.battery": {"state": "1.0", "attributes": {"unit_of_measurement": "kW"}},
    "sensor.import_price": {"state": "0.3", "attributes": {"unit_of_measurement": "USD/kWh"}},
    "sensor.export_price": {"state": "0.07", "attributes": {"unit_of_measurement": "USD/kWh"}},
}


def _client() -> HaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/states/"):
            entity = path.removeprefix("/api/states/")
            if entity in STATES:
                return httpx.Response(200, json=STATES[entity])
            return httpx.Response(404, text="Entity not found.")
        if path.startswith("/api/history/period/"):
            ids = request.url.params["filter_entity_id"].split(",")
            return httpx.Response(
                200,
                json=[
                    [
                        {
                            "entity_id": e,
                            "state": "1.0",
                            "last_changed": "2026-10-22T08:00:00+00:00",
                        },
                        {"state": "2.0", "last_changed": "2026-10-22T09:00:00+00:00"},
                    ]
                    for e in ids
                ],
            )
        return httpx.Response(404)

    return HaClient("https://ha.example", SecretStr("t"), transport=httpx.MockTransport(handler))


def test_now_reads_what_is_configured_and_names_what_is_not(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """schema = "sources/1"
plant = "plant.toml"
[home_assistant]
url = "https://ha.example"
[prices]
from = "entity"
entity = "sensor.rates"
attribute = "rates"
keys = { import = "buy", export = "sell" }
[load_forecast]
from = "emhass"
[measured]
from = "home_assistant"
soc = "sensor.battery_soc"
dispatch_as_modelled = true
""",
    )
    sources = read_sources(path)
    setup = load_setup(tmp_path, sources.plant, sources.engine)

    async def run() -> Any:
        async with _client() as client:
            return await read_now(sources, setup.plant, setup.engine.config, NOW, client=client)

    read = asyncio.run(run())
    assert read.sources == {
        "prices": "entity",
        "load_forecast": "emhass",
        "soc": "home_assistant",
        "dispatch": "home_assistant",
    }
    # No meters or prices recorded under [measured]: no lots, and why.
    assert read.problems.keys() == {"pv_forecast", "battery_lots"}
    assert read.problems["pv_forecast"] == "no source configured"
    assert "house meter" in read.problems["battery_lots"]
    assert read.inputs.lots is None
    soc = read.inputs.soc
    assert soc is not None and soc.samples[0].value == pytest.approx(0.55)
    valuation = setup.engine.value(read.inputs, at=NOW)
    # No PV forecast: the engine names it, and falls back, rather than guessing one.
    assert valuation.kind is ValuationKind.FALLBACK_IMPORT
    assert "pv_forecast" in valuation.degraded
    # What ``now --save`` writes reads back as a dataset, units included.
    saved = tmp_path / "saved"
    write_dataset(saved, read.entries)
    (saved / "plant.toml").write_bytes((tmp_path / "plant.toml").read_bytes())
    assert main(["value", "--data", str(saved), "--at", NOW.isoformat()]) == 0


def test_now_runs_the_ledger_over_the_recorded_history(tmp_path: Path) -> None:
    """``now`` gives the engine the battery's lots, the ledger run over the history
    ``fetch`` reads, so a stored-cost valuation names no ``battery_lots``."""
    path = _write(
        tmp_path,
        """schema = "sources/1"
plant = "plant.toml"
[home_assistant]
url = "https://ha.example"
[measured]
from = "home_assistant"
soc = "sensor.battery_soc"
house = "sensor.house"
import_price = "sensor.import_price"
export_price = "sensor.export_price"
dispatch_as_modelled = true
[measured.meters]
pv = "sensor.pv"
grid = "sensor.grid_power"
battery = "sensor.battery"
""",
    )
    sources = read_sources(path)
    setup = load_setup(tmp_path, sources.plant, sources.engine)

    async def run() -> Any:
        async with _client() as client:
            return await read_now(
                sources,
                setup.plant,
                setup.engine.config,
                NOW,
                client=client,
                limits=Limits(telemetry=timedelta(hours=3)),  # the fake writes hourly
            )

    read = asyncio.run(run())
    assert "battery_lots" not in read.problems
    assert read.sources["battery_lots"].startswith("the ledger over 2 days")
    lots = read.inputs.lots
    assert lots is not None and lots.samples and lots.samples[-1].value is not None


def test_fetch_writes_the_measured_history(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """schema = "sources/1"
plant = "plant.toml"
[home_assistant]
url = "https://ha.example"
[measured]
from = "home_assistant"
soc = "sensor.battery_soc"
house = "sensor.house"
[measured.meters]
grid = "sensor.grid_power"
""",
    )
    measured = read_sources(path).measured
    assert measured is not None

    async def run() -> Any:
        async with _client() as client:
            return await fetch_measured(client, measured, NOW - timedelta(hours=3), NOW)

    entries = asyncio.run(run())
    assert set(entries) == {"grid", "house", "soc", "heartbeat"}
    grid = entries["grid"].series
    assert grid.unit == "kW" and [s.value for s in grid.samples] == [1.0, 2.0]
    assert entries["soc"].series.unit == "%"


def test_the_commands_without_a_token_say_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MICROGRID_HA_TOKEN", raising=False)
    path = _write(
        tmp_path,
        """schema = "sources/1"
plant = "plant.toml"
[home_assistant]
url = "https://ha.example"
[load_forecast]
from = "emhass"
""",
    )
    assert main(["sources", "--sources", str(path)]) == 1
    assert "MICROGRID_HA_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("url", "token_env", "refused"),
    [
        ("http://homeassistant.local:8123", "MICROGRID_HA_TOKEN", None),
        ("http://192.168.1.5:8123", "SITE_HA_TOKEN", None),
        ("http://ha:8123", "MICROGRID_HA_TOKEN", None),
        ("https://ha.example.com", "MICROGRID_HA_TOKEN", None),
        ("http://ha.example.com", "MICROGRID_HA_TOKEN", "home_assistant.url"),
        ("https://attacker.example", "GITHUB_TOKEN", "home_assistant.token_env"),
        ("ftp://ha.local", "MICROGRID_HA_TOKEN", "home_assistant.url"),
        ("http://ha.local:8123", "HA_TOKEN", None),
        ("http://100.101.102.103:8123", "MICROGRID_HA_TOKEN", None),
        ("http://ha.lan.:8123", "MICROGRID_HA_TOKEN", None),
        ("http://[fd00::5]:8123", "MICROGRID_HA_TOKEN", None),
        ("http://[2001:4860::8888]:8123", "MICROGRID_HA_TOKEN", "home_assistant.url"),
        ("http://8.8.8.8:8123", "MICROGRID_HA_TOKEN", "home_assistant.url"),
        ("http://ha.lan", "HA_TOKEN_X", "home_assistant.token_env"),
    ],
)
def test_the_ha_token_goes_only_to_a_safe_url_from_an_ha_token_variable(
    tmp_path: Path, url: str, token_env: str, refused: str | None
) -> None:
    """A shared sources file must not send any secret
    anywhere, nor the token in cleartext beyond the local network."""
    from microgrid.cli.sources.config import SourcesConfigError, read_sources

    path = tmp_path / "sources.toml"
    path.write_text(
        f'schema = "sources/1"\nplant = "plant.toml"\n[home_assistant]\nurl = "{url}"\n'
        f'token_env = "{token_env}"\n'
    )
    if refused is None:
        read_sources(path)
        return
    with pytest.raises(SourcesConfigError, match=refused):
        read_sources(path)


def test_a_csv_source_is_refused(tmp_path: Path) -> None:
    """CSV is not an input type: a ``csv`` source is refused."""
    path = _write(
        tmp_path,
        """schema = "sources/1"
plant = "plant.toml"
[prices]
from = "csv"
path = "prices.csv"
""",
    )
    with pytest.raises(SourcesConfigError) as refused:
        read_sources(path)
    assert (
        "prices.from: must be one of tariffkit, nordpool, amber, tibber, octopus, entsoe,"
        " openei, entity, action" in str(refused.value)
    )
    assert "csv" not in str(refused.value).split("must be one of", 1)[1]
