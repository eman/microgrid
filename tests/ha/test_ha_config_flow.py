"""The config flow and the options: a plant pasted or built, what it measures, each
source checked by the library and read once."""

from typing import Any

import pytest
from custom_components.microgrid.config_flow import section_from_form
from custom_components.microgrid.const import (
    CONF_ENGINE,
    CONF_LOAD_FORECAST,
    CONF_MEASURED,
    CONF_PLANT,
    CONF_PRICES,
    CONF_PV_FORECAST,
    DOMAIN,
)
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from ha_fixtures import NOW, REPO, entry, set_sources

PLANT_TOML = (REPO / "examples" / "dataset" / "plant.toml").read_text(encoding="utf-8")

MEASURED = {
    "meter_pv": "sensor.pv_power",
    "meter_grid": "sensor.grid_power",
    "meter_battery": "sensor.battery_power",
    "soc": "sensor.battery_soc",
    "house": "sensor.house_power",
    "dispatch_as_modelled": True,
}
PRICES = {"entity": "sensor.rates", "attribute": "rates", "import_key": "buy", "export_key": "sell"}

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def frozen(freezer: FrozenDateTimeFactory) -> None:
    freezer.move_to(NOW)


async def _step(hass: HomeAssistant, flow_id: str, data: dict[str, Any]) -> Any:
    return await hass.config_entries.flow.async_configure(flow_id, data)


async def test_a_pasted_plant_and_its_sources_make_an_entry(hass: HomeAssistant) -> None:
    set_sources(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    flow = result["flow_id"]
    result = await _step(hass, flow, {"next_step_id": "plant_paste"})
    result = await _step(hass, flow, {"name": "Home", "plant_toml": PLANT_TOML})
    assert result["step_id"] == "measured"
    result = await _step(hass, flow, MEASURED)
    assert result["step_id"] == "prices"
    result = await _step(hass, flow, {"from": "entity"})
    assert result["step_id"] == "prices_source"
    result = await _step(hass, flow, PRICES)
    assert result["step_id"] == "pv_forecast"
    result = await _step(hass, flow, {"from": "none"})
    assert result["step_id"] == "load_forecast"
    result = await _step(hass, flow, {"from": "entity"})
    result = await _step(
        hass, flow, {"entity": "sensor.load_forecast", "attribute": "forecast", "value_key": "kw"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"][CONF_PLANT]["battery"]["capacity_kwh"] == 12.0
    options = result["options"]
    assert options[CONF_PRICES] == {
        "from": "entity",
        "entity": "sensor.rates",
        "attribute": "rates",
        "keys": {"import": "buy", "export": "sell"},
    }
    assert CONF_PV_FORECAST not in options
    assert options[CONF_LOAD_FORECAST]["keys"] == {"value": "kw"}
    assert options[CONF_MEASURED]["meters"]["battery"] == "sensor.battery_power"
    assert options[CONF_MEASURED]["dispatch_as_modelled"] is True


async def test_each_problem_is_named_and_a_source_is_read_before_it_is_kept(
    hass: HomeAssistant,
) -> None:
    set_sources(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow = result["flow_id"]
    await _step(hass, flow, {"next_step_id": "plant_paste"})
    result = await _step(
        hass, flow, {"name": "Home", "plant_toml": PLANT_TOML.replace("12.0", "-12.0")}
    )
    assert result["errors"] == {"base": "invalid"}
    assert "capacity_kwh" in result["description_placeholders"]["problems"]
    result = await _step(hass, flow, {"name": "Home", "plant_toml": "not = [toml"})
    assert result["errors"] == {"base": "invalid"}
    result = await _step(hass, flow, {"name": "Home", "plant_toml": PLANT_TOML})
    result = await _step(hass, flow, {k: v for k, v in MEASURED.items() if k != "soc"})
    assert result["errors"] == {"base": "invalid"}
    assert "SOC" in result["description_placeholders"]["problems"]
    result = await _step(hass, flow, MEASURED)
    result = await _step(hass, flow, {"from": "nordpool"})
    result = await _step(hass, flow, {"area": "SE3", "export": 0.0})
    assert result["errors"] == {"base": "read_failed"}
    assert "nordpool" in result["description_placeholders"]["error"]


async def test_a_plant_built_step_by_step(hass: HomeAssistant) -> None:
    set_sources(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    flow = result["flow_id"]
    await _step(hass, flow, {"next_step_id": "plant_build"})
    result = await _step(
        hass, flow, {"name": "Home", "pv": "ac", "battery": "dc_bus", "dc_load": False}
    )
    assert result["step_id"] == "plant_battery"
    result = await _step(
        hass,
        flow,
        {"capacity_kwh": 12, "charge_kw": 6, "discharge_kw": 6, "standby_kw": 0.03},
    )
    assert result["step_id"] == "plant_grid"
    result = await _step(hass, flow, {"export_limit_kw": 5})
    assert result["step_id"] == "measured"
    result = await _step(hass, flow, MEASURED)
    result = await _step(hass, flow, {"from": "entity"})
    result = await _step(hass, flow, PRICES)
    result = await _step(hass, flow, {"from": "none"})
    result = await _step(hass, flow, {"from": "none"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    plant = result["data"][CONF_PLANT]
    assert plant["pv"] == [{"name": "pv", "coupling": "ac"}]
    assert plant["battery"]["connection"] == "dc_bus"
    assert plant["hybrid"] == {"standby_kw": 0.03}
    assert plant["grid"] == {"export_limit_kw": 5.0}


async def test_the_options_edit_the_engine_and_the_plant(hass: HomeAssistant) -> None:
    set_sources(hass)
    found: MockConfigEntry = entry()
    found.add_to_hass(hass)
    assert await hass.config_entries.async_setup(found.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(found.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "engine"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"slot_minutes": 7}
    )
    assert result["errors"] == {"base": "invalid"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"slot_minutes": 15, "battery_value": "replacement"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert found.options[CONF_ENGINE] == {"slot_minutes": 15, "battery_value": "replacement"}
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(found.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "plant"}
    )
    schema = result["data_schema"]
    assert schema is not None
    text = schema({})["plant_toml"]
    assert "capacity_kwh = 12.0" in text
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"plant_toml": text.replace("12.0", "10.0")}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert found.data[CONF_PLANT]["battery"]["capacity_kwh"] == 10.0
    await hass.async_block_till_done()  # the reload the change starts
    assert found.runtime_data.setup.plant.battery.capacity_kwh == 10.0
    assert await hass.config_entries.async_unload(found.entry_id)


def test_a_form_becomes_its_section() -> None:
    assert section_from_form(
        CONF_PRICES,
        "action",
        {
            "action": "rates.get",
            "rows": "forecast.rows",
            "import_key": "cost",
            "period_minutes": 30.0,
            "export": 0.04,
        },
    ) == {
        "from": "action",
        "action": "rates.get",
        "rows": ["forecast", "rows"],
        "period_minutes": 30,
        "keys": {"import": "cost"},
        "export": 0.04,
    }
