"""The integration in Home Assistant: set up from a plant and its sources, it publishes a
valuation and a live value, answers ``microgrid.get_valuation``, and falls back where
an input cannot be used. Synthetic states throughout."""

from datetime import timedelta
from typing import Any

import pytest
from custom_components.microgrid.const import (
    CONF_PLANT,
    DOMAIN,
    FAILING_FOR_ISSUE,
    SERVICE_GET_VALUATION,
)
from custom_components.microgrid.diagnostics import async_get_config_entry_diagnostics
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from ha_fixtures import NOW, entity_id, entry, options, plant_data, rows, set_sources, setup

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
def frozen(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(NOW)
    return freezer


async def test_setup_publishes_a_valuation_and_a_live_value(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    await hass.config.async_update(time_zone="Europe/Berlin", currency="EUR")
    set_sources(hass)
    found = entry()
    await setup(hass, found)
    assert found.state is ConfigEntryState.LOADED

    valuation = hass.states.get(entity_id(hass, found, "valuation"))
    assert valuation is not None
    attributes = valuation.attributes
    # The battery's lots are not known (no recorder here): named, and the values stand.
    assert valuation.state == "stored_value_v1", (
        attributes.get("degraded"),
        attributes.get("inputs"),
        attributes.get("problems"),
    )
    assert attributes["reason"] == "forecast_basis"
    assert attributes["degraded"] == ["battery_lots"]
    assert attributes["slots"][0]["p_import"] == 0.30
    assert "battery_lots" in attributes["problems"]

    imported = hass.states.get(entity_id(hass, found, "import_price"))
    assert imported is not None
    assert float(imported.state) == 0.30
    assert imported.attributes["unit_of_measurement"] == "EUR/kWh"
    marginal = hass.states.get(entity_id(hass, found, "marginal_cost"))
    assert marginal is not None and marginal.state not in ("unknown", "unavailable")
    problem = hass.states.get(entity_id(hass, found, "problem", Platform.BINARY_SENSOR))
    assert problem is not None and problem.state == "off"

    answer = await hass.services.async_call(
        DOMAIN, SERVICE_GET_VALUATION, {}, blocking=True, return_response=True
    )
    assert isinstance(answer, dict)
    response: dict[str, Any] = answer
    assert response["valuation"]["kind"] == "stored_value_v1"
    assert response["valuation"]["slots"][0]["p_import"] == 0.30
    assert response["reason"] == "forecast_basis"
    assert response["live"]["kind"] == "stored_value_v1"

    diagnostics = await async_get_config_entry_diagnostics(hass, found)
    assert diagnostics["published"]["reason"] == "forecast_basis"
    assert diagnostics["sources"]["prices"]["entity"] == "**REDACTED**"


async def test_a_valuation_is_published_again_on_its_timer_and_on_new_prices(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    await hass.config.async_update(time_zone="Europe/Berlin")
    set_sources(hass)
    found = entry()
    await setup(hass, found)
    coordinator = found.runtime_data
    first = coordinator.data.published
    assert first is not None

    frozen.tick(timedelta(minutes=2))
    await coordinator.async_refresh()
    assert coordinator.data.published is first  # nothing new: no new valuation

    frozen.tick(timedelta(minutes=10))
    await coordinator.async_refresh()
    refreshed = coordinator.data.published
    assert refreshed is not first and refreshed.reason == "live"

    start = NOW.replace(minute=0)
    hass.states.async_set(
        "sensor.rates",
        "ok",
        {"rates": rows(start, 50, buy=0.40, sell=0.05), "unit_of_measurement": "EUR/kWh"},
    )
    frozen.tick(timedelta(minutes=5))
    await coordinator.async_refresh()
    repriced = coordinator.data.published
    assert repriced.reason == "forecast_basis"
    assert repriced.valuation.slots[-1].p_import == 0.40


async def test_an_input_that_cannot_be_used_is_the_fallback(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    await hass.config.async_update(time_zone="Europe/Berlin")
    set_sources(hass, soc="unavailable")
    found = entry()
    await setup(hass, found)
    valuation = hass.states.get(entity_id(hass, found, "valuation"))
    assert valuation is not None and valuation.state == "fallback_import"
    assert "battery_soc" in valuation.attributes["degraded"]
    problem = hass.states.get(entity_id(hass, found, "problem", Platform.BINARY_SENSOR))
    assert problem is not None and problem.state == "on"


async def test_without_prices_nothing_is_published_and_a_failing_source_is_a_repair(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    await hass.config.async_update(time_zone="Europe/Berlin")
    set_sources(hass)
    hass.states.async_remove("sensor.rates")
    found = entry()
    await setup(hass, found)
    valuation = hass.states.get(entity_id(hass, found, "valuation"))
    assert valuation is not None and valuation.state == "unavailable"
    assert "sensor.rates" in found.runtime_data.data.problems["prices"]
    coordinator = found.runtime_data
    for _ in range(FAILING_FOR_ISSUE // timedelta(minutes=5)):
        frozen.tick(timedelta(minutes=5))
        await coordinator.async_refresh()
    registry = ir.async_get(hass)
    issue = f"{found.entry_id}_prices"
    assert registry.async_get_issue(DOMAIN, issue) is not None
    set_sources(hass)
    frozen.tick(timedelta(minutes=1))  # a failed source is read again at the next minute
    await coordinator.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue) is None
    assert coordinator.data.published is not None

    # Raised again, then the entry reloaded (an options change): the new coordinator
    # counts afresh, so the old issue goes with the old one.
    hass.states.async_remove("sensor.rates")
    # The first failure is the first read after the removal: 30 minutes on from it.
    for _ in range(FAILING_FOR_ISSUE // timedelta(minutes=5) + 1):
        frozen.tick(timedelta(minutes=5))
        await coordinator.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue) is not None
    assert await hass.config_entries.async_reload(found.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, issue) is None
    for _ in range(FAILING_FOR_ISSUE // timedelta(minutes=5)):
        frozen.tick(timedelta(minutes=5))
        await found.runtime_data.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue) is not None
    assert await hass.config_entries.async_unload(found.entry_id)
    assert registry.async_get_issue(DOMAIN, issue) is None


async def test_a_silent_heartbeat_is_named_in_the_live_value(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    await hass.config.async_update(time_zone="Europe/Berlin")
    set_sources(hass)
    hass.states.async_set("sensor.heartbeat", "1")
    found_options = options()
    found_options["measured"] = {**found_options["measured"], "heartbeat": "sensor.heartbeat"}
    found = MockConfigEntry(
        domain=DOMAIN, title="Home", data={CONF_PLANT: plant_data()}, options=found_options
    )
    await setup(hass, found)
    coordinator = found.runtime_data
    assert coordinator.data.live is not None
    assert "telemetry" not in coordinator.data.live.external

    frozen.tick(timedelta(minutes=4))  # the heartbeat silent past TELEMETRY
    await coordinator.async_run_live()
    live = coordinator.data.live
    assert live is not None and live.external == ("telemetry",)

    hass.states.async_set("sensor.heartbeat", "2")
    await coordinator.async_run_live()
    live = coordinator.data.live
    assert live is not None and live.external == ()


async def test_a_plant_the_library_refuses_does_not_load(
    hass: HomeAssistant, frozen: FrozenDateTimeFactory
) -> None:
    bad = plant_data()
    bad["battery"]["capacity_kwh"] = -1
    found = MockConfigEntry(domain=DOMAIN, title="Home", data={CONF_PLANT: bad}, options=options())
    found.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(found.entry_id)
    assert found.state is ConfigEntryState.SETUP_ERROR
