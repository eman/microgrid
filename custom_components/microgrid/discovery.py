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

from microgrid.cli.history import LotsError
from microgrid.cli.seen import BatterySeen, battery_seen, rounded
from microgrid.cli.signs import Signs, meter_signs
from microgrid.engine.series import Sample, Series, means, slot_floor
from microgrid.engine.units import UnitError, convert
from microgrid.plant import Plant

from .recorder_history import RecorderHistory, run_in_recorder

_LOGGER = logging.getLogger(__name__)

#: How far back the battery's meters are read for its rating.
SEEN_OVER: Final = timedelta(days=7)
#: How far back the meters are read to tell their signs.
SIGNS_OVER: Final = timedelta(days=2)
#: Hours with every meter recorded the signs need: a day, so the PV, the battery's
#: charge and discharge, and night's imports each show.
SIGNS_NEED: Final = 24
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


async def battery_measured(hass: HomeAssistant, found: EnergyFound) -> BatterySeen:
    """The battery's rating as the Energy dashboard's battery sensor (positive
    discharging) and SOC show it over the last :data:`SEEN_OVER`, rounded; its capacity
    the dashboard's where it states one. Nothing without the recorder or the sensor."""
    unknown = BatterySeen(None, None, found.capacity_kwh)
    if found.battery is None or "recorder" not in hass.config.components:
        return unknown
    entities = [found.battery, *([found.soc] if found.soc else [])]
    history = RecorderHistory(hass, {entity: _unit(hass, entity) for entity in entities})
    end = slot_floor(dt_util.utcnow(), _HOUR)
    start = end - SEEN_OVER
    battery, soc_entity = found.battery, found.soc
    soc_unit = (_unit(hass, soc_entity) if soc_entity else None) or "%"

    async def read() -> BatterySeen:
        power = await history.power_series(battery, start, end, carry_in=_HOUR)
        charging = Series(  # the plant's convention: positive charging
            tuple(Sample(s.at, None if s.value is None else -s.value) for s in power.samples),
            power.known_until,
        )
        soc = None
        if soc_entity:
            raw = await history.numeric_series(soc_entity, start, end, carry_in=_HOUR)
            soc = Series(
                tuple(
                    Sample(
                        s.at, None if s.value is None else convert(s.value, soc_unit, "fraction")
                    )
                    for s in raw.samples
                ),
                raw.known_until,
            )
        return battery_seen(charging, soc, start, end)

    try:
        seen = rounded(await run_in_recorder(hass, read))
    except (LotsError, UnitError, ValueError) as err:
        _LOGGER.debug("battery not measured: %s", err)
        return unknown
    if found.capacity_kwh is not None:
        return BatterySeen(seen.charge_kw, seen.discharge_kw, found.capacity_kwh)
    return seen


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
    except (LotsError, UnitError, ValueError) as err:  # a unit that is not power, say
        _LOGGER.debug("signs not read: %s", err)
        return None
    complete = sum(
        1 for slot in zip(*readings.values(), strict=True) if all(v is not None for v in slot)
    )
    if complete < SIGNS_NEED:
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


def energy_signs(
    plant: Plant, measured: Mapping[str, Any], found: EnergyFound
) -> dict[str, tuple[float, str]]:
    """The signs the Energy dashboard's convention gives the meters ``measured`` reads
    from its sensors: the grid positive importing (the plant's), the battery positive
    discharging (the plant's opposite)."""
    entities: Mapping[str, str] = measured.get("meters", {})
    signs: dict[str, tuple[float, str]] = {}
    for meter in plant.config.meter_set:
        entity = entities.get(meter.name)
        if entity is None:
            continue
        if meter.measures == "battery" and entity == found.battery:
            signs[meter.name] = (
                -1.0,
                f"{meter.name} is the Energy dashboard's battery power, positive discharging",
            )
        elif meter.measures == "grid" and entity == found.grid:
            signs[meter.name] = (
                1.0,
                f"{meter.name} is the Energy dashboard's grid power, positive importing",
            )
    return signs


async def proposed_signs(
    hass: HomeAssistant, plant: Plant, measured: Mapping[str, Any]
) -> dict[str, tuple[float, str]]:
    """Each meter's sign and why, where something says: the Energy dashboard's
    convention for its own sensors, then the recorder's history where it tells."""
    proposed = energy_signs(plant, measured, await energy_found(hass))
    signs = await read_signs(hass, plant, measured)
    if signs is not None:
        proposed |= {name: (sign, signs.why[name]) for name, sign in signs.found.items()}
    return proposed


def changed_signs(
    plant: Plant, proposed: Mapping[str, tuple[float, str]]
) -> dict[str, tuple[float, str]]:
    """The signs proposed that differ from the plant's."""
    configured = {meter.name: meter.sign for meter in plant.config.meter_set}
    return {
        name: found for name, found in proposed.items() if configured.get(name, 1.0) != found[0]
    }
