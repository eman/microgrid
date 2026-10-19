"""The problem sensor: on while the published valuation is not the stored-value rule's
on every input (a fallback, none yet, or lapsed)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from microgrid.engine import ValuationKind

from .coordinator import MicrogridConfigEntry, MicrogridCoordinator
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MicrogridConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ProblemSensor(entry.runtime_data)])


class ProblemSensor(CoordinatorEntity[MicrogridCoordinator], BinarySensorEntity):
    """On while the valuation falls back, is missing, or has lapsed."""

    _attr_has_entity_name = True
    _attr_translation_key = "problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: MicrogridCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_problem"
        self._attr_device_info = device_info(coordinator)

    @property
    def is_on(self) -> bool:
        published = self.coordinator.data.published
        if published is None or dt_util.utcnow() >= published.valid_until:
            return True
        return published.valuation.kind is not ValuationKind.STORED_VALUE_V1

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        snapshot = self.coordinator.data
        published = snapshot.published
        return {
            "degraded": (
                [] if published is None else [str(c) for c in published.valuation.degraded]
            ),
            "problems": snapshot.problems,
        }
