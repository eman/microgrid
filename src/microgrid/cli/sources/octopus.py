"""Prices from the Octopus Energy integration (``from = "octopus"``).

Each electricity meter has day-rates event entities (today's, tomorrow's) whose
``rates`` attribute lists the half hours, each with ``start``, ``end`` and
``value_inc_vat``: pounds per kWh with VAT. The import meter's entities give the import
price; the export meter's, the export price (else the configuration's fixed
``export``). The first entity named (today's) must hold rates; a later one may not yet
(tomorrow's are published in the afternoon). An entity changes only when a day's rates
are published, so the forecast is issued when it is read: the rates the integration
holds now.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi
from microgrid.cli.sources.config import Octopus
from microgrid.cli.sources.prices import join_prices
from microgrid.cli.sources.rows import rows_forecast
from microgrid.engine.series import Forecast, Price

__all__ = [
    "OctopusError",
    "fetch_prices",
    "parse_meter",
    "parse_prices",
]


class OctopusError(SourceError):
    """An Octopus rates entity is unavailable, or today's holds no rates."""


def parse_meter(
    states: Mapping[str, object], entities: Sequence[str], *, now: datetime
) -> Forecast[float]:
    """One meter's day-rates entities as per-kWh values in time order."""
    rows: list[object] = []
    for index, entity in enumerate(entities):
        state = states.get(entity)
        attributes = state.get("attributes") if isinstance(state, dict) else None
        rates = attributes.get("rates") if isinstance(attributes, dict) else None
        if not isinstance(rates, list) or not rates:
            if index == 0:
                raise OctopusError(f"{entity}: no rates")
            continue  # tomorrow's, not published yet
        rows.extend(rates)
    return rows_forecast(
        rows,
        start_key="start",
        end_key="end",
        value_key="value_inc_vat",
        issued_at=now,
        source=f"octopus:{entities[0]}",
        where=entities[0],
    )


def parse_prices(
    states: Mapping[str, object], config: Octopus, *, now: datetime
) -> Forecast[Price]:
    """The import meter's rates with the export meter's (or a fixed export)."""
    imports = parse_meter(states, config.entities, now=now)
    if config.export_entities:
        exports = parse_meter(states, config.export_entities, now=now)
        return join_prices(imports, exports, source="octopus")
    if config.export is None:  # pragma: no cover - the configuration requires one
        raise OctopusError("no export price: name the export entities or a fixed export")
    return join_prices(imports, config.export, source="octopus")


async def fetch_prices(
    client: HomeAssistantApi, config: Octopus, *, now: datetime
) -> Forecast[Price]:
    """Read the configured entities and parse them (:func:`parse_prices`)."""
    states: dict[str, object] = {}
    for entity in (*config.entities, *config.export_entities):
        try:
            states[entity] = await client.get_state(entity)
        except SourceError:
            if entity in (config.entities[0], *config.export_entities[:1]):
                raise
    return parse_prices(states, config, now=now)
