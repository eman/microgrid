"""The valuations and the live value, kept current: one coordinator per config entry.

- **Forecasts** are read from their sources every :data:`~.const.FORECAST_READ`, each
  read one issue received then (the library's readers, through :class:`~.hass_api.HassApi`).
- **Measured** entities feed series as their states change (:mod:`.telemetry`).
- **A valuation** is made every :data:`~.const.DECIDE_EVERY` (``Engine.value``, in the
  executor) and published when the issue policy says (:mod:`.issuing`): on a new
  forecast basis, or on its refresh timer. A valuation with no price for the slot under
  way is not published: the last one lapses at its ``valid_until``.
- **The live value** is run every :data:`~.const.LIVE_EVERY` from the measured series
  and the published valuation (``Engine.live``).
- **The battery's lots** are the ledger's (:mod:`.lots`), run over the recorder's
  history, where the plant has a battery and the configuration names its meters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from microgrid.cli.history import LotsError
from microgrid.cli.inputs import no_dc_forecast
from microgrid.cli.sources import SourceError
from microgrid.cli.sources.read import read_forecast, source_name
from microgrid.engine import Assessment, Engine, Inputs, LiveValue, Valuation, ValuationKind
from microgrid.engine.ledger import Lots
from microgrid.engine.series import Forecast, Price, Sample, Series, slot_floor

from .const import (
    CONF_LOAD_FORECAST,
    CONF_PRICES,
    CONF_PV_FORECAST,
    DECIDE_EVERY,
    DOMAIN,
    FAILURES_FOR_ISSUE,
    FORECAST_READ,
    LIVE_EVERY,
    LOAD_ISSUES,
)
from .hass_api import HassApi
from .issuing import Basis, IssuePolicy, LastIssue, Reason, decide
from .lots import LotsTracker, bindings
from .recorder_history import RecorderHistory
from .setup import Setup, read_setup
from .telemetry import Telemetry

_LOGGER = logging.getLogger(__name__)

type MicrogridConfigEntry = ConfigEntry[MicrogridCoordinator]

#: The input each source section feeds, and the reader's kind.
_KINDS: dict[str, Any] = {
    CONF_PRICES: "prices",
    CONF_PV_FORECAST: "pv_forecast",
    CONF_LOAD_FORECAST: "load_forecast",
}


@dataclass(frozen=True, slots=True)
class Published:
    """A valuation as published: why, and until when it holds."""

    valuation: Valuation
    reason: Reason
    valid_until: datetime


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What the entities show."""

    #: The latest published valuation (None: none yet).
    published: Published | None = None
    #: The live value (None: not run yet).
    live: LiveValue | None = None
    #: The prices in force now, per kWh, from the prices read (None: none).
    price_now: Price | None = None
    lots: Lots | None = None
    #: Each input read, and its source; each not read, and why.
    sources: dict[str, str] = field(default_factory=dict)
    problems: dict[str, str] = field(default_factory=dict)


class MicrogridCoordinator(DataUpdateCoordinator[Snapshot]):
    """Reads the inputs, values them and keeps the live value."""

    config_entry: MicrogridConfigEntry

    def __init__(self, hass: HomeAssistant, entry: MicrogridConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=DECIDE_EVERY,
        )
        self.setup: Setup = read_setup(entry.data, entry.options, hass.config.time_zone)
        self.engine = Engine(self.setup.engine)
        self.policy = IssuePolicy()
        self._api = HassApi(hass)
        plant = self.setup.plant
        measured = self.setup.measured
        self.telemetry = Telemetry(measured, plant.meters) if measured is not None else None
        self._received: dict[str, list[Sample[Forecast[Any]]]] = {}
        self._read_at: datetime | None = None
        self._read_problems: dict[str, str] = {}
        self._read_sources: dict[str, str] = {}
        self._failures: dict[str, int] = {}
        self._last: LastIssue | None = None
        self._published: Published | None = None
        self._live: LiveValue | None = None
        self._lots: LotsTracker | None = None
        self._lots_problem: str | None = None
        self._lots_tried: datetime | None = None
        self.last_valuation: Valuation | None = None

    # -- lifecycle -----------------------------------------------------------------------

    async def async_start(self) -> None:
        """Follow the measured entities and run the live value on its timer."""
        entry = self.config_entry
        if self.telemetry is not None:
            entities = list(self.telemetry.entities())
            now = dt_util.utcnow()
            for entity in entities:
                if (state := self.hass.states.get(entity)) is not None:
                    changed = min(state.last_changed, now)
                    self.telemetry.add(
                        entity,
                        (state.state, state.attributes.get("unit_of_measurement"), changed),
                    )
            entry.async_on_unload(
                async_track_state_change_event(self.hass, entities, self._state_changed)
            )
        entry.async_on_unload(async_track_time_interval(self.hass, self._live_tick, LIVE_EVERY))

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        if self.telemetry is None:
            return
        if state is None:
            text, unit = "unavailable", None
        else:
            text, unit = state.state, state.attributes.get("unit_of_measurement")
        at = state.last_changed if state is not None else dt_util.utcnow()
        self.telemetry.add(event.data["entity_id"], (text, unit, at))

    # -- the forecasts -------------------------------------------------------------------

    async def _read_forecasts(self, now: datetime) -> None:
        plant = self.setup.plant
        problems: dict[str, str] = {}
        used: dict[str, str] = {}
        for name, kind in _KINDS.items():
            if name == CONF_PV_FORECAST and not plant.has_pv:
                continue
            if name == CONF_PV_FORECAST and plant.pv_per_array:
                problems[name] = (
                    "this plant takes a PV forecast per array, which the integration does"
                    " not read yet"
                )
                continue
            source = self.setup.sources.get(name)
            if source is None:
                problems[name] = "no source configured"
                continue
            try:
                forecast = await read_forecast(
                    self._api, source, kind, self.setup.engine, now, tz=plant.config.tz
                )
            except (SourceError, ValueError) as err:
                problems[name] = str(err)
                self._failed(name, str(err))
                continue
            self._read_again(name)
            used[name] = source_name(source)
            kept = self._received.setdefault(name, [])
            kept.append(Sample(now, forecast))
            del kept[: -(LOAD_ISSUES if name == CONF_LOAD_FORECAST else 1)]
        self._read_problems, self._read_sources = problems, used
        self._read_at = now

    def _failed(self, name: str, why: str) -> None:
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        if count == FAILURES_FOR_ISSUE:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{self.config_entry.entry_id}_{name}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="source_failing",
                translation_placeholders={
                    "entry": self.config_entry.title,
                    "input": name,
                    "error": why,
                },
            )

    def _read_again(self, name: str) -> None:
        if self._failures.pop(name, 0) >= FAILURES_FOR_ISSUE:
            ir.async_delete_issue(self.hass, DOMAIN, f"{self.config_entry.entry_id}_{name}")

    def _series(self, name: str, now: datetime) -> Series[Forecast[Any]] | None:
        kept = self._received.get(name)
        return Series(tuple(kept), now) if kept else None

    def price_now(self, now: datetime) -> Price | None:
        """The prices in force at ``now``, from the prices last read."""
        kept = self._received.get(CONF_PRICES)
        if not kept or kept[-1].value is None:
            return None
        for interval in kept[-1].value.intervals:
            if interval.start <= now < interval.end:
                value: Price = interval.value
                return value
        return None

    # -- the battery's lots --------------------------------------------------------------

    def _price_entities(self) -> tuple[str | None, str | None]:
        registry = er.async_get(self.hass)
        found = [
            registry.async_get_entity_id("sensor", DOMAIN, f"{self.config_entry.entry_id}_{key}")
            for key in ("import_price", "export_price")
        ]
        return found[0], found[1]

    async def _update_lots(self, now: datetime) -> None:
        plant = self.setup.plant
        measured = self.setup.measured
        if plant.storage is None:
            return
        if self.setup.engine.pricing.battery_value != "stored_cost":
            return  # the make-up rule reads no lots
        slot = self.setup.engine.slot
        if self._lots is None:
            if measured is None:
                self._lots_problem = "no measured entities to run the ledger on"
                return
            import_price, export_price = self._price_entities()
            if import_price is None or export_price is None:
                self._lots_problem = "the price sensors are not registered yet"
                return
            try:
                bound = bindings(
                    plant, measured, import_price=import_price, export_price=export_price
                )
            except LotsError as err:
                self._lots_problem = str(err)
                return
            self._lots = LotsTracker(RecorderHistory(self.hass), bound, slot)
        if self._lots_tried is not None and slot_floor(now, slot) == self._lots_tried:
            return  # one try a slot
        self._lots_tried = slot_floor(now, slot)
        try:
            await self._lots.update(now)
        except (LotsError, ValueError) as err:
            self._lots_problem = str(err)
            return
        except Exception as err:  # the recorder's own failure: the lots go stale
            _LOGGER.warning("battery lots: %s: %s", type(err).__name__, err)
            self._lots_problem = f"{type(err).__name__}: {err}"
            return
        self._lots_problem = None

    def _lots_series(self, now: datetime) -> tuple[Series[Lots] | None, Lots | None]:
        lots = self._lots.current(now) if self._lots is not None else None
        if lots is None or lots.as_of > now:
            return None, None
        return Series.of([(lots.as_of, lots)], known_until=now), lots

    # -- a valuation ---------------------------------------------------------------------

    def _inputs(self, now: datetime) -> Inputs:
        plant = self.setup.plant
        engine = self.setup.engine
        telemetry = self.telemetry
        has_battery = plant.storage is not None
        lots, _ = self._lots_series(now)
        dc = no_dc_forecast(now, engine.horizon, engine.slot) if plant.has_dc_loads else None
        return Inputs(
            prices=self._series(CONF_PRICES, now),
            pv=self._series(CONF_PV_FORECAST, now) if plant.has_pv else None,
            load=self._series(CONF_LOAD_FORECAST, now),
            dc_load=Series((Sample(now, dc),), now) if dc is not None else None,
            soc=(
                telemetry.soc.series(now)
                if has_battery and telemetry is not None and telemetry.soc is not None
                else None
            ),
            dispatch_as_modelled=(
                telemetry.dispatch_series(now) if has_battery and telemetry is not None else None
            ),
            lots=lots,
        )

    async def _async_update_data(self) -> Snapshot:
        now = dt_util.utcnow()
        if self._read_at is None or now - self._read_at >= FORECAST_READ:
            await self._read_forecasts(now)
        await self._update_lots(now)
        inputs = self._inputs(now)
        valuation = await self.hass.async_add_executor_job(
            partial(self.engine.value, inputs, at=now)
        )
        self.last_valuation = valuation
        if valuation.kind is not ValuationKind.UNPRICED:
            basis = Basis(
                pv_issued_at=_issued(self._received.get(CONF_PV_FORECAST)),
                load_issued_at=_issued(self._received.get(CONF_LOAD_FORECAST)),
                prices={slot.start: (slot.p_import, slot.p_export) for slot in valuation.slots},
            )
            assessment = Assessment(valuation.kind, tuple(map(str, valuation.degraded)))
            reason = decide(self.policy, valuation.now, self._last, basis, assessment)
            if reason is not None:
                valid_until = valuation.now + self.policy.valid_for
                self._published = Published(valuation, reason, valid_until)
                self._last = LastIssue(valuation.now, valid_until, basis, assessment)
        return self._snapshot(now)

    def _snapshot(self, now: datetime) -> Snapshot:
        problems = dict(self._read_problems)
        if self._lots_problem is not None:
            problems["battery_lots"] = f"{self._lots_problem} (all it holds priced at the import)"
        _, lots = self._lots_series(now)
        return Snapshot(
            published=self._published,
            live=self._live,
            price_now=self.price_now(now),
            lots=lots,
            sources=dict(self._read_sources),
            problems=problems,
        )

    # -- the live value ------------------------------------------------------------------

    def _live_inputs(self, now: datetime) -> Inputs:
        plant = self.setup.plant
        telemetry = self.telemetry
        lots, _ = self._lots_series(now)
        if telemetry is None:
            return Inputs(prices=None, lots=lots)
        telemetry.prune(now, self.setup.engine.smoothing_window)
        has_battery = plant.storage is not None
        meters = {
            name: found
            for name, buffer in telemetry.meters.items()
            if (found := buffer.series(now)) is not None
        }
        return Inputs(
            prices=None,
            soc=telemetry.soc.series(now) if has_battery and telemetry.soc is not None else None,
            dispatch_as_modelled=telemetry.dispatch_series(now) if has_battery else None,
            meters=meters,
            measured_load=telemetry.house.series(now) if telemetry.house is not None else None,
            lots=lots,
        )

    async def _live_tick(self, _now: datetime | None = None) -> None:
        now = dt_util.utcnow()
        published = self._published
        self._live = await self.hass.async_add_executor_job(
            partial(
                self.engine.live,
                self._live_inputs(now),
                at=now,
                valuation=published.valuation if published is not None else None,
                valid_until=published.valid_until if published is not None else None,
            )
        )
        # Not async_set_updated_data: that puts the next valuation off by a whole
        # interval, and the live value runs more often than the valuations.
        self.data = self._snapshot(now)
        self.async_update_listeners()

    async def async_run_live(self) -> None:
        """Run the live value now (the first, before the timer's)."""
        await self._live_tick()


def _issued(kept: list[Sample[Forecast[Any]]] | None) -> datetime | None:
    if not kept or kept[-1].value is None:
        return None
    return kept[-1].value.issued_at
