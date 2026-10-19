"""The microgrid engine: the model that values energy by time and source.

The contract is what this module exports; every other module (``_value``,
``plant._paths``, ...) is private.

    engine = Engine(EngineConfig(plant=plant.Plant(plant.PlantConfig(tz=..., pv=..., ...))))
    # what the battery holds and what it cost: the ledger over the meters' recent record
    # (``ledger.integrate`` the meters, ``ledger.Ledger.step`` each slot; a ``Lots`` after
    # each), as a series; without it the battery's energy is priced as bought and the
    # valuation names ``battery_lots``
    lots = series.Series.of([(slot_end, ledger.Lots(slot_end, held, pool)), ...],
                            known_until=...)
    # time series, each read as it stood at the moment:
    valuation = engine.value(Inputs(prices=..., pv=..., load=..., soc=...,
                                    dispatch_as_modelled=..., lots=lots), at=...)
    # the live value: the valuation run again from the measured series at a moment
    value = engine.live(Inputs(prices=None, soc=..., dispatch_as_modelled=...,
                               meters={"pv": ..., "grid": ..., "battery": ...},
                               measured_load=..., lots=lots),
                        at=..., valuation=valuation, valid_until=...)
    # or, for readings that arrive one at a time
    session = engine.live_session()
    value = session.update(LiveReading(at=..., load_kw=..., soc=..., dispatch_as_modelled=...,
                                       meters={"pv": ..., "grid": ..., "battery": ...}),
                           valuation, valid_until)

The ``microgrid`` command does all of this on a dataset, or on a site's sources now
(``microgrid.cli``; ``microgrid now`` runs the ledger over the recorded history).

Every valuation and live value has a ``kind`` (``ValuationKind``: ``unpriced``, with no
price for the slot under way, among them) and names the engine's causes (``Cause``) in
``degraded``; a live value keeps the client's own beside them (``external``). A gap in
the load forecast that cut the horizon is ``Valuation.horizon_cut``. The versions:
``ENGINE_CONTRACT`` (``engine-api/5``), this API's; ``ENGINE_SCHEMA`` (``engine/2``),
the configuration's; ``VALUATION_SCHEMA`` (``valuation/2``), the outputs'; and
``MODEL_VERSION`` (``pricing/1.3``), the price rule and plant model each valuation
names.

**The public names, in tiers**; every other module is private:

- this module: ``Engine`` and its configuration (``EngineConfig``, ``Pricing``,
  ``EngineConfigError``, ``ENGINE_SCHEMA``), ``Inputs``, the live session
  (``LiveSession``, ``LiveReading``), the outputs (``Valuation``, ``SlotValue``,
  ``LiveValue``, ``PvValue``, ``BatteryValue``, ``Step``), their enums and parts
  (``ValuationKind``, ``Regime``, ``Cause``, ``Served``, ``PvRouting``, ``Displaces``,
  ``InputState``, ``Assessment``), each output's ``to_mapping`` (plain data, its
  schema ``valuation/2`` shipped in ``schemas/``) and the versions.
- ``microgrid.engine.series``: the form a client's data takes (``Series``, values held
  until the next sample and known up to ``known_until``; ``Forecast``), the operations
  on it, and the slot grid (``SLOT``, ``slot_starts``, ...).
- ``microgrid.engine.units``: a series in the engine's units.
- ``microgrid.plant``: the plant a configuration describes (``Plant``,
  ``PlantConfig`` and its parts, ``Battery`` and ``GridCharge`` among them),
  behind the port, and ``examples``.
- ``microgrid.engine.ledger``: the battery's lots, where the energy it holds came from
  and what it cost (``Ledger``, ``Lots``).
- ``microgrid.engine.analysis``: the models checked against what the meters did
  (``scoring``: what a kWh realised; ``ground_truth``: the projection over metered
  inputs).
- ``microgrid.engine.extend``: for a client's own plant model, the port (``PlantModel``,
  ``Storage``, ``ChargeSource``, ``SlotFlow``) and the operations the engine is built
  from (``price_flows``, ``value_energy``, ``forecast_surplus``).
- ``microgrid.engine.testing``: ``assert_plant_model_contract``, for such a model.

A plant may have a DC bus: DC-coupled PV, a battery folded or split on a hybrid's
DC bus, the hybrid's AC rating, a hybrid beside a battery on its own inverter. A plant
may have no battery or no PV. Without a battery its ``storage`` is None and its SOCs are
None, and its inputs take no SOC or dispatch.
Without PV it takes no PV forecast. Without either, every kWh is the import price.
"""

from microgrid.engine._api import (
    ENGINE_CONTRACT,
    MODEL_VERSION,
    VALUATION_SCHEMA,
    Engine,
    EngineConfig,
    LiveReading,
    LiveSession,
    LiveValue,
    SlotValue,
    Valuation,
)
from microgrid.engine._assess import Assessment, Cause, InputState, ValuationKind
from microgrid.engine._engine_config import ENGINE_SCHEMA, EngineConfigError
from microgrid.engine._inputs import Inputs
from microgrid.engine._supply import Step
from microgrid.engine._value import (
    BatteryValue,
    Displaces,
    Pricing,
    PvRouting,
    PvValue,
    Served,
)
from microgrid.plant.flows import Regime

__all__ = [
    "ENGINE_CONTRACT",
    "ENGINE_SCHEMA",
    "MODEL_VERSION",
    "VALUATION_SCHEMA",
    "Assessment",
    "BatteryValue",
    "Cause",
    "Displaces",
    "Engine",
    "EngineConfig",
    "EngineConfigError",
    "InputState",
    "Inputs",
    "LiveReading",
    "LiveSession",
    "LiveValue",
    "Pricing",
    "PvRouting",
    "PvValue",
    "Regime",
    "Served",
    "SlotValue",
    "Step",
    "Valuation",
    "ValuationKind",
]
