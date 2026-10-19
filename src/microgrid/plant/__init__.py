"""The plant model and its simulator (``microgrid.plant``).

A plant is described by a :class:`PlantConfig` (``plant/2``), given as data.
:class:`Plant` is that plant behind the port
(:class:`~microgrid.plant.port.PlantModel`): one configurable model, running every
configuration through one dispatch. ``Plant.project`` projects
the slots it is given from a state of charge, PV and load, and returns their flows
(:class:`~microgrid.plant.flows.SlotFlow`): where every kWh goes, the battery's charge,
what is imported and exported.

The public names: :class:`PlantConfig` and every part of it (the battery's parameters,
:class:`Battery`, the grid-charge windows, :class:`GridCharge`, ...), :class:`Plant`,
:class:`PlantConfigError`, the inverter's working threshold (:data:`WORKING_KW`), the
flow types (:mod:`.flows`), the port a plant model implements (:mod:`.port`), and
``examples``.
"""

from microgrid.plant import examples, flows, port
from microgrid.plant._battery import Battery
from microgrid.plant._config import (
    BATTERY_FIELDS,
    PLANT_SCHEMA,
    WORKING_KW,
    BatteryPart,
    Conversion,
    Dispatch,
    ExportWindow,
    GridCharge,
    GridLimits,
    HoldRule,
    Inverter,
    Load,
    Meter,
    PlantConfig,
    PlantConfigError,
    PvArray,
)
from microgrid.plant._plant import Plant
from microgrid.plant.flows import Flows, SlotFlow
from microgrid.plant.port import ChargeSource, PlantModel, Storage

__all__ = [
    "BATTERY_FIELDS",
    "PLANT_SCHEMA",
    "WORKING_KW",
    "Battery",
    "BatteryPart",
    "ChargeSource",
    "Conversion",
    "Dispatch",
    "ExportWindow",
    "Flows",
    "GridCharge",
    "GridLimits",
    "HoldRule",
    "Inverter",
    "Load",
    "Meter",
    "Plant",
    "PlantConfig",
    "PlantConfigError",
    "PlantModel",
    "PvArray",
    "SlotFlow",
    "Storage",
    "examples",
    "flows",
    "port",
]
