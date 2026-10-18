"""Example plants, as ``plant/2`` data.

Four topologies, each with synthetic, round parameters a reader can follow. None is
fitted to a real plant:

- ``ac_coupled_hybrid``: AC-coupled PV (micro-inverters) and a battery folded on a
  hybrid inverter's DC bus, its conversion one path from the AC bus to stored
  energy. The grid tops it up overnight. This is the topology the engine was first
  validated on, with made-up numbers.
- ``dc_coupled_hybrid``: DC strings and a battery on a hybrid's DC bus, the battery
  split (its own DC-to-stored step) behind the hybrid's conversion. The hybrid's AC
  rating clips the strings, and the grid's export limit curtails what is left.
- ``pv_only``: two AC arrays with micro-inverters, east and west, and no battery.
- ``battery_only``: a battery on its own inverter, no PV, buying overnight and
  exporting in the evening.

Each is the plain data :meth:`PlantConfig.from_mapping` reads, as a client would
pass it from a file or a config entry: :func:`example_mapping` gives a copy, and
:func:`example_plant` the configuration, in a given time zone (the windows are local
times). :data:`EXAMPLE_NAMES` names them. The engine's tests use them.

Pure: no I/O. The data is here, not in files, because the engine reads none.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo

from microgrid.plant._config import PLANT_SCHEMA, PlantConfig

_AC_COUPLED_HYBRID: Final[Mapping[str, object]] = {
    "pv": [{"name": "roof", "coupling": "ac"}],
    "battery": {
        "capacity_kwh": 12.0,
        "charge_kw": 6.0,
        "discharge_kw": 6.0,
        "eta_charge": 0.96,
        "eta_discharge": 0.96,
        "reserve_soc": 0.1,
        "wear_charge_per_kwh": 0.02,
        "wear_discharge_per_kwh": 0.02,
        "loss_charge_per_kw": 0.003,
        "loss_discharge_per_kw": 0.003,
        # Folded: the battery's meter is on the hybrid's DC bus, so it stores 0.99 per
        # DC kWh it is metered taking in, and draws 1.0 per DC kWh it gives.
        "meter_charge": 0.99,
        "meter_discharge": 1.0,
        "connection": "dc_bus",
        "form": "folded",
    },
    "hybrid": {"standby_kw": 0.03, "working_kw": 0.06},
    "dispatch": {
        "grid_charge": [
            {"start": "01:00:00", "end": "05:00:00", "target_soc": 0.6, "timing": "late"}
        ]
    },
}

_DC_COUPLED_HYBRID: Final[Mapping[str, object]] = {
    "pv": [{"name": "strings", "coupling": "dc", "dc_rating_kw": 8.0}],
    "battery": {
        "capacity_kwh": 10.0,
        "charge_kw": 5.0,
        "discharge_kw": 5.0,
        "eta_charge": 0.98,
        "eta_discharge": 0.98,
        "reserve_soc": 0.1,
        "wear_charge_per_kwh": 0.02,
        "wear_discharge_per_kwh": 0.02,
        "connection": "dc_bus",
        "form": "split",
    },
    "hybrid": {
        "ac_rating_kw": 6.0,
        "standby_kw": 0.03,
        "working_kw": 0.04,
        "conversion": {
            "eta_in": 0.97,
            "eta_out": 0.97,
            "loss_in_per_kw": 0.002,
            "loss_out_per_kw": 0.002,
        },
    },
    "grid": {"export_limit_kw": 5.0},
    "dispatch": {"grid_charge": [{"start": "02:00:00", "end": "05:00:00", "target_soc": 0.5}]},
}

_PV_ONLY: Final[Mapping[str, object]] = {
    "pv": [
        {"name": "east", "coupling": "ac", "dc_rating_kw": 3.5, "ac_rating_kw": 3.0},
        {"name": "west", "coupling": "ac", "dc_rating_kw": 3.5, "ac_rating_kw": 3.0},
    ],
}

_BATTERY_ONLY: Final[Mapping[str, object]] = {
    "battery": {
        "capacity_kwh": 13.5,
        "charge_kw": 5.0,
        "discharge_kw": 5.0,
        "eta_charge": 0.95,
        "eta_discharge": 0.95,
        "reserve_soc": 0.2,
        "wear_charge_per_kwh": 0.03,
        "wear_discharge_per_kwh": 0.03,
        "connection": "ac",
        "inverter": {"ac_rating_kw": 5.0, "standby_kw": 0.03, "working_kw": 0.03},
    },
    "dispatch": {
        "grid_charge": [{"start": "00:00:00", "end": "05:00:00", "target_soc": 0.9}],
        "export_windows": [
            {"start": "17:00:00", "end": "20:00:00", "power_kw": 3.0, "floor_soc": 0.3}
        ],
    },
}

#: Each example's ``plant/2`` mapping, by name, without the schema and zone. Private:
#: a client gets a copy (:func:`example_mapping`), so no one can change the examples
#: for everyone else in the process.
_EXAMPLES: Final[Mapping[str, Mapping[str, object]]] = MappingProxyType(
    {
        "ac_coupled_hybrid": _AC_COUPLED_HYBRID,
        "dc_coupled_hybrid": _DC_COUPLED_HYBRID,
        "pv_only": _PV_ONLY,
        "battery_only": _BATTERY_ONLY,
    }
)

#: The examples' names.
EXAMPLE_NAMES: Final[tuple[str, ...]] = tuple(_EXAMPLES)


def example_mapping(name: str, tz: ZoneInfo) -> dict[str, object]:
    """Example ``name``'s ``plant/2`` mapping in zone ``tz``: a fresh copy each time, to
    read or change."""
    if name not in _EXAMPLES:
        raise KeyError(f"no example plant {name!r}: one of {sorted(_EXAMPLES)}")
    key: str | None = getattr(tz, "key", None)
    if key is None:
        raise ValueError("the zone needs a key (a ZoneInfo by name): plant/2 names its zone")
    return {"schema": PLANT_SCHEMA, "timezone": key, **_copied(_EXAMPLES[name])}


def _copied(data: Mapping[str, object]) -> dict[str, object]:
    """A copy of plain data, tables and lists copied through (no ``deepcopy``: the
    architecture test keeps it out of the engine, as it can re-open a zone's file)."""

    def copy(value: object) -> object:
        if isinstance(value, Mapping):
            return {key: copy(item) for key, item in value.items()}
        if isinstance(value, list):
            return [copy(item) for item in value]
        return value

    return {key: copy(item) for key, item in data.items()}


def example_plant(name: str, tz: ZoneInfo) -> PlantConfig:
    """Example ``name`` as a configuration, its windows in ``tz``'s local time."""
    return PlantConfig.from_mapping(example_mapping(name, tz), tz=tz)
