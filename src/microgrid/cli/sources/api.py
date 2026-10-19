"""What a reader asks of Home Assistant: an entity's state, an action's response and an
integration's config entries.

The library's REST client (:class:`~microgrid.cli.sources.home_assistant.HaClient`)
answers these over HTTP; a client running inside Home Assistant answers them from its own
state machine and actions. A reader takes either, so the same readers serve the
``microgrid`` command and a Home Assistant integration.

A failed read raises :class:`~microgrid.cli.sources.SourceError` naming what failed.
"""

from __future__ import annotations

from typing import Any, Protocol

from microgrid.cli.sources import SourceError

__all__ = [
    "HomeAssistantApi",
    "discover_config_entry",
]


class HomeAssistantApi(Protocol):
    """Home Assistant as the readers use it."""

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        """The entity's state as Home Assistant serializes it: ``entity_id``, ``state``,
        ``attributes``, ``last_changed``, ``last_updated`` (ISO 8601 times)."""
        ...

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        """Call an action that returns a response, and return the response."""
        ...

    async def config_entries(self, domain: str) -> list[dict[str, Any]]:
        """The integration's config entries, each with ``entry_id`` and ``state``."""
        ...


async def discover_config_entry(client: HomeAssistantApi, domain: str) -> str:
    """The ID of the integration's one loaded config entry. Raises
    :class:`~microgrid.cli.sources.SourceError` when there is none, or more than one."""
    loaded = [
        entry
        for entry in await client.config_entries(domain)
        if entry.get("state") == "loaded" and isinstance(entry.get("entry_id"), str)
    ]
    if len(loaded) != 1:
        raise SourceError(
            f"expected one loaded {domain} config entry, found {len(loaded)}: name config_entry"
        )
    entry_id: str = loaded[0]["entry_id"]
    return entry_id
