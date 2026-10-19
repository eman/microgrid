"""Prices from TariffKit's Home Assistant integration (``from = "tariffkit"``).

TariffKit prices a utility's tariff; its integration's ``get_rates`` service returns
all-in import and export prices per kWh, by the hour. This asks for an hour more than the
horizon from the top of the hour under way, and the engine steps the hours onto its slots.
The answer is one forecast issue, stamped when TariffKit answered (``generated_at``), its
intervals contiguous (a gap in an answer is an error).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.api import HomeAssistantApi
from microgrid.engine.series import Forecast, Interval, Price

__all__ = [
    "DOMAIN",
    "MAX_HORIZON_HOURS",
    "MAX_REQUEST_HOURS",
    "RESOLUTION_MINUTES",
    "TariffKitError",
    "discover_config_entry",
    "fetch_rates",
    "parse_rates",
]

DOMAIN = "tariffkit"
RESOLUTION_MINUTES = 60


class TariffKitError(SourceError):
    """TariffKit's response was missing, ambiguous or malformed."""


class _Quality(BaseModel):
    model_config = ConfigDict(extra="ignore")
    complete: bool
    exact: bool
    locked: bool


class _Point(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: AwareDatetime
    end: AwareDatetime
    # "import" is a keyword, so the fields are aliased.
    p_import: float = Field(alias="import")
    p_export: float = Field(alias="export")
    quality: _Quality


class _RatesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    resolution: int
    generated_at: AwareDatetime
    points: list[_Point]


def parse_rates(response: object, *, resolution: int = RESOLUTION_MINUTES) -> Forecast[Price]:
    """A ``get_rates`` service response as a forecast of prices, in UTC."""
    try:
        rates = _RatesResponse.model_validate(response)
    except ValidationError as err:
        raise TariffKitError(f"unexpected get_rates response: {err}") from err
    if not rates.points:
        raise TariffKitError("get_rates returned no points")
    if rates.resolution != resolution:
        raise TariffKitError(f"get_rates answered at {rates.resolution} min, not {resolution}")
    try:
        intervals = tuple(
            Interval(
                point.start.astimezone(UTC),
                point.end.astimezone(UTC),
                Price(point.p_import, point.p_export),
            )
            for point in rates.points
        )
        for earlier, later in pairwise(intervals):
            if later.start != earlier.end:
                raise ValueError(f"a gap or overlap at {later.start.isoformat()}")
        return Forecast(intervals, rates.generated_at.astimezone(UTC), DOMAIN)
    except ValueError as err:
        raise TariffKitError(f"get_rates points are inconsistent: {err}") from err


#: ``get_rates`` rejects a longer window (TariffKit ``services.py`` ``MAX_HOURS``).
MAX_REQUEST_HOURS = 168
#: The longest horizon this reader can serve: one hour goes to mid-hour slots.
MAX_HORIZON_HOURS = MAX_REQUEST_HOURS - 1


async def discover_config_entry(client: HomeAssistantApi) -> str:
    """The ID of the single loaded TariffKit config entry."""
    loaded = [
        entry
        for entry in await client.config_entries(DOMAIN)
        if entry.get("state") == "loaded" and isinstance(entry.get("entry_id"), str)
    ]
    if len(loaded) != 1:
        raise TariffKitError(
            f"expected one loaded TariffKit entry, found {len(loaded)}: "
            "set [tariffkit] config_entry in the site config"
        )
    entry_id: str = loaded[0]["entry_id"]
    return entry_id


async def fetch_rates(
    client: HomeAssistantApi, *, config_entry: str, horizon: timedelta, now: datetime
) -> Forecast[Price]:
    """Hourly prices covering ``[now, now + horizon)``.

    The request starts at the top of the hour containing ``now`` and asks for
    one hour more than ``horizon``, so slots that start mid-hour are still
    priced at the far end.
    """
    if now.utcoffset() != timedelta(0):
        raise ValueError("now must be UTC")
    if horizon % timedelta(hours=1):
        raise ValueError("horizon must be whole hours")
    hours = int(horizon / timedelta(hours=1))
    if not 1 <= hours <= MAX_HORIZON_HOURS:
        raise ValueError(f"horizon must be 1..{MAX_HORIZON_HOURS} h, not {hours}")
    start = now.replace(minute=0, second=0, microsecond=0)
    response = await client.call_service(
        DOMAIN,
        "get_rates",
        {
            "config_entry": config_entry,
            "start": start.isoformat(),
            "horizon": hours + 1,
            "resolution": RESOLUTION_MINUTES,
        },
    )
    rates = parse_rates(response)
    first, last = rates.intervals[0].start, rates.intervals[-1].end
    if first != start or last < start + timedelta(hours=hours + 1):
        raise TariffKitError(
            f"get_rates covered {first.isoformat()} .. {last.isoformat()}, "
            f"not the requested {hours + 1} h from {start.isoformat()}"
        )
    return rates
