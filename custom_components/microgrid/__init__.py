"""Marginal energy prices for a solar and battery plant, valued in Home Assistant by the
microgrid library."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import MicrogridConfigEntry, MicrogridCoordinator
from .services import async_setup_services
from .setup import SetupError

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the action even when no entry is loaded."""
    await async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: MicrogridConfigEntry) -> bool:
    try:
        coordinator = MicrogridCoordinator(hass, entry)
    except SetupError as err:
        raise ConfigEntryError("; ".join(err.problems)) from err
    entry.runtime_data = coordinator
    # The measured entities first: the first valuation reads their states.
    await coordinator.async_start()
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.async_run_live()
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MicrogridConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: MicrogridConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
