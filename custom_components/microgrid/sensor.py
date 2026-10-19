"""The integration's sensors: the valuation, the live value, the prices in force and the
battery's floor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from microgrid.engine import Regime, ValuationKind
from microgrid.engine.ledger import floor_per_kwh

from .const import ATTR_SLOTS, DOMAIN
from .coordinator import MicrogridConfigEntry, MicrogridCoordinator, Snapshot

#: The battery floor's draw: one marginal kWh, at 1 kW over the hour.
_FLOOR_KWH = 1.0
_FLOOR_KW = _FLOOR_KWH / (timedelta(hours=1) / timedelta(hours=1))


@dataclass(frozen=True, kw_only=True)
class MicrogridSensorDescription(SensorEntityDescription):
    """A sensor and how it reads the coordinator's snapshot."""

    value: Callable[[Snapshot], Any]
    #: Priced: its unit is a currency per kWh.
    priced: bool = False
    #: Only for a plant with a battery.
    battery: bool = False


def _live(snapshot: Snapshot, read: Callable[[Any], Any]) -> Any:
    live = snapshot.live
    if live is None or live.kind is ValuationKind.UNPRICED:
        return None
    return read(live)


def _floor(snapshot: Snapshot, coordinator: MicrogridCoordinator) -> float | None:
    lots = snapshot.lots
    plant = coordinator.setup.plant
    if lots is None or plant.storage is None:
        return None
    return floor_per_kwh(lots.held, plant.battery, draw_kwh=_FLOOR_KWH, draw_kw=_FLOOR_KW)


SENSORS: tuple[MicrogridSensorDescription, ...] = (
    MicrogridSensorDescription(
        key="marginal_cost",
        translation_key="marginal_cost",
        priced=True,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value=lambda s: _live(s, lambda live: live.marginal_cost),
    ),
    MicrogridSensorDescription(
        key="surplus",
        translation_key="surplus",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value=lambda s: _live(s, lambda live: live.surplus_kw),
    ),
    MicrogridSensorDescription(
        key="regime",
        translation_key="regime",
        device_class=SensorDeviceClass.ENUM,
        options=[regime.value for regime in Regime],
        value=lambda s: _live(s, lambda live: live.regime.value),
    ),
    MicrogridSensorDescription(
        key="pv_value",
        translation_key="pv_value",
        priced=True,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value=lambda s: _live(
            s, lambda live: None if live.pv_value is None else live.pv_value.per_kwh
        ),
    ),
    MicrogridSensorDescription(
        key="battery_value",
        translation_key="battery_value",
        priced=True,
        battery=True,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value=lambda s: _live(
            s, lambda live: None if live.battery_value is None else live.battery_value.per_kwh
        ),
    ),
    MicrogridSensorDescription(
        key="import_price",
        translation_key="import_price",
        priced=True,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value=lambda s: None if s.price_now is None else s.price_now.p_import,
    ),
    MicrogridSensorDescription(
        key="export_price",
        translation_key="export_price",
        priced=True,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value=lambda s: None if s.price_now is None else s.price_now.p_export,
    ),
    MicrogridSensorDescription(
        key="valuation",
        translation_key="valuation",
        device_class=SensorDeviceClass.ENUM,
        options=[kind.value for kind in ValuationKind],
        value=lambda s: None if s.published is None else s.published.valuation.kind.value,
    ),
)

#: The battery's floor, read with the plant.
FLOOR = MicrogridSensorDescription(
    key="battery_floor",
    translation_key="battery_floor",
    priced=True,
    battery=True,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=4,
    entity_category=EntityCategory.DIAGNOSTIC,
    value=lambda s: None,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MicrogridConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    has_battery = coordinator.setup.plant.storage is not None
    entities: list[SensorEntity] = [
        MicrogridSensor(coordinator, description)
        for description in SENSORS
        if has_battery or not description.battery
    ]
    if has_battery:
        entities.append(FloorSensor(coordinator, FLOOR))
    async_add_entities(entities)


def device_info(coordinator: MicrogridCoordinator) -> DeviceInfo:
    """The entry's service device."""
    entry = coordinator.config_entry
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        entry_type=DeviceEntryType.SERVICE,
        model="Marginal energy prices",
    )


class MicrogridSensor(CoordinatorEntity[MicrogridCoordinator], SensorEntity):
    """A value from the coordinator's snapshot."""

    entity_description: MicrogridSensorDescription
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({ATTR_SLOTS})

    def __init__(
        self, coordinator: MicrogridCoordinator, description: MicrogridSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._attr_device_info = device_info(coordinator)
        if description.priced:
            self._attr_native_unit_of_measurement = f"{coordinator.hass.config.currency}/kWh"

    @property
    def available(self) -> bool:
        if not super().available or self.coordinator.data is None:
            return False
        published = self.coordinator.data.published
        if self.entity_description.key == "valuation":
            # A valuation past its valid_until is absent to a consumer.
            return published is not None and dt_util.utcnow() < published.valid_until
        return True

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        snapshot = self.coordinator.data
        key = self.entity_description.key
        if key == "valuation":
            return valuation_attributes(snapshot)
        if key in ("marginal_cost", "surplus", "regime") and snapshot.live is not None:
            live = snapshot.live
            return {
                "at": live.at.isoformat(),
                "kind": live.kind.value,
                "certainty_spill": live.certainty_spill,
                "certainty_covered": live.certainty_covered,
                "degraded": [str(cause) for cause in (*live.degraded, *live.external)],
            }
        return None


class FloorSensor(MicrogridSensor):
    """What the battery's next kWh drawn cost the site (its lots)."""

    @property
    def native_value(self) -> Any:
        return _floor(self.coordinator.data, self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        lots = self.coordinator.data.lots
        if lots is None:
            return None
        return {
            "as_of": lots.as_of.isoformat(),
            "held_kwh": {str(source): round(amount.kwh, 3) for source, amount in lots.held.items()},
            "average_cost": (
                lots.pool.unit if lots.pool is not None and lots.pool.kwh > 0 else None
            ),
        }


def valuation_attributes(snapshot: Snapshot) -> dict[str, Any] | None:
    """The published valuation: when, why, until when, what it rests on, and its slots."""
    published = snapshot.published
    if published is None:
        return {"problems": snapshot.problems} if snapshot.problems else None
    valuation = published.valuation
    slots = valuation.slots
    return {
        "issued_at": valuation.now.isoformat(),
        "valid_until": published.valid_until.isoformat(),
        "reason": published.reason.value,
        "degraded": [str(cause) for cause in valuation.degraded],
        "model_version": valuation.model_version,
        "load_forecast": valuation.load_forecast,
        "horizon_end": (
            (slots[-1].start + (slots[1].start - slots[0].start)).isoformat()
            if len(slots) > 1
            else None
        ),
        "inputs": {
            str(name): {
                "as_of": None if state.as_of is None else state.as_of.isoformat(),
                "usable": state.usable,
            }
            for name, state in valuation.inputs.items()
        },
        "sources": snapshot.sources,
        "problems": snapshot.problems,
        ATTR_SLOTS: [
            {
                "start": slot.start.isoformat(),
                "marginal_cost": slot.marginal_cost,
                "marginal_cost_p10": slot.marginal_cost_p10,
                "surplus_kw": slot.surplus_kw,
                "surplus_kw_p10": slot.surplus_kw_p10,
                "p_import": slot.p_import,
                "p_export": slot.p_export,
                "regime": slot.regime.value,
            }
            for slot in slots
        ],
    }
