"""Prices from Home Assistant's Amber Electric integration (``from = "amber"``).

Each channel has a price entity, the interval under way, with ``start_time``,
``end_time`` and ``per_kwh`` among its attributes, and a forecast entity whose
``forecasts`` attribute lists the intervals after it, each with the same keys. Prices
are dollars per kWh with GST. The feed-in channel's ``per_kwh`` is what an exported kWh
earns (the integration turns Amber's sign around), so it is the export price; without
it the export is the configuration's fixed ``export``. The forecast is issued at the
earlier ``last_updated`` of the entities read.
"""

from __future__ import annotations

from collections.abc import Mapping

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi
from microgrid.cli.sources.config import Amber
from microgrid.cli.sources.prices import join_prices
from microgrid.cli.sources.rows import rows_forecast, time
from microgrid.engine.series import Forecast, Price

__all__ = [
    "AmberError",
    "fetch_prices",
    "parse_channel",
    "parse_prices",
]


class AmberError(SourceError):
    """An Amber entity is unavailable, or holds no price."""


def _state(state: object, entity: str) -> dict[str, object]:
    if not isinstance(state, dict):
        raise AmberError(f"{entity}: no state")
    return state


def _attributes(state: object, entity: str) -> dict[str, object]:
    attributes = _state(state, entity).get("attributes")
    if not isinstance(attributes, dict):
        raise AmberError(f"{entity}: no attributes (state unavailable?)")
    return attributes


def parse_channel(price: object, forecast: object, *, names: tuple[str, str]) -> Forecast[float]:
    """One channel's price now and its forecast, as per-kWh values in time order."""
    price_entity, forecast_entity = names
    rows: list[object] = [_attributes(price, price_entity)]
    forecasts = _attributes(forecast, forecast_entity).get("forecasts")
    if not isinstance(forecasts, list):
        raise AmberError(f"{forecast_entity}: no forecasts list")
    rows.extend(forecasts)
    issued = min(
        time(_state(state, entity).get("last_updated"), f"{entity} last_updated")
        for state, entity in ((price, price_entity), (forecast, forecast_entity))
    )
    return rows_forecast(
        rows,
        start_key="start_time",
        end_key="end_time",
        value_key="per_kwh",
        issued_at=issued,
        source=f"amber:{forecast_entity}",
        where=forecast_entity,
    )


def parse_prices(states: Mapping[str, object], config: Amber) -> Forecast[Price]:
    """The general channel's prices with the feed-in channel's (or a fixed export)."""
    imports = parse_channel(
        states.get(config.price), states.get(config.forecast), names=(config.price, config.forecast)
    )
    if config.feed_in_price is not None and config.feed_in_forecast is not None:
        exports = parse_channel(
            states.get(config.feed_in_price),
            states.get(config.feed_in_forecast),
            names=(config.feed_in_price, config.feed_in_forecast),
        )
        return join_prices(imports, exports, source="amber")
    if config.export is None:  # pragma: no cover - the configuration requires one
        raise AmberError("no export price: name the feed-in entities or a fixed export")
    return join_prices(imports, config.export, source="amber")


async def fetch_prices(client: HomeAssistantApi, config: Amber) -> Forecast[Price]:
    """Read the configured entities and parse them (:func:`parse_prices`)."""
    names = [config.price, config.forecast, config.feed_in_price, config.feed_in_forecast]
    states: dict[str, object] = {}
    for name in names:
        if name is not None:
            states[name] = await client.get_state(name)
    return parse_prices(states, config)
