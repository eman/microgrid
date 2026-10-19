"""For a client's own plant model: the port and the price rule over it.

A plant model is any object that keeps :class:`PlantModel`: it projects a horizon as
:class:`SlotFlow` s, its dispatch's bookkeeping in each slot's :class:`SlotDispatch`,
answers the price rule's questions about its battery through :class:`Storage`, and reads
its meters into :class:`Flows`. ``microgrid.engine.testing.assert_plant_model_contract``
checks one.

The operations the engine is built from, for a client that brings its own flows, traces
or prices (a scorer, a backtest): the price rule over given flows (:func:`price_flows`,
with the noise floors it reads them at: :data:`MODEL` for a plant model's flows,
:data:`OBSERVED` for metered ones; the house importing and PV spilling in a slot, as it
reads them: :func:`importing`, :func:`spilling`), the valuation of given PV scenarios
(:func:`value_energy`) and the surplus forecast (:func:`forecast_surplus`).
"""

from microgrid.engine._surplus import SurplusForecast, forecast_surplus
from microgrid.engine._value import (
    MODEL,
    OBSERVED,
    EnergyValue,
    Event,
    FlowPrices,
    Noise,
    Served,
    importing,
    price_flows,
    spilling,
    value_energy,
)
from microgrid.plant.flows import (
    Flows,
    MeterReading,
    PvKw,
    RunEnd,
    SlotDispatch,
    SlotFlow,
)
from microgrid.plant.port import ChargeSource, PlantModel, Storage

__all__ = [
    "MODEL",
    "OBSERVED",
    "ChargeSource",
    "EnergyValue",
    "Event",
    "FlowPrices",
    "Flows",
    "MeterReading",
    "Noise",
    "PlantModel",
    "PvKw",
    "RunEnd",
    "Served",
    "SlotDispatch",
    "SlotFlow",
    "Storage",
    "SurplusForecast",
    "forecast_surplus",
    "importing",
    "price_flows",
    "spilling",
    "value_energy",
]
