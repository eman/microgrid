"""The price readers: each named integration's published shape (synthetic, as its source
publishes it), the generic action reader, rows and the import/export join."""

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from microgrid.cli.offline import load_setup
from microgrid.cli.sources import SourceError
from microgrid.cli.sources.action import parse_response
from microgrid.cli.sources.amber import parse_prices as parse_amber
from microgrid.cli.sources.config import (
    ActionForecast,
    Amber,
    EntityForecast,
    Entsoe,
    NordPool,
    Octopus,
    OpenEI,
    SourcesConfigError,
    Tibber,
    forecast_source,
)
from microgrid.cli.sources.entity import parse_forecast
from microgrid.cli.sources.entsoe import parse_prices as parse_entsoe
from microgrid.cli.sources.nordpool import fetch_prices as fetch_nordpool
from microgrid.cli.sources.octopus import fetch_prices as fetch_octopus
from microgrid.cli.sources.octopus import parse_prices as parse_octopus
from microgrid.cli.sources.openei import OpenEIError
from microgrid.cli.sources.openei import parse_prices as parse_openei
from microgrid.cli.sources.prices import PriceJoinError, join_prices
from microgrid.cli.sources.read import read_forecast, source_name
from microgrid.cli.sources.rows import RowError, on_slots, rows_forecast
from microgrid.cli.sources.tibber import TibberError
from microgrid.cli.sources.tibber import parse_prices as parse_tibber
from microgrid.engine.series import Forecast, Interval, Price, align, price_mean

NOW = datetime(2026, 10, 22, 10, 3, tzinfo=UTC)
H = timedelta(hours=1)
LA = ZoneInfo("America/Los_Angeles")
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "dataset"


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 22, hour, minute, tzinfo=UTC)


def _prices(forecast: Forecast[Any]) -> list[tuple[datetime, datetime, float, float]]:
    """Each interval's times and prices, rounded past a scale's float error."""
    return [
        (i.start, i.end, round(i.value.p_import, 9), round(i.value.p_export, 9))
        for i in forecast.intervals
    ]


class FakeApi:
    """Home Assistant as the readers ask it: states, action answers, config entries."""

    def __init__(
        self,
        states: dict[str, dict[str, Any]] | None = None,
        answers: dict[tuple[str, str], Any] | None = None,
        entries: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.states = states or {}
        self.answers = answers or {}
        self.entries = entries or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        if entity_id not in self.states:
            raise SourceError(f"{entity_id}: not found")
        return self.states[entity_id]

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        self.calls.append((domain, service, data))
        answer = self.answers.get((domain, service))
        if callable(answer):
            return answer(data)
        if answer is None:
            raise SourceError(f"{domain}.{service}: no answer")
        return answer

    async def config_entries(self, domain: str) -> list[dict[str, Any]]:
        return self.entries.get(domain, [])


# -- rows and the join ---------------------------------------------------------------------


def test_rows_read_any_time_form_and_end_at_the_next_start() -> None:
    rows = [
        {"time": "2026-10-22 12:00:00+02:00", "price": 0.2},  # a space, ENTSO-E's form
        {"time": datetime(2026, 10, 22, 11, tzinfo=UTC), "price": 0.3},  # inside HA
        {"time": "2026-10-22T09:00:00Z", "price": 0.1},
    ]
    found = rows_forecast(
        rows, start_key="time", value_key="price", issued_at=NOW, source="s", where="w"
    )
    assert [(i.start, i.end, i.value) for i in found.intervals] == [
        (_at(9), _at(10), 0.1),
        (_at(10), _at(11), 0.2),
        (_at(11), _at(12), 0.3),  # the last lasts as long as the one before
    ]


def test_rows_refuse_what_cannot_be_read() -> None:
    def read(rows: list[object], **kwargs: Any) -> None:
        rows_forecast(
            rows, start_key="t", value_key="v", issued_at=NOW, source="s", where="w", **kwargs
        )

    with pytest.raises(RowError, match="without an offset"):
        read([{"t": "2026-10-22 10:00:00", "v": 1}, {"t": "2026-10-22 11:00:00", "v": 1}])
    with pytest.raises(RowError, match="not a number"):
        read([{"t": "2026-10-22T10:00:00Z", "v": "high"}], period=H)
    with pytest.raises(RowError, match="cannot say how long"):
        read([{"t": "2026-10-22T10:00:00Z", "v": 1}])
    with pytest.raises(RowError, match="a second, different row"):
        read(
            [{"t": "2026-10-22T10:00:00Z", "v": 1}, {"t": "2026-10-22T10:00:00Z", "v": 2}], period=H
        )
    with pytest.raises(RowError, match="no rows"):
        read([])
    # a naive time in the zone the source publishes in
    local = rows_forecast(
        [{"t": "2026-10-22 03:00:00", "v": 1}],
        start_key="t",
        value_key="v",
        period=H,
        tz=LA,
        issued_at=NOW,
        source="s",
        where="w",
    )
    assert local.intervals[0].start == _at(10)


def test_import_and_export_join_where_both_are_known() -> None:
    imports = Forecast(
        (Interval(_at(10), _at(11), 0.30), Interval(_at(11), _at(13), 0.40)),
        _at(9, 50),
        "in",
    )
    exports = Forecast(
        (Interval(_at(10, 30), _at(12), 0.05), Interval(_at(12), _at(14), 0.07)),
        _at(9, 40),
        "out",
    )
    joined = join_prices(imports, exports)
    assert _prices(joined) == [
        (_at(10, 30), _at(11), 0.30, 0.05),
        (_at(11), _at(12), 0.40, 0.05),
        (_at(12), _at(13), 0.40, 0.07),
    ]
    assert joined.issued_at == _at(9, 40)  # only as fresh as its older part
    assert _prices(join_prices(imports, 0.04))[0] == (_at(10), _at(11), 0.30, 0.04)
    later = Forecast((Interval(_at(13), _at(14), 0.05),), NOW, "out")
    with pytest.raises(PriceJoinError, match="share no interval"):
        join_prices(imports, later)


def test_quarter_hours_are_split_at_the_engines_slots() -> None:
    quarters = Forecast(
        tuple(
            Interval(
                _at(10) + i * timedelta(minutes=15), _at(10) + (i + 1) * timedelta(minutes=15), v
            )
            for i, v in enumerate(
                [Price(0.2, 0.0), Price(0.5, 0.0), Price(0.3, 0.0), Price(0.1, 0.0)]
            )
        ),
        NOW,
        "q",
    )
    ten = timedelta(minutes=10)
    split = on_slots(quarters, ten)
    assert [(i.start.minute, i.end.minute) for i in split.intervals] == [
        (0, 10),
        (10, 15),
        (15, 20),
        (20, 30),
        (30, 40),
        (40, 45),
        (45, 50),
        (50, 0),
    ]
    starts = [_at(10) + i * ten for i in range(6)]
    priced = align(split, starts, ten, mean=price_mean)
    assert [None if p is None else round(p.p_import, 9) for p in priced] == [
        0.2,
        0.35,
        0.5,
        0.3,
        0.2,
        0.1,
    ]
    assert on_slots(quarters, timedelta(minutes=15)) is quarters  # nothing to split


# -- the named readers ---------------------------------------------------------------------


def test_nord_pool_prices_per_mwh_become_per_kwh_and_end_with_what_is_published() -> None:
    def answer(data: dict[str, Any]) -> Any:
        if data["date"] != "2026-10-22":
            raise SourceError("no prices for 2026-10-23 yet")
        return {
            "SE3": [
                {
                    "start": "2026-10-22T10:00:00+00:00",
                    "end": "2026-10-22T11:00:00+00:00",
                    "price": 85.0,
                },
                {
                    "start": "2026-10-22T11:00:00+00:00",
                    "end": "2026-10-22T12:00:00+00:00",
                    "price": 120.5,
                },
            ]
        }

    api = FakeApi(
        answers={("nordpool", "get_prices_for_date"): answer},
        entries={"nordpool": [{"entry_id": "np1", "state": "loaded"}]},
    )
    found = asyncio.run(fetch_nordpool(api, NordPool("SE3", export=0.02), now=NOW))
    assert _prices(found) == [
        (_at(10), _at(11), 0.085, 0.02),
        (_at(11), _at(12), 0.1205, 0.02),
    ]
    assert found.issued_at == NOW
    assert [call[2]["date"] for call in api.calls] == ["2026-10-22", "2026-10-23"]
    assert api.calls[0][2] == {"config_entry": "np1", "date": "2026-10-22", "areas": ["SE3"]}


def test_amber_joins_the_general_channel_with_feed_in() -> None:
    def channel(prefix: str, now: float, later: list[float]) -> dict[str, dict[str, Any]]:
        price = {
            "state": str(now),
            "last_updated": "2026-10-22T10:00:05+00:00",
            "attributes": {
                "start_time": "2026-10-22T20:00:01+10:00",
                "end_time": "2026-10-22T20:30:00+10:00",
                "per_kwh": now,
                "duration": 30,
            },
        }
        start = datetime(2026, 10, 22, 20, 30, tzinfo=ZoneInfo("Australia/Brisbane"))
        forecasts = [
            {
                "start_time": (
                    start + i * timedelta(minutes=30) + timedelta(seconds=1)
                ).isoformat(),
                "end_time": (start + (i + 1) * timedelta(minutes=30)).isoformat(),
                "per_kwh": value,
                "duration": 30,
            }
            for i, value in enumerate(later)
        ]
        forecast = {
            "state": str(later[0]),
            "last_updated": "2026-10-22T10:00:04+00:00",
            "attributes": {"forecasts": forecasts, "channel_type": prefix},
        }
        return {f"sensor.home_{prefix}_price": price, f"sensor.home_{prefix}_forecast": forecast}

    states = {**channel("general", 0.31, [0.33, 0.29]), **channel("feed_in", 0.08, [0.07, -0.01])}
    config = forecast_source(
        "prices",
        {
            "from": "amber",
            "price": "sensor.home_general_price",
            "forecast": "sensor.home_general_forecast",
            "feed_in_price": "sensor.home_feed_in_price",
            "feed_in_forecast": "sensor.home_feed_in_forecast",
        },
    )
    assert isinstance(config, Amber)
    found = parse_amber(states, config)
    assert [(i.value.p_import, i.value.p_export) for i in found.intervals] == [
        (0.31, 0.08),
        (0.33, 0.07),
        (0.29, -0.01),
    ]
    # Amber's 20:00:01 start is its interval's 20:00 boundary: the intervals tile
    assert [i.start for i in found.intervals] == [_at(10), _at(10, 30), _at(11)]
    assert align(found, [_at(10), _at(10, 10)], timedelta(minutes=10), mean=price_mean) == (
        Price(0.31, 0.08),
        Price(0.31, 0.08),
    )
    assert found.issued_at == datetime(2026, 10, 22, 10, 0, 4, tzinfo=UTC)


def test_tibber_quarter_hours_last_until_the_next() -> None:
    response = {
        "prices": {
            "Home": [
                {"start_time": "2026-10-22T12:00:00.000+02:00", "price": 0.2512},
                {"start_time": "2026-10-22T12:15:00.000+02:00", "price": 0.2633},
                {"start_time": "2026-10-22T12:30:00.000+02:00", "price": 0.2401},
            ]
        }
    }
    found = parse_tibber(response, Tibber(export=0.0), now=NOW)
    assert _prices(found) == [
        (_at(10), _at(10, 15), 0.2512, 0.0),
        (_at(10, 15), _at(10, 30), 0.2633, 0.0),
        (_at(10, 30), _at(10, 45), 0.2401, 0.0),
    ]
    two = {"prices": {"Home": response["prices"]["Home"], "Cabin": []}}
    with pytest.raises(TibberError, match="several homes"):
        parse_tibber(two, Tibber(export=0.0), now=NOW)
    assert parse_tibber(two, Tibber(export=0.0, home="Home"), now=NOW) == found


def _octopus(
    meter: str, day: int, values: list[float], *, as_objects: bool = False
) -> dict[str, Any]:
    start = datetime(2026, 10, day, tzinfo=ZoneInfo("Europe/London"))
    rates = []
    for index, value in enumerate(values):
        begin = start + index * timedelta(minutes=30)
        end = begin + timedelta(minutes=30)
        rates.append(
            {
                "start": begin if as_objects else begin.isoformat(),
                "end": end if as_objects else end.isoformat(),
                "value_inc_vat": value,
                "is_capped": False,
            }
        )
    return {
        "state": "2026-10-22T00:00:00+00:00",
        "last_updated": f"2026-10-{day - 1 if day > 22 else day}T16:00:00+00:00",
        "attributes": {
            "rates": rates,
            "mpan": meter,
            "event_type": "octopus_energy_electricity_current_day_rates",
        },
    }


def test_octopus_reads_each_meters_rates_and_tomorrows_when_published() -> None:
    imp = "event.octopus_energy_electricity_s1_m1_current_day_rates"
    imp_next = "event.octopus_energy_electricity_s1_m1_next_day_rates"
    exp = "event.octopus_energy_electricity_s1_m2_export_current_day_rates"
    states = {
        imp: _octopus("m1", 22, [0.2451, 0.1502], as_objects=True),  # inside HA: datetimes
        imp_next: {
            "state": "unknown",
            "last_updated": "2026-10-22T00:00:00+00:00",
            "attributes": {},
        },
        exp: _octopus("m2", 22, [0.15, 0.15]),
    }
    config = Octopus((imp, imp_next), (exp,))
    found = parse_octopus(states, config, now=NOW)
    # midnight in London on 22 October (BST) is 23:00 UTC on the 21st
    assert _prices(found) == [
        (
            datetime(2026, 10, 21, 23, tzinfo=UTC),
            datetime(2026, 10, 21, 23, 30, tzinfo=UTC),
            0.2451,
            0.15,
        ),
        (
            datetime(2026, 10, 21, 23, 30, tzinfo=UTC),
            datetime(2026, 10, 22, 0, tzinfo=UTC),
            0.1502,
            0.15,
        ),
    ]
    states[imp_next] = _octopus("m1", 23, [0.30])
    longer = asyncio.run(fetch_octopus(FakeApi(states), config, now=NOW))
    assert len(longer.intervals) == 2  # the export holds no rate for tomorrow
    fixed = parse_octopus(states, Octopus((imp, imp_next), export=0.15), now=NOW)
    assert len(fixed.intervals) == 3


def test_entsoe_prices_in_its_unit_with_space_separated_times() -> None:
    state = {
        "state": "0.21",
        "last_updated": "2026-10-22T09:58:00+00:00",
        "attributes": {
            "unit_of_measurement": "EUR/MWh",
            "prices": [
                {"time": "2026-10-22 12:00:00+02:00", "price": 210.0},
                {"time": "2026-10-22 13:00:00+02:00", "price": 190.0},
            ],
        },
    }
    found = parse_entsoe(state, Entsoe("entsoe.average_price", export=0.01), now=NOW)
    assert _prices(found) == [
        (_at(10), _at(11), 0.21, 0.01),
        (_at(11), _at(12), 0.19, 0.01),
    ]
    assert found.issued_at == NOW  # the prices the integration holds now


def _openei(change: str, *, adjustment: str = "0.012") -> dict[str, dict[str, Any]]:
    return {
        "sensor.plan_current_energy_rate": {"state": "0.41", "attributes": {}},
        "sensor.plan_current_energy_adjustment": {"state": adjustment, "attributes": {}},
        "sensor.plan_current_energy_sell_rate": {"state": "0.05", "attributes": {}},
        "sensor.plan_next_energy_rate_structure_time": {"state": change, "attributes": {}},
    }


def test_openei_holds_the_rate_in_force_until_it_changes() -> None:
    config = OpenEI(
        "sensor.plan_current_energy_rate",
        "sensor.plan_next_energy_rate_structure_time",
        adjustment="sensor.plan_current_energy_adjustment",
        sell_rate="sensor.plan_current_energy_sell_rate",
    )
    # 03:03 in Los Angeles; the rate changes at 16:00 local (23:00 UTC)
    found = parse_openei(_openei("2026-10-22 16:00:00"), config, now=NOW, horizon=48 * H, tz=LA)
    assert _prices(found) == [(_at(10), _at(23), 0.422, 0.05)]
    # no adjustment published: the rate alone; no change to come: the whole horizon
    alone = parse_openei(
        _openei("unknown", adjustment="unknown"), config, now=NOW, horizon=48 * H, tz=LA
    )
    assert _prices(alone) == [(_at(10), NOW + 48 * H, 0.41, 0.05)]
    with pytest.raises(OpenEIError, match="is past"):
        parse_openei(_openei("2026-10-22 02:00:00"), config, now=NOW, horizon=48 * H, tz=LA)


# -- the generic readers -------------------------------------------------------------------


def test_an_action_response_is_read_by_its_rows_path() -> None:
    config = forecast_source(
        "prices",
        {
            "from": "action",
            "action": "rates.get_forecast",
            "data": {"days": 2},
            "rows": ["forecast", "rows"],
            "start_key": "from",
            "period_minutes": 30,
            "keys": {"import": "cost"},
            "unit": "EUR/MWh",
            "export": 0.03,
        },
    )
    assert isinstance(config, ActionForecast)
    response = {"forecast": {"rows": [{"from": "2026-10-22T10:00:00Z", "cost": 150}]}}
    found = parse_response(response, config, "prices", now=NOW)
    assert _prices(found) == [(_at(10), _at(10, 30), 0.15, 0.03)]
    assert found.intervals[0].end == _at(10, 30)
    api = FakeApi(answers={("rates", "get_forecast"): response})
    engine = load_setup(EXAMPLE).engine.config
    read = asyncio.run(read_forecast(api, config, "prices", engine, NOW, tz=LA))
    assert read == found
    assert api.calls == [("rates", "get_forecast", {"days": 2})]
    assert source_name(config) == "action"


def test_an_entitys_import_alone_takes_a_fixed_export() -> None:
    config = EntityForecast("sensor.rates", "rates", keys={"import": "buy"}, export=0.04)
    state = {
        "state": "ok",
        "last_updated": "2026-10-22T10:00:00+00:00",
        "attributes": {
            "rates": [{"start": "2026-10-22T10:00:00Z", "end": "2026-10-22T11:00:00Z", "buy": 0.3}]
        },
    }
    found = parse_forecast(state, config, "prices")
    assert found.intervals[0].value == Price(0.3, 0.04)


def test_an_action_reads_a_power_forecast_in_its_unit() -> None:
    config = forecast_source(
        "load_forecast",
        {
            "from": "action",
            "action": "forecaster.load",
            "rows": ["rows"],
            "keys": {"value": "w"},
            "unit": "W",
        },
    )
    assert isinstance(config, ActionForecast)
    response = {
        "rows": [{"start": "2026-10-22T10:00:00Z", "end": "2026-10-22T10:30:00Z", "w": 1500}]
    }
    found = parse_response(response, config, "load_forecast", now=NOW)
    assert found.intervals[0].value == 1.5
    with pytest.raises(SourcesConfigError, match=r"pv_forecast\.unit: is required"):
        forecast_source("pv_forecast", {"from": "action", "action": "a.b", "keys": {"value": "w"}})
    with pytest.raises(SourcesConfigError, match=r"load_forecast\.export: is not a setting"):
        forecast_source(
            "load_forecast",
            {
                "from": "entity",
                "entity": "e",
                "attribute": "a",
                "keys": {"value": "w"},
                "export": 1,
            },
        )


def test_a_price_section_names_every_problem() -> None:
    cases: list[tuple[dict[str, object], list[str]]] = [
        ({"from": "nordpool"}, ["prices.area: is required", "prices.export: is required"]),
        (
            {"from": "amber", "price": "a", "forecast": "b", "feed_in_price": "c"},
            ["feed_in_price and feed_in_forecast"],
        ),
        ({"from": "amber", "price": "a", "forecast": "b"}, ["or a fixed export"]),
        ({"from": "octopus", "entities": []}, ["prices.entities: must list"]),
        (
            {
                "from": "openei",
                "current_rate": "a",
                "next_structure_time": "b",
                "sell_rate": "c",
                "export": 0.1,
            },
            ["sell_rate or a fixed export"],
        ),
        ({"from": "tibber", "export": "cheap"}, ["prices.export: must be a price"]),
        ({"from": "action", "action": "rates", "keys": {"import": "c"}}, ["must be domain.action"]),
        (
            {"from": "entity", "entity": "e", "attribute": "a", "keys": {"import": "c"}},
            ["keys.export or a fixed export"],
        ),
    ]
    for data, messages in cases:
        with pytest.raises(SourcesConfigError) as refused:
            forecast_source("prices", data)
        for message in messages:
            assert message in str(refused.value), (data, str(refused.value))


def test_each_reader_is_configured_by_its_from() -> None:
    sections: list[dict[str, object]] = [
        {"from": "nordpool", "area": "se3", "export": 0},
        {"from": "tibber", "export": 0},
        {"from": "octopus", "entities": ["event.a"], "export": 0},
        {"from": "entsoe", "entity": "entsoe.avg", "export": 0},
        {"from": "openei", "current_rate": "a", "next_structure_time": "b", "export": 0},
    ]
    for data in sections:
        source = forecast_source("prices", data)
        assert source_name(source) == data["from"]
    nordpool = forecast_source("prices", sections[0])
    assert isinstance(nordpool, NordPool) and nordpool.area == "SE3"


def test_the_readers_import_without_an_http_client() -> None:
    names = "action amber entity entsoe nordpool octopus openei prices rows tariffkit tibber"
    code = (
        "import sys\n"
        + "".join(f"import microgrid.cli.sources.{name}\n" for name in names.split())
        + "assert 'httpx' not in sys.modules, 'httpx'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).parent)
