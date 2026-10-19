"""Prices from the ENTSO-E integration (``from = "entsoe"``).

Its average-price entity's ``prices`` attribute lists ``{time, price}`` rows, hourly or
by quarter hour, as its own settings make them (a modifier template, VAT), in the
entity's unit (a currency per kWh or per MWh). A row has no end: it lasts until the next
row's start. ENTSO-E publishes no export price, so the export is the configuration's
fixed ``export``. The entity changes only when the day's prices are published, so the
forecast is issued when it is read: the prices the integration holds now.
"""

from __future__ import annotations

from datetime import datetime

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi
from microgrid.cli.sources.config import Entsoe
from microgrid.cli.sources.prices import join_prices
from microgrid.cli.sources.rows import rows_forecast
from microgrid.engine.series import Forecast, Price
from microgrid.engine.units import UnitError, convert

__all__ = [
    "EntsoeError",
    "fetch_prices",
    "parse_prices",
]


class EntsoeError(SourceError):
    """The ENTSO-E entity holds no prices, or in no unit of price."""


def parse_prices(state: object, config: Entsoe, *, now: datetime) -> Forecast[Price]:
    """The entity's ``prices`` as prices per kWh."""
    entity = config.entity
    attributes = state.get("attributes") if isinstance(state, dict) else None
    if not isinstance(state, dict) or not isinstance(attributes, dict):
        raise EntsoeError(f"{entity}: no attributes (state unavailable?)")
    rows = attributes.get("prices")
    if not isinstance(rows, list) or not rows:
        raise EntsoeError(f"{entity}: no prices list")
    unit = attributes.get("unit_of_measurement")
    try:
        convert(1.0, None if unit is None else str(unit), "price")
    except UnitError as err:
        raise EntsoeError(f"{entity}: {err}") from err
    imports = rows_forecast(
        rows,
        start_key="time",
        value_key="price",
        scale=lambda value: convert(value, None if unit is None else str(unit), "price"),
        issued_at=now,
        source=f"entsoe:{entity}",
        where=f"{entity} prices",
    )
    return join_prices(imports, config.export)


async def fetch_prices(
    client: HomeAssistantApi, config: Entsoe, *, now: datetime
) -> Forecast[Price]:
    """Read the entity and parse it (:func:`parse_prices`)."""
    return parse_prices(await client.get_state(config.entity), config, now=now)
