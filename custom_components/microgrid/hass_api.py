"""Home Assistant as the library's readers ask it (``HomeAssistantApi``), answered from
the state machine, the action registry and the config entries, in process."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from microgrid.cli.sources import SourceError


class HassApi:
    """The readers' ``HomeAssistantApi``, inside Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        """The entity's state as Home Assistant serializes it (``State.as_dict``)."""
        state = self._hass.states.get(entity_id)
        if state is None:
            raise SourceError(f"{entity_id}: no such entity")
        return dict(state.as_dict())

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        """Call an action that returns a response, and return the response."""
        if not self._hass.services.has_service(domain, service):
            raise SourceError(f"{domain}.{service}: no such action (is the integration loaded?)")
        try:
            return await self._hass.services.async_call(
                domain, service, data, blocking=True, return_response=True
            )
        except Exception as err:  # an action's own failure, or its fields refused
            raise SourceError(f"{domain}.{service}: {err}") from err

    async def config_entries(self, domain: str) -> list[dict[str, Any]]:
        """The integration's config entries, each with ``entry_id`` and ``state``."""
        return [
            {"entry_id": entry.entry_id, "state": entry.state.value, "title": entry.title}
            for entry in self._hass.config_entries.async_entries(domain)
        ]
