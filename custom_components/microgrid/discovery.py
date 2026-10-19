"""What setup can find for itself: the measured entities the Energy dashboard names, each
meter's sign from what the recorder kept, and the sources the installed integrations
publish (:mod:`.found_sources`).

Each finding is a proposal: the forms show it, and the user keeps or changes it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Final

from homeassistant.components.energy.data import async_get_manager
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from microgrid.cli.signs import Signs, meter_signs
from microgrid.engine.series import means, slot_floor
from microgrid.engine.units import UnitError, convert
from microgrid.plant import Plant

from .recorder_history import RecorderHistory, run_in_recorder

_LOGGER = logging.getLogger(__name__)

#: How far back the meters are read to tell their signs.
SIGNS_OVER: Final = timedelta(days=2)
_HOUR: Final = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class EnergyFound:
    """What the Energy dashboard names: power sensors in its own convention (the grid
    positive importing, the battery positive discharging), the battery's SOC and its
    capacity."""

    pv: str | None = None
    grid: str | None = None
    battery: str | None = None
    soc: str | None = None
    capacity_kwh: float | None = None


def _entity(hass: HomeAssistant, value: object) -> str | None:
    """``value`` where it is an entity Home Assistant has (not an external statistic)."""
    if isinstance(value, str) and hass.states.get(value) is not None:
        return value
    return None


async def energy_found(hass: HomeAssistant) -> EnergyFound:
    """The Energy dashboard's power sensors, where it names one of each kind; nothing
    where it is not set up."""
    try:
        manager = await async_get_manager(hass)
    except Exception as err:  # no Energy dashboard: nothing to propose
        _LOGGER.debug("no energy preferences: %s", err)
        return EnergyFound()
    prefs: Mapping[str, Any] | None = manager.data
    if not prefs:
        return EnergyFound()
    by_type: dict[str, list[Mapping[str, Any]]] = {}
    for source in prefs.get("energy_sources", []):
        by_type.setdefault(str(source.get("type")), []).append(source)

    def one(kind: str) -> Mapping[str, Any] | None:
        found = by_type.get(kind, [])
        return found[0] if len(found) == 1 else None  # several: the plant's are unclear

    solar, grid, battery = one("solar"), one("grid"), one("battery")
    capacity = battery.get("capacity") if battery is not None else None
    return EnergyFound(
        pv=_entity(hass, solar.get("stat_rate")) if solar else None,
        grid=_entity(hass, grid.get("stat_rate")) if grid else None,
        battery=_entity(hass, battery.get("stat_rate")) if battery else None,
        soc=_entity(hass, battery.get("stat_soc")) if battery else None,
        capacity_kwh=float(capacity) if isinstance(capacity, int | float) else None,
    )


def _unit(hass: HomeAssistant, entity: str) -> str | None:
    state = hass.states.get(entity)
    unit = None if state is None else state.attributes.get("unit_of_measurement")
    return None if unit is None else str(unit)


async def read_signs(
    hass: HomeAssistant,
    plant: Plant,
    measured: Mapping[str, Any],
) -> Signs | None:
    """Each of the plant's meters' sign, from the recorder's last :data:`SIGNS_OVER` of
    the entities ``measured`` (a ``sources/1`` section) names; None without the
    recorder or anything recorded."""
    if "recorder" not in hass.config.components:
        return None
    entities: Mapping[str, str] = measured.get("meters", {})
    soc_entity = measured.get("soc")
    house_entity = measured.get("house")
    named = [*entities.values(), *(e for e in (soc_entity, house_entity) if e)]
    history = RecorderHistory(hass, {entity: _unit(hass, entity) for entity in named})
    end = slot_floor(dt_util.utcnow(), _HOUR)
    start = end - SIGNS_OVER
    starts = [start + i * _HOUR for i in range(int(SIGNS_OVER / _HOUR))]
    soc_unit = (_unit(hass, soc_entity) if soc_entity else None) or measured.get("soc_unit", "%")

    async def read() -> tuple[dict[str, tuple[float | None, ...]], Any, Any]:
        readings = {
            name: means(
                await history.power_series(entity, start, end, carry_in=_HOUR), starts, _HOUR
            )
            for name, entity in entities.items()
        }
        soc = house = None
        if soc_entity:
            raw = means(
                await history.numeric_series(soc_entity, start, end, carry_in=_HOUR),
                starts,
                _HOUR,
            )
            soc = tuple(
                None if v is None else convert(v, soc_unit or None, "fraction") for v in raw
            )
        if house_entity:
            house = means(
                await history.power_series(house_entity, start, end, carry_in=_HOUR),
                starts,
                _HOUR,
            )
        return readings, soc, house

    try:
        readings, soc, house = await run_in_recorder(hass, read)
    except (UnitError, ValueError) as err:
        _LOGGER.debug("signs not read: %s", err)
        return None
    if not any(v is not None for values in readings.values() for v in values):
        return None
    return meter_signs(plant.config.meter_set, readings, hours=1.0, soc=soc, house=house)


def with_signs(
    plant_data: Mapping[str, Any], plant: Plant, signs: Mapping[str, float]
) -> dict[str, Any]:
    """The plant's mapping with its meters' signs as ``signs`` gives them (each meter's
    other settings as they were)."""
    config = plant.config
    meters = tuple(
        replace(meter, sign=signs.get(meter.name, meter.sign)) for meter in config.meter_set
    )
    found = dict(plant_data)
    found["meters"] = replace(config, meters=meters).to_mapping()["meters"]
    return found


def changed_signs(plant: Plant, signs: Signs) -> dict[str, float]:
    """The signs found that differ from the plant's."""
    configured = {meter.name: meter.sign for meter in plant.config.meter_set}
    return {name: sign for name, sign in signs.found.items() if configured.get(name, 1.0) != sign}
