"""Synthetic states and a config entry for the integration's tests: a plant (the example
dataset's), prices and forecasts as entities' rows, and the plant's meters."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from custom_components.microgrid.const import (
    CONF_ENGINE,
    CONF_LOAD_FORECAST,
    CONF_MEASURED,
    CONF_PLANT,
    CONF_PRICES,
    CONF_PV_FORECAST,
    DOMAIN,
)
from homeassistant.components.energy.data import EnergyPreferencesUpdate, async_get_manager
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from microgrid.cli.offline import read_plant

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 22, 10, 3, tzinfo=UTC)
HOUR = timedelta(hours=1)


def plant_data() -> dict[str, Any]:
    """The example dataset's plant (an AC array, a battery on a hybrid's DC bus)."""
    return read_plant(REPO / "examples" / "dataset" / "plant.toml").to_mapping()


def rows(start: datetime, hours: int, **values: float) -> list[dict[str, Any]]:
    return [
        {
            "start": (start + i * HOUR).isoformat(),
            "end": (start + (i + 1) * HOUR).isoformat(),
            **values,
        }
        for i in range(hours)
    ]


def set_sources(hass: HomeAssistant, *, soc: str = "55") -> None:
    """Prices, a PV and a load forecast as entities' rows, and the plant's meters."""
    start = NOW.replace(minute=0)
    hass.states.async_set(
        "sensor.rates",
        "ok",
        {"rates": rows(start, 50, buy=0.30, sell=0.05), "unit_of_measurement": "EUR/kWh"},
    )
    hass.states.async_set(
        "sensor.pv_forecast",
        "ok",
        {"forecast": rows(start, 50, low=0.5, mid=1.5, high=2.5), "unit_of_measurement": "kW"},
    )
    hass.states.async_set(
        "sensor.load_forecast",
        "ok",
        {"forecast": rows(start, 50, kw=0.8), "unit_of_measurement": "kW"},
    )
    for entity, state in (
        ("sensor.pv_power", "1200"),
        ("sensor.grid_power", "-300"),
        ("sensor.battery_power", "100"),
        ("sensor.house_power", "800"),
    ):
        hass.states.async_set(entity, state, {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.battery_soc", soc, {"unit_of_measurement": "%"})


def options() -> dict[str, Any]:
    return {
        CONF_ENGINE: {},
        CONF_PRICES: {
            "from": "entity",
            "entity": "sensor.rates",
            "attribute": "rates",
            "keys": {"import": "buy", "export": "sell"},
        },
        CONF_PV_FORECAST: {
            "from": "entity",
            "entity": "sensor.pv_forecast",
            "attribute": "forecast",
            "keys": {"p10": "low", "p50": "mid", "p90": "high"},
        },
        CONF_LOAD_FORECAST: {
            "from": "entity",
            "entity": "sensor.load_forecast",
            "attribute": "forecast",
            "keys": {"value": "kw"},
        },
        CONF_MEASURED: {
            "from": "home_assistant",
            "soc": "sensor.battery_soc",
            "house": "sensor.house_power",
            "meters": {
                "pv": "sensor.pv_power",
                "grid": "sensor.grid_power",
                "battery": "sensor.battery_power",
            },
            "dispatch_as_modelled": True,
        },
    }


def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, title="Home", data={CONF_PLANT: plant_data()}, options=options()
    )


async def setup(hass: HomeAssistant, found: MockConfigEntry) -> None:
    found.add_to_hass(hass)
    assert await hass.config_entries.async_setup(found.entry_id)
    await hass.async_block_till_done()


def entity_id(
    hass: HomeAssistant, found: MockConfigEntry, key: str, platform: str = "sensor"
) -> str:
    registry = er.async_get(hass)
    found_id = registry.async_get_entity_id(platform, DOMAIN, f"{found.entry_id}_{key}")
    assert found_id is not None, key
    return found_id


async def energy_prefs(hass: HomeAssistant, *, capacity: float | None = None) -> None:
    """The Energy dashboard naming the grid's, the PV's and the battery's power sensors
    and the battery's SOC (and capacity)."""
    battery: dict[str, Any] = {
        "type": "battery",
        "stat_energy_from": "sensor.battery_out",
        "stat_energy_to": "sensor.battery_in",
        "stat_rate": "sensor.battery_power",
        "stat_soc": "sensor.battery_soc",
    }
    if capacity is not None:
        battery["capacity"] = capacity
    prefs: dict[str, Any] = {
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
            battery,
        ],
    }
    manager = await async_get_manager(hass)
    await manager.async_update(cast("EnergyPreferencesUpdate", prefs))
