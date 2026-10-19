"""A forecast an action returns (``from = "action"``): for an integration that publishes
its forecast as an action's response rather than an entity's attribute.

The action is called with the configured ``data``; ``rows`` is the path of keys from the
response to the list of rows, which are read as an entity's are
(:func:`~microgrid.cli.sources.entity.parse_rows`). An action's response carries no time
it was made, so the forecast is issued when it is read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi
from microgrid.cli.sources.config import ActionForecast
from microgrid.cli.sources.entity import Kind, parse_rows
from microgrid.engine.series import Forecast, Price, Quantiles

__all__ = [
    "ActionError",
    "fetch_forecast",
    "parse_response",
]


class ActionError(SourceError):
    """The action's response holds no forecast where the configuration says."""


def parse_response(
    response: object, config: ActionForecast, kind: Kind, *, now: datetime
) -> Forecast[Price] | Forecast[Quantiles] | Forecast[float]:
    """The forecast in an action's ``response``, as configured, issued ``now``."""
    name = f"{config.domain}.{config.service}"
    found: object = response
    for key in config.rows:
        if not isinstance(found, dict) or key not in found:
            raise ActionError(f"{name}: no {key!r} in the response")
        found = found[key]
    if not isinstance(found, list) or not found:
        path = ".".join(config.rows) or "the response"
        raise ActionError(f"{name}: {path} is not a list of rows")
    return parse_rows(
        found,
        start_key=config.start_key,
        end_key=config.end_key,
        period=config.period,
        keys=config.keys,
        unit=config.unit,
        kind=kind,
        issued_at=now,
        source=f"action:{name}",
        where=name,
        export=config.export,
    )


async def fetch_forecast(
    client: HomeAssistantApi, config: ActionForecast, kind: Kind, *, now: datetime
) -> Forecast[Price] | Forecast[Quantiles] | Forecast[float]:
    """Call the action and parse its response (:func:`parse_response`)."""
    data: dict[str, Any] = dict(config.data)
    response = await client.call_service(config.domain, config.service, data)
    return parse_response(response, config, kind, now=now)
