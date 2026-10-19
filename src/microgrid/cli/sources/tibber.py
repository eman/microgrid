"""Prices from Home Assistant's Tibber integration (``from = "tibber"``).

Its ``tibber.get_prices`` action answers ``{"prices": {home: [{start_time, price}]}}``:
each home's total price per kWh (energy and tax, VAT included) in its currency, by
quarter hour, from ``start`` to ``end``. A row has no end: it lasts until the next row's
start. Tibber publishes no export price, so the export is the configuration's fixed
``export``. The answer carries no time it was made: the forecast is issued when it is
read.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi
from microgrid.cli.sources.config import Tibber
from microgrid.cli.sources.prices import join_prices
from microgrid.cli.sources.rows import rows_forecast
from microgrid.engine.series import Forecast, Price

__all__ = [
    "DOMAIN",
    "TibberError",
    "fetch_prices",
    "parse_prices",
]

DOMAIN = "tibber"


class TibberError(SourceError):
    """Tibber's answer holds no prices for the home."""


def parse_prices(response: object, config: Tibber, *, now: datetime) -> Forecast[Price]:
    """A ``get_prices`` answer as prices per kWh, for the configured (or only) home."""
    homes = response.get("prices") if isinstance(response, dict) else None
    if not isinstance(homes, dict) or not homes:
        raise TibberError("get_prices: no prices in the answer")
    if config.home is None:
        if len(homes) != 1:
            names = ", ".join(sorted(map(str, homes)))
            raise TibberError(f"get_prices: several homes ({names}): name the home")
        home, rows = next(iter(homes.items()))
    else:
        home, rows = config.home, homes.get(config.home)
    if not isinstance(rows, list):
        raise TibberError(f"get_prices: no prices for {home!r}")
    imports = rows_forecast(
        rows,
        start_key="start_time",
        value_key="price",
        issued_at=now,
        source=f"{DOMAIN}:{home}",
        where=f"{DOMAIN} {home}",
    )
    return join_prices(imports, config.export)


async def fetch_prices(
    client: HomeAssistantApi, config: Tibber, *, now: datetime, horizon: timedelta
) -> Forecast[Price]:
    """The prices from the top of the hour under way over ``horizon``."""
    if now.utcoffset() != timedelta(0):
        raise ValueError("now must be UTC")
    start = now.replace(minute=0, second=0, microsecond=0)
    data = {"start": start.isoformat(), "end": (now + horizon).isoformat()}
    return parse_prices(await client.call_service(DOMAIN, "get_prices", data), config, now=now)
