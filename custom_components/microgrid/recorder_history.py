"""Home Assistant's recorder as the library's recorded history (``History``): what the
battery's ledger reads to run in its lots.

Each entity is read as the recorder kept it, its changes, from the state in force at the
window's start, in the unit its entity stated when the reader was made (as ``microgrid
fetch`` reads Home Assistant's history). A heartbeat's samples are its changes alone: the
state in force at the start says nothing of the path being alive then.

The reads query the database where they are awaited: run them, and the ledger over them,
in the recorder's executor (:func:`run_in_recorder`), never on the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.recorder import history
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.recorder import get_instance

from microgrid.cli.history import WEEK, LotsError
from microgrid.engine.series import Sample, Series
from microgrid.engine.units import UnitError, convert


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None  # unavailable, unknown: no value from here


async def run_in_recorder[T](hass: HomeAssistant, work: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """``work`` (a coroutine reading a :class:`RecorderHistory`) run to its end on a loop
    of its own, in the recorder's executor."""
    result: T = await get_instance(hass).async_add_executor_job(lambda: asyncio.run(work()))
    return result


class RecorderHistory:
    """The recorder, read through the library's ``History`` port, in the units
    ``units`` names (each entity's ``unit_of_measurement``)."""

    def __init__(self, hass: HomeAssistant, units: Mapping[str, str | None]) -> None:
        self._hass = hass
        #: Each entity's unit, read on the event loop before a read.
        self.units: dict[str, str | None] = dict(units)

    def _states(
        self, entity_id: str, start: datetime, end: datetime, *, at_start: bool
    ) -> list[State]:
        found = history.state_changes_during_period(
            self._hass,
            start,
            end,
            entity_id,
            no_attributes=True,
            include_start_time_state=at_start,
        )
        return list(found.get(entity_id.lower(), []))

    def _numbers(self, entity_id: str, start: datetime, end: datetime) -> Series[float]:
        rows = self._states(entity_id, start, end, at_start=True)
        samples = sorted(
            (
                Sample(max(state.last_changed.astimezone(UTC), start), _number(state.state))
                for state in rows
                if state.last_changed < end
            ),
            key=lambda sample: sample.at,
        )
        return Series(tuple(samples), end)

    async def power_series(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        carry_in: timedelta,
        chunk: timedelta = WEEK,
    ) -> Series[float]:
        """Power in kW, converted from the entity's unit."""
        found = self._numbers(entity_id, start, end)
        unit = self.units.get(entity_id)
        try:
            return Series(
                tuple(
                    Sample(s.at, None if s.value is None else convert(s.value, unit, "power"))
                    for s in found.samples
                ),
                end,
            )
        except UnitError as err:
            raise LotsError(f"{entity_id}: {err}") from err

    async def numeric_series(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        carry_in: timedelta,
        chunk: timedelta = WEEK,
    ) -> Series[float]:
        """Any numeric entity's values, the unit unchecked."""
        return self._numbers(entity_id, start, end)

    async def sample_times(
        self,
        entity_id: str,
        start: datetime,
        end: datetime,
        *,
        carry_in: timedelta,
        chunk: timedelta = WEEK,
    ) -> tuple[datetime, ...]:
        """When the entity's changes were recorded, from ``carry_in`` before ``start``."""
        rows = self._states(entity_id, start - carry_in, end, at_start=False)
        return tuple(
            sorted(state.last_updated.astimezone(UTC) for state in rows if state.last_updated < end)
        )
