"""A PV forecast from Home Assistant's Solcast integration (``from = "solcast"``).

Each daily forecast entity (today, tomorrow, day 3, ...) carries a ``detailedForecast``
attribute of half-hourly slots with the mean kW over the slot at P50 (``pv_estimate``),
P10 and P90; the forecast is the library's quantiles 0.1, 0.5 and 0.9. Its age is the
integration's last successful poll (the ``last_polled`` entity), not the entities'
``last_updated``, which Home Assistant moves at every local midnight and restart.

An entity that is unavailable or malformed is skipped and named
(:attr:`SolcastRead.skipped`); the forecast is the contiguous run from the earliest
usable slot. Two entities that disagree about the same half hour are an error.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from microgrid.cli.sources import SourceError
from microgrid.cli.sources.home_assistant import HaClient, HaError
from microgrid.engine.series import Forecast, Interval, Quantiles

__all__ = [
    "PERIOD",
    "SOURCE",
    "SolcastError",
    "SolcastRead",
    "fetch_forecast",
    "parse_forecast",
    "parse_last_polled",
]

log = logging.getLogger(__name__)


PERIOD = timedelta(minutes=30)
#: The forecast's source label.
SOURCE = "solcast"


class SolcastError(SourceError):
    """No usable Solcast forecast, or its freshness is unknown."""


@dataclass(frozen=True, slots=True)
class SolcastRead:
    """The PV forecast read, and the forecast sensors that could not be read."""

    forecast: Forecast[Quantiles]
    skipped: tuple[str, ...] = ()


class _Slot(BaseModel):
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)
    period_start: AwareDatetime
    pv_estimate: float = Field(ge=0)
    pv_estimate10: float = Field(ge=0)
    pv_estimate90: float = Field(ge=0)


class _Attributes(BaseModel):
    model_config = ConfigDict(extra="ignore")
    detailedForecast: list[_Slot] = Field(min_length=1)  # noqa: N815 - Solcast's name


class _State(BaseModel):
    model_config = ConfigDict(extra="ignore")
    entity_id: str
    attributes: _Attributes


def parse_last_polled(state: object) -> datetime:
    """The last successful poll, from the ``api_last_polled`` sensor's state (UTC)."""
    value = state.get("state") if isinstance(state, dict) else None
    try:
        polled = datetime.fromisoformat(str(value))
    except ValueError as err:
        raise SolcastError(f"Solcast last-polled time is not a timestamp: {value!r}") from err
    if polled.tzinfo is None:
        raise SolcastError(f"Solcast last-polled time has no offset: {value!r}")
    return polled.astimezone(UTC)


def _values(slot: _Slot) -> tuple[float, float, float]:
    return (slot.pv_estimate, slot.pv_estimate10, slot.pv_estimate90)


def parse_forecast(
    states: Sequence[object], *, as_of: datetime, skipped: Sequence[str] = ()
) -> SolcastRead:
    """Merge the forecast sensors' states into one contiguous UTC forecast.

    ``as_of`` is the last successful poll (:func:`parse_last_polled`).
    ``skipped`` names sensors the caller could not read; unusable states are
    added to it.
    """
    skipped_all = list(skipped)
    merged: dict[datetime, tuple[float, float, float]] = {}
    for state in states:
        entity = str(state.get("entity_id")) if isinstance(state, dict) else "?"
        try:
            parsed = _State.model_validate(state)
        except ValidationError as err:
            log.warning("skipping Solcast sensor %s: %s", entity, err.errors()[0]["msg"])
            skipped_all.append(entity)
            continue
        for slot in parsed.attributes.detailedForecast:
            start = slot.period_start.astimezone(UTC)
            if start in merged and merged[start] != _values(slot):
                raise SolcastError(
                    f"Solcast sensors disagree at {start.isoformat()} ({entity} and another)"
                )
            merged[start] = _values(slot)
    if not merged:
        raise SolcastError(f"no usable Solcast forecast (skipped: {', '.join(skipped_all)})")

    run: list[Interval[Quantiles]] = []
    for start, (p50, p10, p90) in sorted(merged.items()):
        if run and start != run[-1].end:
            log.warning("Solcast forecast has a gap at %s; using it up to there", start)
            break
        run.append(Interval(start, start + PERIOD, Quantiles({0.1: p10, 0.5: p50, 0.9: p90})))
    return SolcastRead(Forecast(tuple(run), as_of, SOURCE), tuple(skipped_all))


async def fetch_forecast(
    client: HaClient, entities: Sequence[str], last_polled_entity: str
) -> SolcastRead:
    """Read every configured forecast sensor and the last-polled sensor, and merge.

    An unreadable forecast sensor is skipped. An unreadable last-polled sensor
    raises: without it the forecast's age is unknown.
    """
    states: list[object] = []
    skipped: list[str] = []
    for entity in entities:
        try:
            states.append(await client.get_state(entity))
        except HaError as err:
            log.warning("skipping Solcast sensor %s: %s", entity, err)
            skipped.append(entity)
    as_of = parse_last_polled(await client.get_state(last_polled_entity))
    return parse_forecast(states, as_of=as_of, skipped=skipped)
