"""Onboarding: the sources the installed integrations publish, found and read at once;
the measured entities the Energy dashboard names; each meter's sign from the
recorder's history; a plant and a ``sources/1`` file imported; one entry per plant."""

from datetime import timedelta
from typing import Any

import pytest
from custom_components.microgrid import config_flow
from custom_components.microgrid.const import (
    CONF_LOAD_FORECAST,
    CONF_MEASURED,
    CONF_PLANT,
    CONF_PRICES,
    CONF_PV_FORECAST,
    DOMAIN,
)
from custom_components.microgrid.discovery import (
    changed_signs,
    energy_found,
    read_signs,
    with_signs,
)
from custom_components.microgrid.found_sources import EXPORT, found_sources
from custom_components.microgrid.setup import read_plant
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.energy.data import async_get_manager
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from ha_fixtures import HOUR, NOW, REPO, plant_data, set_sources

LOADS = pytest.mark.usefixtures("enable_custom_integrations")

PLANT_TOML = (REPO / "examples" / "dataset" / "plant.toml").read_text(encoding="utf-8")
MEASURED_FORM = {
    "meter_pv": "sensor.pv_power",
    "meter_grid": "sensor.grid_power",
    "meter_battery": "sensor.battery_power",
    "soc": "sensor.battery_soc",
    "house": "sensor.house_power",
    "dispatch_as_modelled": True,
}
SOURCES_TOML = """
schema = "sources/1"
plant = "plant.toml"

[home_assistant]
url = "http://homeassistant.local:8123"

[prices]
from = "entity"
entity = "sensor.rates"
attribute = "rates"
keys = { import = "buy", export = "sell" }

[load_forecast]
from = "entity"
entity = "sensor.load_forecast"
attribute = "forecast"
keys = { value = "kw" }

[measured]
from = "home_assistant"
soc = "sensor.battery_soc"
house = "sensor.house_power"
dispatch_as_modelled = true
meters = { pv = "sensor.pv_power", grid = "sensor.grid_power", battery = "sensor.battery_power" }
"""


def _emhass(hass: HomeAssistant, entity: str = "sensor.p_load_forecast_48h") -> None:
    """EMHASS's load forecast, its rows keyed by the entity's own object id."""
    key = entity.removeprefix("sensor.")
    start = NOW.replace(minute=0)
    rows = [
        {"date": (start + i * timedelta(minutes=30)).isoformat(), key: "800.0"} for i in range(96)
    ]
    hass.states.async_set(entity, "800", {"forecasts": rows, "unit_of_measurement": "W"})


def test_the_installed_integrations_sources_are_found(hass: HomeAssistant) -> None:
    registry = er.async_get(hass)

    def entity(domain: str, platform: str, unique_id: str, **kwargs: Any) -> str:
        return registry.async_get_or_create(domain, platform, unique_id, **kwargs).entity_id

    MockConfigEntry(domain="tariffkit", title="E-ELEC").add_to_hass(hass)
    nordpool = MockConfigEntry(domain="nordpool", data={"areas": ["SE3", "SE4"]})
    nordpool.add_to_hass(hass)
    general = entity("sensor", "amberelectric", "site-current-general")
    forecast = entity("sensor", "amberelectric", "site-forecasts-general")
    feed_price = entity("sensor", "amberelectric", "site-current-feed_in")
    feed_forecast = entity("sensor", "amberelectric", "site-forecasts-feed_in")
    today = entity("event", "octopus_energy", "octopus_energy_electricity_s_m_current_day_rates")
    tomorrow = entity("event", "octopus_energy", "octopus_energy_electricity_s_m_next_day_rates")
    entsoe = entity("sensor", "entsoe", "entsoe.avg_price")
    openei = MockConfigEntry(domain="openei", title="PG&E")
    openei.add_to_hass(hass)
    rate = entity("sensor", "openei", f"current_rate_{openei.entry_id}", config_entry=openei)
    change = entity(
        "sensor",
        "openei",
        f"next_energy_rate_structure_time_{openei.entry_id}",
        config_entry=openei,
    )
    sell = entity("sensor", "openei", f"current_sell_rate_{openei.entry_id}", config_entry=openei)
    hass.states.async_set(sell, "unknown")  # no sell rate: an export price is needed

    prices = {f.label: f for f in found_sources(hass, CONF_PRICES)}
    assert prices["TariffKit: E-ELEC"].section["from"] == "tariffkit"
    assert prices["TariffKit: E-ELEC"].missing == ()
    assert prices["Nord Pool: SE4"].section == {
        "from": "nordpool",
        "area": "SE4",
        "config_entry": nordpool.entry_id,
    }
    assert prices["Nord Pool: SE4"].missing == (EXPORT,)
    amber = prices[f"Amber Electric: {general}"]
    assert amber.section == {
        "from": "amber",
        "price": general,
        "forecast": forecast,
        "feed_in_price": feed_price,
        "feed_in_forecast": feed_forecast,
    }
    assert amber.missing == ()
    octopus = prices["Octopus Energy: s_m"]
    assert octopus.section == {"from": "octopus", "entities": [today, tomorrow]}
    assert octopus.missing == (EXPORT,)
    assert prices[f"ENTSO-E: {entsoe}"].section == {"from": "entsoe", "entity": entsoe}
    assert prices["OpenEI: PG&E"].section == {
        "from": "openei",
        "current_rate": rate,
        "next_structure_time": change,
    }
    assert prices["OpenEI: PG&E"].missing == (EXPORT,)

    days = [
        entity("sensor", "solcast_solar", key)
        for key in ("total_kwh_forecast_today", "total_kwh_forecast_tomorrow")
    ]
    polled = entity("sensor", "solcast_solar", "lastupdated")
    (solcast,) = found_sources(hass, CONF_PV_FORECAST)
    assert solcast.section == {"from": "solcast", "entities": days, "last_polled": polled}

    _emhass(hass)
    _emhass(hass, "sensor.p_pv_forecast")  # published the same way: not a load
    hass.states.async_set("predbat.load_energy_predicted", "3.2", {"results": {}})
    loads = {f.label: f.section for f in found_sources(hass, CONF_LOAD_FORECAST)}
    assert len(loads) == 2
    assert loads["EMHASS: sensor.p_load_forecast_48h"] == {
        "from": "emhass",
        "entity": "sensor.p_load_forecast_48h",
        "value_key": "p_load_forecast_48h",
        "period_minutes": 30,
    }
    assert loads["Predbat: predbat.load_energy_predicted"] == {
        "from": "predbat",
        "entity": "predbat.load_energy_predicted",
    }


async def _to_load_forecast(hass: HomeAssistant) -> str:
    """A flow through the plant, what it measures, the prices and no PV forecast."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow, {"next_step_id": "plant_paste"})
    await hass.config_entries.flow.async_configure(flow, {"name": "Home", "plant_toml": PLANT_TOML})
    await hass.config_entries.flow.async_configure(flow, MEASURED_FORM)
    await hass.config_entries.flow.async_configure(flow, {"from": "entity"})
    await hass.config_entries.flow.async_configure(
        flow,
        {"entity": "sensor.rates", "attribute": "rates", "import_key": "buy", "export_key": "sell"},
    )
    result = await hass.config_entries.flow.async_configure(flow, {"from": "none"})
    assert result["step_id"] == "load_forecast"
    return str(flow)


@LOADS
async def test_a_source_found_is_proposed_and_read_at_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    set_sources(hass)
    _emhass(hass)
    flow = await _to_load_forecast(hass)
    shown = hass.config_entries.flow.async_get(flow)
    assert shown["step_id"] == "load_forecast"
    result = await hass.config_entries.flow.async_configure(flow, {"from": "found:0"})
    assert result["type"] is FlowResultType.CREATE_ENTRY  # nothing more to give
    assert result["options"][CONF_LOAD_FORECAST]["value_key"] == "p_load_forecast_48h"


@LOADS
async def test_a_source_found_that_needs_an_export_price_asks_for_it(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    set_sources(hass)
    MockConfigEntry(domain="nordpool", data={"areas": ["SE3"]}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow, {"next_step_id": "plant_paste"})
    await hass.config_entries.flow.async_configure(flow, {"name": "Home", "plant_toml": PLANT_TOML})
    result = await hass.config_entries.flow.async_configure(flow, MEASURED_FORM)
    assert result["step_id"] == "prices"
    schema = result["data_schema"]
    assert schema is not None and schema({})["from"] == "found:0"  # proposed
    result = await hass.config_entries.flow.async_configure(flow, {"from": "found:0"})
    assert result["step_id"] == "prices_source"
    shown = result["data_schema"]
    assert shown is not None
    area = next(key for key in shown.schema if key == "area")
    assert area.description == {"suggested_value": "SE3"}


@LOADS
async def test_the_energy_dashboard_proposes_what_is_measured(hass: HomeAssistant) -> None:
    set_sources(hass)
    manager = await async_get_manager(hass)
    await manager.async_update(
        {
            "device_consumption": [],
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.grid_in",
                    "stat_energy_to": None,
                    "stat_cost": None,
                    "entity_energy_price": None,
                    "number_energy_price": None,
                    "stat_compensation": None,
                    "entity_energy_price_export": None,
                    "number_energy_price_export": None,
                    "cost_adjustment_day": 0.0,
                    "stat_rate": "sensor.grid_power",
                },
                {
                    "type": "solar",
                    "stat_energy_from": "sensor.pv_energy",
                    "stat_rate": "sensor.pv_power",
                    "config_entry_solar_forecast": None,
                },
                {
                    "type": "battery",
                    "stat_energy_from": "sensor.battery_out",
                    "stat_energy_to": "sensor.battery_in",
                    "stat_rate": "sensor.battery_power",
                    "stat_soc": "sensor.battery_soc",
                    "capacity": 12.5,
                },
            ],
        }
    )
    found = await energy_found(hass)
    assert (found.pv, found.grid, found.battery, found.soc) == (
        "sensor.pv_power",
        "sensor.grid_power",
        "sensor.battery_power",
        "sensor.battery_soc",
    )
    assert found.capacity_kwh == 12.5

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow, {"next_step_id": "plant_paste"})
    result = await hass.config_entries.flow.async_configure(
        flow, {"name": "Home", "plant_toml": PLANT_TOML}
    )
    assert result["step_id"] == "measured"
    schema = result["data_schema"]
    assert schema is not None
    suggested = {
        str(key): key.description["suggested_value"]
        for key in schema.schema
        if getattr(key, "description", None)
    }
    assert suggested["meter_grid"] == "sensor.grid_power"
    assert suggested["meter_battery"] == "sensor.battery_power"
    assert suggested["soc"] == "sensor.battery_soc"
    # Kept, the dashboard's battery sensor reads positive discharging: its sign is set,
    # with no history to read.
    result = await hass.config_entries.flow.async_configure(flow, MEASURED_FORM)
    assert result["step_id"] == "signs"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["notes"] == (
        "- battery is the Energy dashboard's battery power, positive discharging: sign -1"
    )


async def test_signs_are_told_from_the_recorders_history(
    recorder_mock: object, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Two days of a grid meter positive on export and a battery meter positive
    discharging (as the Energy dashboard reads them): both found backwards."""
    import math

    start = NOW.replace(minute=0) - 50 * HOUR
    stored = 0.4
    for hour in range(51):
        at = start + hour * HOUR
        h = at.hour
        made = max(0.0, 4.0 * math.sin(math.pi * (h - 6) / 12)) if 6 <= h <= 18 else 0.0
        drew = 0.6 + (1.5 if 18 <= h < 21 else 0.0)
        charge = 0.0
        if made > drew and stored < 0.95:
            charge = min(made - drew, 3.0)
        elif made < drew and h >= 17 and stored > 0.2:
            charge = -min(drew - made, 2.0)
        stored = min(1.0, max(0.0, stored + charge / 12.0))
        freezer.move_to(at)
        for entity, kw in (
            ("sensor.pv_power", made),
            ("sensor.house_power", drew),
            ("sensor.grid_power", -(drew + charge - made)),  # positive exporting
            ("sensor.battery_power", -charge),  # positive discharging
        ):
            hass.states.async_set(entity, f"{kw * 1000:.1f}", {"unit_of_measurement": "W"})
        hass.states.async_set("sensor.house_energy", f"{hour:.1f}", {"unit_of_measurement": "kWh"})
        hass.states.async_set(
            "sensor.battery_soc", f"{stored * 100:.2f}", {"unit_of_measurement": "%"}
        )
        await async_wait_recording_done(hass)
    freezer.move_to(start + 50 * HOUR + timedelta(minutes=5))
    plant = read_plant(plant_data(), "UTC")
    section = config_flow.measured_from_form(plant, MEASURED_FORM)
    signs = await read_signs(hass, plant, section)
    assert signs is not None, "nothing read"
    proposed = {name: (sign, signs.why[name]) for name, sign in signs.found.items()}
    changed = {name: sign for name, (sign, _) in changed_signs(plant, proposed).items()}
    assert changed == {"grid": -1.0, "battery": -1.0}, signs.undecided
    meters = {m["name"]: m for m in with_signs(plant_data(), plant, changed)["meters"]}
    assert meters["grid"]["sign"] == -1.0
    assert meters["battery"]["sign"] == -1.0
    assert meters["pv"].get("sign", 1.0) == 1.0
    # A house entity in energy, not power: no signs told, and nothing raised.
    energy_house = {**section, "house": "sensor.house_energy"}
    assert await read_signs(hass, plant, energy_house) is None
    # Under a day of hours recorded: too little to tell.
    freezer.move_to(start + 10 * HOUR)
    assert await read_signs(hass, plant, section) is None


@LOADS
async def test_signs_the_records_contradict_are_confirmed_then_applied(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    freezer.move_to(NOW)
    set_sources(hass)

    async def backwards(*_: object) -> dict[str, tuple[float, str]]:
        return {
            "grid": (-1.0, "grid reads negative while importing (the balance)"),
            "pv": (1.0, "pv reads positive when it makes power"),
        }

    monkeypatch.setattr(config_flow, "proposed_signs", backwards)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow, {"next_step_id": "plant_paste"})
    await hass.config_entries.flow.async_configure(flow, {"name": "Home", "plant_toml": PLANT_TOML})
    result = await hass.config_entries.flow.async_configure(flow, MEASURED_FORM)
    assert result["step_id"] == "signs"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["notes"] == "- grid reads negative while importing (the balance): sign -1"
    result = await hass.config_entries.flow.async_configure(flow, {"apply": True})
    assert result["step_id"] == "prices"
    await hass.config_entries.flow.async_configure(flow, {"from": "entity"})
    await hass.config_entries.flow.async_configure(
        flow,
        {"entity": "sensor.rates", "attribute": "rates", "import_key": "buy", "export_key": "sell"},
    )
    await hass.config_entries.flow.async_configure(flow, {"from": "none"})
    result = await hass.config_entries.flow.async_configure(flow, {"from": "none"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    meters = {m["name"]: m for m in result["data"][CONF_PLANT]["meters"]}
    assert meters["grid"]["sign"] == -1.0


@LOADS
async def test_a_plant_and_a_sources_file_make_an_entry_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    set_sources(hass)

    async def files() -> Any:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        flow = result["flow_id"]
        await hass.config_entries.flow.async_configure(flow, {"next_step_id": "files"})
        return await hass.config_entries.flow.async_configure(
            flow, {"name": "Home", "plant_toml": PLANT_TOML, "sources_toml": SOURCES_TOML}
        )

    result = await files()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = result["options"]
    assert options[CONF_PRICES]["entity"] == "sensor.rates"
    assert options[CONF_MEASURED]["meters"]["grid"] == "sensor.grid_power"
    assert "home_assistant" not in options
    again = await files()
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "already_configured"

    # The grid meter changed in the options: the plant is known by the new one.
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.unique_id == "sensor.grid_power"
    hass.states.async_set("sensor.grid_power_2", "-300", {"unit_of_measurement": "W"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "measured"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**MEASURED_FORM, "meter_grid": "sensor.grid_power_2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.unique_id == "sensor.grid_power_2"
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)


@LOADS
async def test_a_sources_file_without_prices_is_refused(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    set_sources(hass)
    start = SOURCES_TOML.index("[prices]")
    end = SOURCES_TOML.index("[load_forecast]")
    no_prices = SOURCES_TOML[:start] + SOURCES_TOML[end:]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow = result["flow_id"]
    await hass.config_entries.flow.async_configure(flow, {"next_step_id": "files"})
    result = await hass.config_entries.flow.async_configure(
        flow, {"name": "Home", "plant_toml": PLANT_TOML, "sources_toml": no_prices}
    )
    assert result["errors"] == {"base": "invalid"}
    placeholders = result["description_placeholders"]
    assert placeholders is not None and "prices" in placeholders["problems"]


def test_emhass_names_its_own_key() -> None:
    form = {"entity": "sensor.p_load_forecast_48h", "value_key": "p_load_forecast_48h"}
    section = config_flow.section_from_form(CONF_LOAD_FORECAST, "emhass", form)
    assert section == {"from": "emhass", **form}
    assert config_flow.form_values(CONF_LOAD_FORECAST, "emhass", section) == form
