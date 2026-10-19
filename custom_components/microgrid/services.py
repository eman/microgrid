"""The ``microgrid.get_valuation`` action: the published valuation (``valuation/2``),
with why and until when it holds, and the live value."""

from __future__ import annotations

from typing import Any, cast

import probatio as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import selector

from .const import ATTR_CONFIG_ENTRY, DOMAIN, SERVICE_GET_VALUATION
from .coordinator import MicrogridCoordinator

_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY): selector.ConfigEntrySelector(
            selector.ConfigEntrySelectorConfig(integration=DOMAIN)
        ),
    }
)


def _coordinator(hass: HomeAssistant, entry_id: str | None) -> MicrogridCoordinator:
    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and (entry_id is None or entry.entry_id == entry_id)
    ]
    if len(loaded) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_entry" if not loaded else "several_entries",
        )
    return cast(MicrogridCoordinator, loaded[0].runtime_data)


async def _get_valuation(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    coordinator = _coordinator(hass, call.data.get(ATTR_CONFIG_ENTRY))
    snapshot = coordinator.data
    published = None if snapshot is None else snapshot.published
    live = None if snapshot is None else snapshot.live
    response: dict[str, Any] = {
        "valuation": None,
        "live": None if live is None else live.to_mapping(),
        "problems": {} if snapshot is None else dict(snapshot.problems),
    }
    if published is not None:
        response["valuation"] = published.valuation.to_mapping()
        response["reason"] = published.reason.value
        response["valid_until"] = published.valid_until.isoformat()
    return cast(ServiceResponse, response)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register the action once for the integration."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_VALUATION):
        return

    async def handle(call: ServiceCall) -> ServiceResponse:
        return await _get_valuation(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_VALUATION,
        handle,
        schema=_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
