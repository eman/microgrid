"""The battery's lots, live: the library's ledger run over what the recorder kept.

When the integration starts, the ledger runs in over :data:`RUN_IN` of history (all the
battery held before then of unknown origin); then each completed slot is read on its own
and stepped. The meters, the SOC and the house load are the measured entities; the
prices each slot was charged at are the integration's own import and export price
sensors, as recorded. A failed read advances nothing and the lots go stale:
:meth:`LotsTracker.current` gives them only while fresh and while the last slot was
metered. Longer than :data:`RESTART_AFTER` without a step (Home Assistant was down) and
the ledger runs in again.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from microgrid.cli.history import (
    TELEMETRY,
    Bindings,
    DcLoadMeter,
    History,
    LotsError,
    read_recorded,
    step_slots,
)
from microgrid.cli.sources.config import Measured
from microgrid.engine.ledger import Ledger, Lots
from microgrid.engine.series import require_utc, slot_floor
from microgrid.plant import Plant

#: How far back the ledger starts: two days drew down what the battery held before
#: (unknown energy) in a 34-day run of one home's meters.
RUN_IN: Final = timedelta(days=2)
#: A completed slot is read once the recorder has written its last samples.
SETTLE: Final = timedelta(seconds=30)
#: Longer without a step and the ledger runs in again.
RESTART_AFTER: Final = timedelta(hours=6)
#: A DC-bus load's last reading carried into a window from this far back.
_DC_CARRY_IN: Final = timedelta(days=1)
#: A DC-bus load's reading other than 0 silent this long is stale.
_DC_STALE_AFTER: Final = timedelta(minutes=5)


def bindings(plant: Plant, measured: Measured, *, import_price: str, export_price: str) -> Bindings:
    """Which recorded entity is which, for ``plant``. Raises :class:`LotsError` naming
    what the ledger needs and the configuration does not name."""
    by_kind = {kind: name for name, kind in plant.meters.items()}
    missing = [kind for kind in ("pv", "grid", "battery") if kind not in by_kind]
    if missing:
        raise LotsError(f"the plant meters no {', '.join(missing)}: the ledger reads them")
    unnamed = [name for name in plant.meters if name not in measured.meters]
    if unnamed:
        raise LotsError(f"no entity for the plant's meters {', '.join(unnamed)}")
    if measured.soc is None or measured.house is None:
        raise LotsError("the ledger reads the battery's SOC and the house load")
    entity = {name: measured.meters[name] for name in plant.meters}
    return Bindings(
        plant=plant,
        pv=entity[by_kind["pv"]],
        grid=entity[by_kind["grid"]],
        battery=entity[by_kind["battery"]],
        soc=measured.soc,
        soc_unit=measured.soc_unit,
        house=measured.house,
        heartbeat=measured.heartbeat or measured.house,
        telemetry=TELEMETRY,
        import_price=import_price,
        export_price=export_price,
        inverter=measured.inverter,
        dc_load=(
            DcLoadMeter(entity[by_kind["dc_load"]], _DC_CARRY_IN, _DC_STALE_AFTER)
            if "dc_load" in by_kind and plant.has_dc_loads
            else None
        ),
    )


class LotsTracker:
    """The battery's live lots: run in from the recorder, then advanced a slot at a
    time."""

    def __init__(
        self,
        history: History,
        bound: Bindings,
        slot: timedelta,
        *,
        run_in: timedelta = RUN_IN,
    ) -> None:
        self._history = history
        self._bindings = bound
        self._slot = slot
        self._run_in = run_in
        self._max_age = 3 * slot
        self._ledger: Ledger | None = None
        self._through: datetime | None = None
        #: Whether the last slot stepped was metered, with a SOC at its end.
        self._complete = False

    async def update(self, now: datetime) -> None:
        """Step the ledger through the last completed slot. Raises on a failed read
        (:class:`LotsError`, or the recorder's own)."""
        require_utc(now)
        plant = self._bindings.plant
        end = slot_floor(now - SETTLE, self._slot)
        if self._ledger is None or self._through is None or end - self._through > RESTART_AFTER:
            recorded = await read_recorded(
                self._history, self._bindings, end - self._run_in, end, step=self._slot
            )
            if recorded.stored_before is None:
                raise LotsError("no battery SOC at the ledger's start")
            ledger = Ledger(
                plant.battery, unknown_kwh=recorded.stored_before, storage=plant.storage
            )
        elif end > self._through:
            recorded = await read_recorded(
                self._history, self._bindings, self._through, end, step=self._slot
            )
            ledger = self._ledger
        else:
            return
        slots = step_slots(ledger, recorded, plant)
        self._ledger, self._through = ledger, end
        last = len(recorded.starts) - 1
        self._complete = bool(slots) and (
            recorded.flows[last] is not None and recorded.stored_after[last] is not None
        )

    def current(self, now: datetime) -> Lots | None:
        """The lots, if fresh and the last slot was metered; else None."""
        if self._ledger is None or self._through is None or not self._complete:
            return None
        if now - self._through > self._max_age:
            return None
        return Lots(as_of=self._through, held=self._ledger.held, pool=self._ledger.pool)

    @property
    def through(self) -> datetime | None:
        """The end of the last slot stepped."""
        return self._through
