"""Prices from the Octopus Energy integration (``from = "octopus"``).

Each electricity meter has day-rates event entities (today's, tomorrow's) whose
``rates`` attribute lists the half hours, each with ``start``, ``end`` and
``value_inc_vat``: pounds per kWh with VAT. The import meter's entities give the import
price; the export meter's, the export price (else the configuration's fixed
``export``). The first entity named (today's) must hold rates; a later one may not yet
(tomorrow's are published in the afternoon). The forecast is issued at the earliest
``last_updated`` of the entities that held rates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi
from microgrid.cli.sources.config import Octopus
from microgrid.cli.sources.prices import join_prices
from microgrid.cli.sources.rows import rows_forecast, time
from microgrid.engine.series import Forecast, Price

__all__ = [
    "OctopusError",
    "fetch_prices",
    "parse_meter",
    "parse_prices",
]


class OctopusError(SourceError):
    """An Octopus rates entity is unavailable, or today's holds no rates."""


def parse_meter(states: Mapping[str, object], entities: Sequence[str]) -> Forecast[float]:
    """One meter's day-rates entities as per-kWh values in time order."""
    rows: list[object] = []
    issued = []
    for index, entity in enumerate(entities):
        state = states.get(entity)
        attributes = state.get("attributes") if isinstance(state, dict) else None
        rates = attributes.get("rates") if isinstance(attributes, dict) else None
        if not isinstance(rates, list) or not rates:
            if index == 0:
                raise OctopusError(f"{entity}: no rates")
            continue  # tomorrow's, not published yet
        assert isinstance(state, dict)
        issued.append(time(state.get("last_updated"), f"{entity} last_updated"))
        rows.extend(rates)
    return rows_forecast(
        rows,
        start_key="start",
        end_key="end",
        value_key="value_inc_vat",
        issued_at=min(issued),
        source=f"octopus:{entities[0]}",
        where=entities[0],
    )


def parse_prices(states: Mapping[str, object], config: Octopus) -> Forecast[Price]:
    """The import meter's rates with the export meter's (or a fixed export)."""
    imports = parse_meter(states, config.entities)
    if config.export_entities:
        return join_prices(imports, parse_meter(states, config.export_entities), source="octopus")
    if config.export is None:  # pragma: no cover - the configuration requires one
        raise OctopusError("no export price: name the export entities or a fixed export")
    return join_prices(imports, config.export, source="octopus")


async def fetch_prices(client: HomeAssistantApi, config: Octopus) -> Forecast[Price]:
    """Read the configured entities and parse them (:func:`parse_prices`)."""
    states: dict[str, object] = {}
    for entity in (*config.entities, *config.export_entities):
        try:
            states[entity] = await client.get_state(entity)
        except SourceError:
            if entity in (config.entities[0], *config.export_entities[:1]):
                raise
    return parse_prices(states, config)
