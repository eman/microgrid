"""A battery's conversion and limits, as the plant model needs them (``plant/2``'s
``[battery]``), and the legacy dispatch's grid connection.

The battery's conversion curves and limits are the plant model's, and the price model
asks about them only through ``Storage``. A battery on its own inverter converts through
this law; one on a hybrid's DC bus folds it into the hybrid's curve (``folded``) or holds
it between the DC bus and its cells (``split``; ``plant._paths.Port``).

Pure: no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from microgrid._checks import require_finite


@dataclass(frozen=True, slots=True)
class Battery:
    """A battery, as the plant model needs it.

    ``reserve_soc`` is the discharge floor the plant keeps (backup reserve);
    ``saturation_soc`` is where charging is treated as full, below 1.0 because
    the BMS tapers the charge rate near the top.

    **Conversion** (fitted by ``microgrid fit-battery``). What converts its port's power to
    stored energy and back (on its own inverter, AC; folded on a hybrid's DC bus, the
    hybrid's AC side) loses more with the power converted. Charging
    at ``P`` kW DC takes ``P / eta_c' + loss_charge_per_kw x P^2`` kW AC and
    stores ``meter_charge x P``; discharging ``P`` kW DC draws ``meter_discharge
    x P`` stored and delivers ``eta_d' x P - loss_discharge_per_kw x P^2`` AC,
    where ``eta_c' = eta_charge / meter_charge`` and ``eta_d' = eta_discharge x
    meter_discharge``. So ``eta_charge`` (AC to stored) and ``eta_discharge``
    (stored to AC) are the efficiencies at low power, and the loss terms how
    they fall with power. ``meter_charge`` and ``meter_discharge`` are the
    battery's own meter, which reads DC at the battery (1: it reads stored
    energy). The inverter's fixed draws are the plant's
    (``Inverter.standby_kw``, ``working_kw``). The defaults (no loss
    growth, a meter that reads stored energy) are one efficiency each way.

    **Wear** is the battery's own cost of a kWh through it, per AC kWh, each
    direction (``docs/methodology/stored-value.md``): the hurdle a charge or a
    discharge has to clear, which :meth:`dead_band` and :meth:`minimum_spread`
    apply.
    """

    capacity_kwh: float
    charge_kw: float
    discharge_kw: float
    eta_charge: float = 0.94
    eta_discharge: float = 0.94
    reserve_soc: float = 0.0
    saturation_soc: float = 1.0
    #: Wear per AC kWh charged, and per AC kWh discharged, in the prices' currency.
    wear_charge_per_kwh: float = 0.0
    wear_discharge_per_kwh: float = 0.0
    #: The conversion's loss growth, AC kW per (DC kW)^2, each way.
    loss_charge_per_kw: float = 0.0
    loss_discharge_per_kw: float = 0.0
    #: Stored kWh per metered kWh charged, and drawn per metered kWh discharged.
    meter_charge: float = 1.0
    meter_discharge: float = 1.0

    def __post_init__(self) -> None:
        require_finite(
            "battery",
            capacity_kwh=self.capacity_kwh,
            charge_kw=self.charge_kw,
            discharge_kw=self.discharge_kw,
            wear_charge_per_kwh=self.wear_charge_per_kwh,
            wear_discharge_per_kwh=self.wear_discharge_per_kwh,
            loss_charge_per_kw=self.loss_charge_per_kw,
            loss_discharge_per_kw=self.loss_discharge_per_kw,
            meter_charge=self.meter_charge,
            meter_discharge=self.meter_discharge,
        )
        for name in (
            "wear_charge_per_kwh",
            "wear_discharge_per_kwh",
            "loss_charge_per_kw",
            "loss_discharge_per_kw",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"battery {name} must not be negative")
        for name in ("capacity_kwh", "charge_kw", "discharge_kw"):
            if getattr(self, name) <= 0:
                raise ValueError(f"battery {name} must be positive")
        # Plausible ranges: fitted on a real plant's meters (microgrid fit-battery), the
        # efficiencies come out at 0.95-0.99 and the meter factors at 0.99-1.02. Less than
        # half each way is a fault, not a parameter, and beyond these the conversion's
        # arithmetic leaves the floats.
        for name in ("eta_charge", "eta_discharge"):
            if not 0.5 < getattr(self, name) <= 1:
                raise ValueError(f"battery {name} must be in (0.5, 1]")
        for name in ("meter_charge", "meter_discharge"):
            if not 0.5 < getattr(self, name) <= 1.5:
                raise ValueError(f"battery {name} must be in (0.5, 1.5]")
        if not 0 <= self.reserve_soc < self.saturation_soc <= 1:
            raise ValueError("battery needs 0 <= reserve_soc < saturation_soc <= 1")
        if self.loss_discharge_per_kw > 0:
            try:
                deliverable = self._dc_out(self.discharge_kw) is not None
            except ZeroDivisionError:  # a conversion that underflows to nothing
                deliverable = False
            if not deliverable:
                raise ValueError("battery cannot deliver discharge_kw with that loss growth")

    # AC per DC kW at low power, each way.
    @property
    def _ac_per_dc_in(self) -> float:
        return self.meter_charge / self.eta_charge

    @property
    def _ac_per_dc_out(self) -> float:
        return self.eta_discharge * self.meter_discharge

    def _dc_in(self, ac_kw: float) -> float:
        """DC kW a charge of ``ac_kw`` AC puts through the meter."""
        k, q = self._ac_per_dc_in, self.loss_charge_per_kw
        if ac_kw <= 0:
            return ac_kw / k
        # The root of q P^2 + k P = ac, in the form that stays exact as q -> 0.
        return 2 * ac_kw / (k + math.sqrt(k * k + 4 * q * ac_kw))

    def _dc_out(self, ac_kw: float) -> float | None:
        """DC kW a discharge delivering ``ac_kw`` AC takes; None beyond the inverter."""
        k, q = self._ac_per_dc_out, self.loss_discharge_per_kw
        if ac_kw <= 0:
            return ac_kw / k
        disc = k * k - 4 * q * ac_kw
        # The smaller root of k P - q P^2 = ac, in the form exact as q -> 0.
        return None if disc < 0 else 2 * ac_kw / (k + math.sqrt(disc))

    def stored_by(self, ac_kw: float) -> float:
        """Stored kW a charge of ``ac_kw`` AC adds."""
        return self.meter_charge * self._dc_in(ac_kw)

    def charge_for(self, stored_kw: float) -> float:
        """AC kW that adds ``stored_kw`` stored (the inverse of :meth:`stored_by`)."""
        return self.charge_ac(stored_kw / self.meter_charge)

    def drawn_by(self, ac_kw: float) -> float:
        """Stored kW a discharge delivering ``ac_kw`` AC draws."""
        dc = self._dc_out(ac_kw)
        if dc is None:
            raise ValueError(f"the inverter cannot deliver {ac_kw} kW")
        return self.meter_discharge * dc

    def delivered_by(self, drawn_kw: float) -> float:
        """AC kW delivered by drawing ``drawn_kw`` stored (the inverse of :meth:`drawn_by`),
        at most the inverter's peak."""
        k, q = self._ac_per_dc_out, self.loss_discharge_per_kw
        dc = drawn_kw / self.meter_discharge
        if q > 0:
            dc = min(dc, k / (2 * q))
        return self.discharge_ac(dc)

    def charge_ac(self, metered_kw: float) -> float:
        """AC kW a charge the battery's meter reads as ``metered_kw`` took."""
        return self._ac_per_dc_in * metered_kw + self.loss_charge_per_kw * metered_kw**2

    def discharge_ac(self, metered_kw: float) -> float:
        """AC kW a discharge the battery's meter reads as ``metered_kw`` delivered."""
        return self._ac_per_dc_out * metered_kw - self.loss_discharge_per_kw * metered_kw**2

    def eta_charge_at(self, ac_kw: float = 0.0) -> float:
        """Stored per AC kWh, for one more kWh charged while charging at ``ac_kw``."""
        dc = self._dc_in(max(ac_kw, 0.0))
        return self.meter_charge / (self._ac_per_dc_in + 2 * self.loss_charge_per_kw * dc)

    def eta_discharge_at(self, ac_kw: float = 0.0) -> float:
        """AC per stored kWh, for one more kWh delivered while delivering ``ac_kw``."""
        dc = self._dc_out(max(ac_kw, 0.0))
        if dc is None:
            raise ValueError(f"the inverter cannot deliver {ac_kw} kW")
        return (self._ac_per_dc_out - 2 * self.loss_discharge_per_kw * dc) / self.meter_discharge

    @property
    def round_trip(self) -> float:
        """AC out per AC in, at low power."""
        return self.eta_charge * self.eta_discharge

    @property
    def wear_per_delivered_kwh(self) -> float:
        """The wear a delivered AC kWh carries: it was charged as ``1 /
        round_trip`` AC kWh, and then discharged (at low power)."""
        return self.wear_charge_per_kwh / self.round_trip + self.wear_discharge_per_kwh

    def refill_value(self, price: float, *, charging_kw: float = 0.0) -> float:
        """The cost per stored kWh the plant puts back by charging at ``price`` per AC
        kWh, while charging at ``charging_kw``: ``(p + c_ch) / eta_c``."""
        return (price + self.wear_charge_per_kwh) / self.eta_charge_at(charging_kw)

    def displaced_value(self, price: float, *, discharging_kw: float = 0.0) -> float:
        """The worth per stored kWh that would have served a load in place of an import
        at ``price``, delivered while delivering ``discharging_kw``: ``eta_d (p - c_dis)``,
        its AC kWh less their discharge wear."""
        eta = self.eta_discharge_at(discharging_kw)
        return eta * (price - self.wear_discharge_per_kwh)

    def dead_band(
        self, stored_value: float, *, charging_kw: float = 0.0, discharging_kw: float = 0.0
    ) -> tuple[float, float]:
        """Where the battery holds, for stored energy worth ``stored_value`` per stored
        kWh (``docs/methodology/stored-value.md``, *The dead band*): charging pays below the first
        (``eta_c v - c_ch``), discharging above the second (``v / eta_d + c_dis``),
        each at the margin of the power already flowing. They are also what a PV kWh
        diverted from charging, and a battery kWh drawn, cost. For stored energy worth
        less than nothing (a strongly negative export price) the band inverts: losses
        then burn energy that costs to keep, and the first is the dearer."""
        return (
            self.eta_charge_at(charging_kw) * stored_value - self.wear_charge_per_kwh,
            stored_value / self.eta_discharge_at(discharging_kw) + self.wear_discharge_per_kwh,
        )

    def minimum_spread(self, charge_price: float) -> float:
        """The least a use must be worth, per kWh, for a kWh charged at ``charge_price``
        to be discharged into it, at low power: ``(p_c + c_ch) / eta_rt + c_dis``
        (``docs/methodology/stored-value.md``, *The minimum spread*)."""
        return self.dead_band(self.refill_value(charge_price))[1]


@dataclass(frozen=True, slots=True)
class Grid:
    """The grid connection. ``export_limit_kw`` is None when no limit binds."""

    export_limit_kw: float | None = None

    def __post_init__(self) -> None:
        if self.export_limit_kw is not None:
            require_finite("grid", export_limit_kw=self.export_limit_kw)
            if self.export_limit_kw < 0:
                raise ValueError("grid export_limit_kw must not be negative")
