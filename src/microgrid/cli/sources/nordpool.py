"""Day-ahead prices from Home Assistant's Nord Pool integration (``from = "nordpool"``).

Its ``nordpool.get_prices_for_date`` action answers, for a delivery date and an area,
``{area: [{start, end, price}]}``: the market's price per MWh in the integration's
currency (or the one asked for), before any fee or tax a supplier adds. This asks for
the date under way and the next, in UTC, which together hold the delivery hour under way
and what is published after it (the next day's prices exist from early afternoon,
Central European time; until then the forecast ends with the day). Nord Pool publishes
no export price, so the export is the configuration's fixed ``export``. The answer
carries no time it was made: the forecast is issued when it is read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi, discover_config_entry
from microgrid.cli.sources.config import NordPool
from microgrid.cli.sources.prices import join_prices
from microgrid.cli.sources.rows import rows_forecast
from microgrid.engine.series import Forecast, Price

__all__ = [
    "DOMAIN",
    "NordPoolError",
    "fetch_prices",
    "parse_prices",
]

DOMAIN = "nordpool"


class NordPoolError(SourceError):
    """Nord Pool's answer is missing the area, or malformed."""


def parse_prices(responses: list[object], config: NordPool, *, now: datetime) -> Forecast[Price]:
    """The ``get_prices_for_date`` answers (one per date) as prices per kWh."""
    rows: list[object] = []
    for response in responses:
        if not isinstance(response, dict) or not isinstance(response.get(config.area), list):
            raise NordPoolError(f"get_prices_for_date: no {config.area} in the answer")
        rows.extend(response[config.area])
    imports = rows_forecast(
        rows,
        start_key="start",
        end_key="end",
        value_key="price",
        scale=lambda per_mwh: per_mwh / 1000,
        issued_at=now,
        source=f"{DOMAIN}:{config.area}",
        where=f"{DOMAIN} {config.area}",
    )
    return join_prices(imports, config.export)


async def fetch_prices(
    client: HomeAssistantApi, config: NordPool, *, now: datetime
) -> Forecast[Price]:
    """The prices published for today and tomorrow (UTC dates), from now."""
    if now.utcoffset() != timedelta(0):
        raise ValueError("now must be UTC")
    entry = config.config_entry or await discover_config_entry(client, DOMAIN)
    today = now.astimezone(UTC).date()
    responses: list[object] = []
    for day in (today, today + timedelta(days=1)):
        data: dict[str, Any] = {"config_entry": entry, "date": day.isoformat()}
        data["areas"] = [config.area]
        if config.currency is not None:
            data["currency"] = config.currency
        try:
            responses.append(await client.call_service(DOMAIN, "get_prices_for_date", data))
        except SourceError:
            if day == today:
                raise
            # tomorrow's prices are not published yet: the forecast ends with today's
    return parse_prices(responses, config, now=now)
