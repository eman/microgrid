"""A plant's configuration, as data.

The plant model takes its topology and parameters from a :class:`PlantConfig`: PV
arrays (any number, none included, AC- or DC-coupled), a battery or none (on the
AC bus behind its own inverter, or on a hybrid inverter's DC bus, folded or
split), the hybrid inverter if there is one, the grid's limits, the loads by
connection point, and the dispatch's parts. Every combination the checks accept
is a valid plant, and :class:`~microgrid.plant.Plant` runs every one.

**Validation names every problem with its path** (``battery.capacity_kwh: must be
positive``), in :class:`PlantConfigError`: every field's and every part's at
once, then the rules across parts once the parts are sound (a rule across parts
cannot be judged on a part that is wrong).

**Plain data both ways:** :meth:`PlantConfig.from_mapping` and
:meth:`PlantConfig.to_mapping` take and give dicts, lists, numbers and strings
(the ``plant/2`` schema), and round-trip exactly. A key given as null is a key
left out.

**Known limit of plant/2:** a grid-charge or export window lies within one local
day (its start before its end). A window across midnight would be an additive
change, an end at or before the start meaning the next day.

Pure: no I/O. The time zone arrives as a :class:`~zoneinfo.ZoneInfo` the client
built: resolving a zone name reads the system's time-zone database, so nothing
here does, and the mapping's ``timezone`` is only compared with the zone's key.
The zone is held once, at the top, and never copied (``asdict`` and ``deepcopy`` of a
zone can re-open its file), so :meth:`PlantConfig.to_mapping`
is written out field by field.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import time
from typing import Final, Literal, cast
from zoneinfo import ZoneInfo

from microgrid.plant._battery import Battery

#: The mapping's schema. It changes only by version, and grows additively within one.
#: ``plant/2``: the wear's keys name no currency (``wear_charge_per_kwh``,
#: ``wear_discharge_per_kwh``; ``plant/1`` said ``usd``), as money is in the prices' own.
PLANT_SCHEMA: Final = "plant/2"

#: The battery works, and the inverter draws ``Inverter.working_kw`` more, above
#: this, kW either way: idle, the battery's meter reads under 0.02 kW in 306 of the
#: 351 slots under 0.05 kW, and the counts climb from 0.05 kW up (measured on the
#: reference plant's meters, 30 days to 2026-09-25; ``microgrid fit-battery`` prints
#: both, and fits the draw with it).
WORKING_KW = 0.05


@dataclass(frozen=True, slots=True)
class GridCharge:
    """The plant's off-peak grid-charge window, in local time.

    Inside the window the grid tops the battery up to a target that PV alone
    would not reach by ``end``. The target is ``target_soc``, or, when
    ``cover_until`` is given, what the house needs from ``end`` until then (the
    expensive hours, net of the PV then and of discharge losses), whichever is
    lower. ``cover_until`` at or before ``end`` means that time the next day
    (midnight: ``00:00``); within the window's own hours it is refused, as the next
    day's window tops up first.

    ``timing`` is when in the window the grid tops up: ``"early"``, from the
    window's start, as soon as PV will fall short; or ``"late"``, only what the
    rest of the window could no longer add at the charge limit, so the top-up
    comes as late as it can and still ends by ``end`` (as measured on the reference
    plant: its top-ups since 2026-09-17 started 13:50-14:50 and took only what PV had
    not delivered). Either way the plant holds the battery while it is short of its
    target in the window.
    """

    start: time
    end: time
    target_soc: float = 1.0
    cover_until: time | None = None
    timing: Literal["early", "late"] = "early"

    def __post_init__(self) -> None:
        if not self.start < self.end:
            raise ValueError("grid charge start must be before its end")
        if not 0 < self.target_soc <= 1:
            raise ValueError("grid charge target_soc must be in (0, 1]")
        if self.timing not in ("early", "late"):
            raise ValueError("grid charge timing must be early or late")
        if self.cover_until is not None and self.start < self.cover_until <= self.end:
            # It would mean the next day, inside the next day's window, which tops up
            # first: a window's cover stops at a window.
            raise ValueError("grid charge cover_until must not fall within its own window")


Coupling = Literal["ac", "dc"]
Connection = Literal["ac", "dc_bus"]
Form = Literal["folded", "split"]
ChargeOrder = Literal["pv_first", "target_first"]
Yields = Literal["pv", "battery"]
Measures = Literal["pv", "grid", "battery", "consumption", "dc_load"]
MeterAt = Literal["dc", "ac"]

_COUPLINGS: Final = ("ac", "dc")
_CONNECTIONS: Final = ("ac", "dc_bus")
_FORMS: Final = ("folded", "split")
_ORDERS: Final = ("pv_first", "target_first")
_YIELDS: Final = ("pv", "battery")
_MEASURES: Final = ("pv", "grid", "battery", "consumption", "dc_load")
_METER_AT: Final = ("dc", "ac")
_TIMINGS: Final = ("early", "late")

#: :class:`Battery`'s fields, named here so the schema changes only on purpose (a
#: test holds this to ``Battery``'s fields).
BATTERY_FIELDS: Final[tuple[str, ...]] = (
    "capacity_kwh",
    "charge_kw",
    "discharge_kw",
    "eta_charge",
    "eta_discharge",
    "reserve_soc",
    "saturation_soc",
    "wear_charge_per_kwh",
    "wear_discharge_per_kwh",
    "loss_charge_per_kw",
    "loss_discharge_per_kw",
    "meter_charge",
    "meter_discharge",
)
_BATTERY_REQUIRED: Final = ("capacity_kwh", "charge_kw", "discharge_kw")


class PlantConfigError(ValueError):
    """A configuration the plant model cannot take; ``errors`` names each problem
    with its path."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__(
            "plant configuration is invalid:\n" + "\n".join(f"  {e}" for e in self.errors)
        )


@dataclass(frozen=True, slots=True)
class PvArray:
    """One PV array. Its forecast is in its coupling's terms: AC kW for an AC-coupled
    array, DC kW for a DC-coupled one."""

    name: str
    coupling: Coupling
    #: The MPPT limit, DC kW: it caps the array's DC power. None: no limit binds.
    dc_rating_kw: float | None = None
    #: An AC-coupled array's own inverter's rating, AC kW. None: no limit binds.
    ac_rating_kw: float | None = None


@dataclass(frozen=True, slots=True)
class Conversion:
    """A converter between a DC bus and the AC bus: the hybrid inverter's, where no
    folded battery holds it. The same law as :class:`Battery`'s conversion:
    low-power efficiencies each way, and AC kW lost per (DC kW)^2 each way."""

    eta_in: float
    eta_out: float
    loss_in_per_kw: float = 0.0
    loss_out_per_kw: float = 0.0


@dataclass(frozen=True, slots=True)
class Inverter:
    """An inverter: a hybrid with a DC bus (:attr:`PlantConfig.hybrid`), or a
    battery's own on the AC bus (:attr:`BatteryPart.inverter`)."""

    #: Its AC rating, on the net power crossing it, kW. None: no limit binds.
    ac_rating_kw: float | None = None
    #: Its own draws, kW: always, and more while the battery works (``microgrid
    #: fit-battery`` fits both). Every
    #: inverter's standby counts; ``working_kw`` is the one the battery converts
    #: through's (``PlantConfig.battery_inverter``), so a hybrid beside a battery on its
    #: own inverter, or with none, adds no working draw.
    standby_kw: float = 0.0
    working_kw: float = 0.0
    #: The battery works above this, kW either way (``WORKING_KW``'s measurement).
    working_threshold_kw: float = WORKING_KW
    #: DC bus to AC: a hybrid's, where its battery is split or absent. None where a
    #: folded battery holds it, and on a battery's own inverter (the battery's
    #: conversion is its inverter's).
    conversion: Conversion | None = None


@dataclass(frozen=True, slots=True)
class BatteryPart:
    """The battery, and where it connects.

    ``battery``'s conversion is from its connection point to stored energy. On the
    AC bus that is its own inverter's path, and ``inverter`` holds that inverter's
    rating and draws. On a hybrid's DC bus it is either the whole path from the AC
    bus with the meter at the DC bus (``folded``: this site's fit, unsplit) or the
    DC bus to stored energy alone (``split``: the hybrid's :class:`Conversion`
    holds DC bus to AC)."""

    battery: Battery
    connection: Connection
    form: Form = "folded"
    #: Its own inverter, on the AC bus; None on a DC bus (the hybrid is there).
    inverter: Inverter | None = None
    #: Its own DC limits, kW, on a DC bus. A folded battery with anything else on its
    #: DC bus needs them: ``battery.charge_kw`` and ``discharge_kw`` are AC-side, and
    #: with these given the plant model reads these in their place (the hybrid's
    #: rating bounds the AC side). Without them a folded battery's AC-side limits hold,
    #: at the DC bus through its curve. A split battery's ``charge_kw`` and
    #: ``discharge_kw`` are at the DC bus already, and the lower of each pair binds.
    dc_charge_kw: float | None = None
    dc_discharge_kw: float | None = None


@dataclass(frozen=True, slots=True)
class GridLimits:
    """The grid connection's limits, kW. None: no limit binds. An export limit of 0
    means the site may not export."""

    import_limit_kw: float | None = None
    export_limit_kw: float | None = None


@dataclass(frozen=True, slots=True)
class Load:
    """A load, by where it connects. The load forecast owns its kW (R-9)."""

    name: str
    connection: Connection = "ac"


@dataclass(frozen=True, slots=True)
class ExportWindow:
    """The plant discharging the battery to the grid, in local time, within a day."""

    start: time
    end: time
    #: Held through the window, AC kW.
    power_kw: float
    #: The window stops at this SOC, at or above the reserve, until it ends: the
    #: battery does not run up and export again in the same window.
    floor_soc: float
    #: Which yields at the export limit.
    yields: Yields = "pv"


@dataclass(frozen=True, slots=True)
class HoldRule:
    """The plant holding its battery through a DC-bus load's session: while the DC-bus
    loads draw more than ``dc_load_above_kw``, a session that starts with the SOC below
    ``start_below_soc`` keeps the battery from discharging, to any load or the grid,
    until it ends. A session that starts at or above it drains the battery as the
    policy does. Measured on the reference plant's meters: it held the battery through
    13 of 14 of its DC-bus load's sessions this way at about 65 % to 2026-09-17, and
    through all nine since, started at 32-85 %."""

    #: A session is the DC-bus loads drawing more than this, DC kW.
    dc_load_above_kw: float
    #: The SOC (0-1) a session must start below to hold the battery.
    start_below_soc: float


@dataclass(frozen=True, slots=True)
class Dispatch:
    """What the plant does on its own, beyond self-consumption."""

    grid_charge: tuple[GridCharge, ...] = ()
    export_windows: tuple[ExportWindow, ...] = ()
    #: PV before the grid (this site), or the grid as a window's target requires
    #: regardless of spare PV.
    charge_order: ChargeOrder = "pv_first"
    #: Rules under which the plant holds the battery.
    hold_rules: tuple[HoldRule, ...] = ()


@dataclass(frozen=True, slots=True)
class Meter:
    """A meter the plant reads: its kW is keyed by ``name`` in a ``MeterReading``.

    - ``pv``: an array's output (``array``), or every array's together (``array``
      None; only where they share one coupling), in the array's terms: AC after an AC
      array's own inverters, DC at a DC array's.
    - ``grid``: the site's meter, AC.
    - ``battery``: at its DC side (``at`` ``"dc"``, the default), or its own
      inverter's AC side (``"ac"``, a battery on the AC bus only). For a battery on a
      DC bus, folded or split, the DC side is the bus, as this site's meter reads; for
      one on its own inverter, its DC terminals, converted by ``Battery``'s law.
    - ``consumption``: the AC loads and the inverters' draw, metered in the grid's
      place: one of the two closes the balance, which gives the other.
    - ``dc_load``: the DC-bus loads' draw, DC at the loads.

    ``sign`` is the meter's convention: 1 where positive is PV made, importing,
    charging or drawing; -1 where it is the opposite."""

    name: str
    measures: Measures
    array: str | None = None
    at: MeterAt | None = None
    sign: float = 1.0


@dataclass(frozen=True, slots=True)
class PlantConfig:
    """A plant: its topology and parameters."""

    tz: ZoneInfo
    pv: tuple[PvArray, ...] = ()
    battery: BatteryPart | None = None
    #: The hybrid inverter, where the plant has a DC bus.
    hybrid: Inverter | None = None
    grid: GridLimits = field(default_factory=GridLimits)
    #: At least one load on the AC bus: the house.
    loads: tuple[Load, ...] = (Load("house"),)
    dispatch: Dispatch = field(default_factory=Dispatch)
    #: The meters the plant reads; None: today's set (:attr:`meter_set`).
    meters: tuple[Meter, ...] | None = None

    def __post_init__(self) -> None:
        if problems := _part_problems(self) or _cross_problems(self):
            raise PlantConfigError(problems)

    @property
    def meter_set(self) -> tuple[Meter, ...]:
        """The meters the plant reads: as configured, or by default the set this
        site has: ``pv`` for every array together where there is PV (where the arrays
        have both couplings, one per array, named ``pv:<array>``, which no other default
        name can be), ``grid``, ``battery`` at its DC side where there is a battery, and
        ``dc_load`` where a load is on the DC bus."""
        if self.meters is not None:
            return self.meters
        meters = []
        if len({array.coupling for array in self.pv}) == 1:
            meters.append(Meter("pv", "pv"))
        else:  # both couplings: no one unit holds both, so each array's, as pv:<array>
            meters += [Meter(f"pv:{array.name}", "pv", array=array.name) for array in self.pv]
        meters.append(Meter("grid", "grid"))
        if self.battery is not None:
            meters.append(Meter("battery", "battery"))
        if any(load.connection == "dc_bus" for load in self.loads):
            meters.append(Meter("dc_load", "dc_load"))
        return tuple(meters)

    @property
    def battery_inverter(self) -> Inverter | None:
        """The inverter the battery converts through, whose draws its work adds: its own
        on the AC bus, or the hybrid on its DC bus. None without a battery."""
        part = self.battery
        if part is None:
            return None
        return part.inverter if part.connection == "ac" else self.hybrid

    @classmethod
    def from_mapping(cls, data: object, *, tz: ZoneInfo) -> PlantConfig:
        """The configuration in ``data`` (the ``plant/2`` schema), in ``tz``, whose key
        must be ``data["timezone"]``. Every field's and part's problem is named at
        once; the rules across parts follow once the parts are sound."""
        return _Reader().plant(data, tz)

    def to_mapping(self) -> dict[str, object]:
        """Plain data that :meth:`from_mapping` reads back to an equal configuration.
        A field that is None is left out."""
        out: dict[str, object] = {"schema": PLANT_SCHEMA, "timezone": self.tz.key}
        out["pv"] = [_plain(array) for array in self.pv]
        if (part := self.battery) is not None:
            battery = {name: getattr(part.battery, name) for name in BATTERY_FIELDS}
            out["battery"] = _drop_none(
                {
                    **battery,
                    "connection": part.connection,
                    "form": part.form,
                    "inverter": None if part.inverter is None else _inverter(part.inverter),
                    "dc_charge_kw": part.dc_charge_kw,
                    "dc_discharge_kw": part.dc_discharge_kw,
                }
            )
        if self.hybrid is not None:
            out["hybrid"] = _inverter(self.hybrid)
        out["grid"] = _plain(self.grid)
        out["loads"] = [_plain(load) for load in self.loads]
        out["dispatch"] = {
            "grid_charge": [_plain(window) for window in self.dispatch.grid_charge],
            "export_windows": [_plain(window) for window in self.dispatch.export_windows],
            "charge_order": self.dispatch.charge_order,
            "hold_rules": [_plain(rule) for rule in self.dispatch.hold_rules],
        }
        if self.meters is not None:
            out["meters"] = [_plain(meter) for meter in self.meters]
        return out


def _values(part: object) -> dict[str, object]:
    """A part's own fields, by name (one level, the objects as they are)."""
    return {f.name: getattr(part, f.name) for f in fields(cast("type", type(part)))}


def _plain(part: object) -> dict[str, object]:
    """A flat part as plain data: times as ISO strings, None left out."""
    return _drop_none(
        {
            name: value.isoformat() if isinstance(value, time) else value
            for name, value in _values(part).items()
        }
    )


def _inverter(inverter: Inverter) -> dict[str, object]:
    out = _values(inverter)
    out["conversion"] = None if inverter.conversion is None else _plain(inverter.conversion)
    return _drop_none(out)


def _drop_none(values: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


# Each part's own rules ----------------------------------------------------------------
#
# Each takes the part's fields by name and checks those present, so the reader can
# check every field that read soundly beside those that did not.

type _Fields = Mapping[str, object]


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _finite(value: object) -> float | None:
    """``value`` as a finite float; None if it is not one (a bool is not a number,
    and an int too large for a float is not finite)."""
    if not _is_number(value):
        return None
    try:
        number = float(cast("float", value))
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _positive_or_none(path: str, value: object) -> list[str]:
    if value is None:
        return []
    number = _finite(value)
    if number is None:
        return [f"{path}: must be a finite number"]
    return [f"{path}: must be positive"] if number <= 0 else []


def _not_negative(path: str, value: object) -> list[str]:
    number = _finite(value)
    if number is None:
        return [f"{path}: must be a finite number"]
    return [f"{path}: must not be negative"] if number < 0 else []


def _fraction(path: str, value: object, *, low: float, high: float) -> list[str]:
    """``low < value <= high``."""
    number = _finite(value)
    if number is None or not low < number <= high:
        return [f"{path}: must be in ({low:g}, {high:g}]"]
    return []


def _name(path: str, value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return [f"{path}: must be a name, not empty or padded with spaces"]
    return []


def _choice(path: str, value: object, choices: Sequence[str]) -> list[str]:
    if value not in choices:
        return [f"{path}: must be one of " + ", ".join(map(repr, choices))]
    return []


def _local(path: str, value: object) -> list[str]:
    if not isinstance(value, time):
        return [f"{path}: must be a local time such as 12:00:00"]
    if value.tzinfo is not None:
        return [f"{path}: must be a local time without an offset (the plant's zone applies)"]
    return []


def _within_a_day(path: str, v: _Fields) -> list[str]:
    """Start before end, where both are sound local times (plant/2's known limit)."""
    start, end = v.get("start"), v.get("end")
    if _local("", start) or _local("", end):
        return []
    if not cast("time", start) < cast("time", end):
        return [f"{path}: start must be before end (a window lies within one local day)"]
    return []


def _array_problems(path: str, v: _Fields) -> list[str]:
    errors = []
    if "name" in v:
        errors += _name(f"{path}.name", v["name"])
    if "coupling" in v:
        errors += _choice(f"{path}.coupling", v["coupling"], _COUPLINGS)
    for key in ("dc_rating_kw", "ac_rating_kw"):
        if key in v:
            errors += _positive_or_none(f"{path}.{key}", v[key])
    if v.get("coupling") == "dc" and v.get("ac_rating_kw") is not None:
        errors.append(f"{path}.ac_rating_kw: a DC-coupled array has no inverter of its own")
    return errors


def _battery_problems(path: str, v: _Fields) -> list[str]:
    """:class:`Battery`'s own rules, each named by its field (a test holds the two
    to agree)."""
    errors = []
    for key in ("capacity_kwh", "charge_kw", "discharge_kw"):
        if key in v:
            errors += _positive_or_none(f"{path}.{key}", v[key])
    for key in ("meter_charge", "meter_discharge"):
        if key in v:
            errors += _fraction(f"{path}.{key}", v[key], low=0.5, high=1.5)
    for key in (
        "wear_charge_per_kwh",
        "wear_discharge_per_kwh",
        "loss_charge_per_kw",
        "loss_discharge_per_kw",
    ):
        if key in v:
            errors += _not_negative(f"{path}.{key}", v[key])
    for key in ("eta_charge", "eta_discharge"):
        if key in v:
            errors += _fraction(f"{path}.{key}", v[key], low=0.5, high=1.0)
    for key in ("reserve_soc", "saturation_soc"):
        if key in v and _finite(v[key]) is None:
            errors.append(f"{path}.{key}: must be finite")
    # The two rules across Battery's fields, each judged as soon as its own fields
    # are sound, whatever the rest.
    reserve, saturation = _finite(v.get("reserve_soc")), _finite(v.get("saturation_soc"))
    if reserve is not None and saturation is not None and not 0 <= reserve < saturation <= 1:
        errors.append(f"{path}.reserve_soc, saturation_soc: need 0 <= reserve < saturation <= 1")
    side = ("discharge_kw", "eta_discharge", "meter_discharge", "loss_discharge_per_kw")
    if all(key in v for key in side) and not any(
        e.startswith(tuple(f"{path}.{key}" for key in side)) for e in errors
    ):
        try:
            # Battery's own arithmetic, on this side's fields and sound defaults.
            Battery(1.0, 1.0, **{key: cast("float", v[key]) for key in side})
        except ValueError, ArithmeticError:
            errors.append(
                f"{path}.loss_discharge_per_kw: cannot deliver discharge_kw with that loss growth"
            )
    return errors


def _inverter_problems(path: str, v: _Fields) -> list[str]:
    errors = []
    if "ac_rating_kw" in v:
        errors += _positive_or_none(f"{path}.ac_rating_kw", v["ac_rating_kw"])
    for key in ("standby_kw", "working_kw", "working_threshold_kw"):
        if key in v:
            errors += _not_negative(f"{path}.{key}", v[key])
    conversion = v.get("conversion")
    if isinstance(conversion, Conversion):
        errors += _conversion_problems(f"{path}.conversion", _values(conversion))
    return errors


def _conversion_problems(path: str, v: _Fields) -> list[str]:
    errors = []
    for key in ("eta_in", "eta_out"):
        if key in v:
            errors += _fraction(f"{path}.{key}", v[key], low=0.0, high=1.0)
    for key in ("loss_in_per_kw", "loss_out_per_kw"):
        if key in v:
            errors += _not_negative(f"{path}.{key}", v[key])
    return errors


def _part_problems_of_battery(path: str, part: BatteryPart) -> list[str]:
    errors = _choice(f"{path}.connection", part.connection, _CONNECTIONS)
    errors += _choice(f"{path}.form", part.form, _FORMS)
    errors += _positive_or_none(f"{path}.dc_charge_kw", part.dc_charge_kw)
    errors += _positive_or_none(f"{path}.dc_discharge_kw", part.dc_discharge_kw)
    if part.inverter is not None:
        errors += _inverter_problems(f"{path}.inverter", _values(part.inverter))
    return errors


def _grid_problems(path: str, v: _Fields) -> list[str]:
    errors = []
    if "import_limit_kw" in v:
        errors += _positive_or_none(f"{path}.import_limit_kw", v["import_limit_kw"])
    if v.get("export_limit_kw") is not None:
        errors += _not_negative(f"{path}.export_limit_kw", v["export_limit_kw"])
    return errors


def _load_problems(path: str, v: _Fields) -> list[str]:
    errors = []
    if "name" in v:
        errors += _name(f"{path}.name", v["name"])
    if "connection" in v:
        errors += _choice(f"{path}.connection", v["connection"], _CONNECTIONS)
    return errors


def _grid_charge_problems(path: str, v: _Fields) -> list[str]:
    """:class:`GridCharge`'s own rules, by field, and local times."""
    errors = []
    for key in ("start", "end"):
        if key in v:
            errors += _local(f"{path}.{key}", v[key])
    if v.get("cover_until") is not None:
        errors += _local(f"{path}.cover_until", v["cover_until"])
    errors += _within_a_day(path, v)
    if "target_soc" in v:
        errors += _fraction(f"{path}.target_soc", v["target_soc"], low=0.0, high=1.0)
    if "timing" in v:
        errors += _choice(f"{path}.timing", v["timing"], _TIMINGS)
    return errors


def _export_problems(path: str, v: _Fields) -> list[str]:
    errors = []
    for key in ("start", "end"):
        if key in v:
            errors += _local(f"{path}.{key}", v[key])
    errors += _within_a_day(path, v)
    if "power_kw" in v:
        errors += _positive_or_none(f"{path}.power_kw", v["power_kw"])
    if "floor_soc" in v and _finite(v["floor_soc"]) is None:
        errors.append(f"{path}.floor_soc: must be finite")
    if "yields" in v:
        errors += _choice(f"{path}.yields", v["yields"], _YIELDS)
    return errors


def _hold_problems(path: str, v: _Fields) -> list[str]:
    errors = []
    if "dc_load_above_kw" in v:
        errors += _not_negative(f"{path}.dc_load_above_kw", v["dc_load_above_kw"])
    if "start_below_soc" in v:
        errors += _fraction(f"{path}.start_below_soc", v["start_below_soc"], low=0.0, high=1.0)
    return errors


def _meter_problems(path: str, v: _Fields) -> list[str]:
    errors = []
    if "name" in v:
        errors += _name(f"{path}.name", v["name"])
    if "measures" in v:
        errors += _choice(f"{path}.measures", v["measures"], _MEASURES)
    if v.get("array") is not None:
        errors += _name(f"{path}.array", v["array"])
    if v.get("at") is not None:
        errors += _choice(f"{path}.at", v["at"], _METER_AT)
    if "sign" in v and _finite(v["sign"]) not in (1.0, -1.0):
        errors.append(f"{path}.sign: must be 1 or -1")
    return errors


def _part_problems(config: PlantConfig) -> list[str]:
    """Every part's own problems (for a configuration built directly)."""
    errors: list[str] = []
    key: str | None = getattr(config.tz, "key", None)  # None for a zone from a file
    if not isinstance(config.tz, ZoneInfo) or key is None:
        errors.append("tz: must be a ZoneInfo with a key (a fixed offset cannot follow DST)")
    for i, array in enumerate(config.pv):
        errors += _array_problems(f"pv[{i}]", _values(array))
    if config.battery is not None:
        errors += _battery_problems("battery", _values(config.battery.battery))
        errors += _part_problems_of_battery("battery", config.battery)
    if config.hybrid is not None:
        errors += _inverter_problems("hybrid", _values(config.hybrid))
    errors += _grid_problems("grid", _values(config.grid))
    for i, load in enumerate(config.loads):
        errors += _load_problems(f"loads[{i}]", _values(load))
    dispatch = config.dispatch
    errors += _choice("dispatch.charge_order", dispatch.charge_order, _ORDERS)
    for i, window in enumerate(dispatch.grid_charge):
        errors += _grid_charge_problems(f"dispatch.grid_charge[{i}]", _values(window))
    for i, export in enumerate(dispatch.export_windows):
        errors += _export_problems(f"dispatch.export_windows[{i}]", _values(export))
    for i, rule in enumerate(dispatch.hold_rules):
        errors += _hold_problems(f"dispatch.hold_rules[{i}]", _values(rule))
    for i, meter in enumerate(config.meters or ()):
        errors += _meter_problems(f"meters[{i}]", _values(meter))
    return errors


# The rules across parts ---------------------------------------------------------------


def _cross_problems(config: PlantConfig) -> list[str]:  # one list of rules
    """The rules across parts, on parts that are each sound."""
    errors: list[str] = []
    loads = {load.name for load in config.loads}  # the default load included
    arrays: dict[str, int] = {}
    for i, array in enumerate(config.pv):
        if array.name in loads:
            errors.append(f"pv[{i}].name: {array.name!r} is also a load's name")
        elif array.name in arrays:
            errors.append(f"pv[{i}].name: {array.name!r} is used by pv[{arrays[array.name]}] too")
        else:
            arrays[array.name] = i
    seen: set[str] = set()
    for i, load in enumerate(config.loads):
        if load.name in seen:
            errors.append(f"loads[{i}].name: {load.name!r} is used twice")
        seen.add(load.name)

    hybrid, part = config.hybrid, config.battery
    dc_arrays = [i for i, array in enumerate(config.pv) if array.coupling == "dc"]
    errors += [
        f"pv[{i}].coupling: a DC-coupled array needs a hybrid inverter"
        for i in dc_arrays
        if hybrid is None
    ]
    dc_loads = [i for i, load in enumerate(config.loads) if load.connection == "dc_bus"]
    errors += [
        f"loads[{i}].connection: a DC-bus load needs a hybrid inverter"
        for i in dc_loads
        if hybrid is None
    ]
    if not any(load.connection == "ac" for load in config.loads):
        errors.append("loads: at least one load on the AC bus (the house)")

    folded = part is not None and part.connection == "dc_bus" and part.form == "folded"
    if part is not None:
        if part.connection == "ac":
            if part.form == "split":
                errors.append("battery.form: only a battery on a DC bus can be split")
            if part.inverter is None:
                errors.append("battery.inverter: a battery on the AC bus needs its own")
            elif part.inverter.conversion is not None:
                errors.append(
                    "battery.inverter.conversion: a battery's own inverter converts as "
                    "the battery does (its conversion is the battery's)"
                )
            if part.dc_charge_kw is not None or part.dc_discharge_kw is not None:
                errors.append(
                    "battery.dc_charge_kw, dc_discharge_kw: only a battery on a DC bus has them"
                )
        else:
            if hybrid is None:
                errors.append("hybrid: a battery on a DC bus needs a hybrid inverter")
            if part.inverter is not None:
                errors.append("battery.inverter: a battery on a DC bus uses the hybrid")
    if hybrid is not None:
        if folded and hybrid.conversion is not None:
            errors.append("hybrid.conversion: a folded battery holds the hybrid's conversion")
        if not folded and hybrid.conversion is None:
            errors.append("hybrid.conversion: a hybrid without a folded battery needs one")
    if folded and part is not None and (dc_arrays or dc_loads):
        # The folded path's AC-side limits merge two that come apart once anything
        # else is on the DC bus.
        if part.dc_charge_kw is None or part.dc_discharge_kw is None:
            errors.append(
                "battery.dc_charge_kw, dc_discharge_kw: a folded battery with anything else "
                "on its DC bus needs its own DC limits"
            )
        if hybrid is not None and hybrid.ac_rating_kw is None:
            errors.append(
                "hybrid.ac_rating_kw: a folded battery with anything else on its DC bus "
                "needs the hybrid's AC rating"
            )
        if part.battery.meter_charge > part.battery.meter_discharge:
            # Netted, the hybrid's loss cancels and the round trip is the DC bus to
            # stored step's: over 1, the dead band inverts, and a kWh kept from charging
            # would be worth more than one drawn, which no real battery allows.
            errors.append(
                "battery.meter_charge: a folded battery with anything else on its DC bus "
                "must not store more per DC kWh than it draws (meter_charge <= "
                "meter_discharge)"
            )

    grid, dispatch = config.grid, config.dispatch
    spans: list[tuple[str, time, time]] = []
    for i, window in enumerate(dispatch.grid_charge):
        where = f"dispatch.grid_charge[{i}]"
        if part is None:
            errors.append(f"{where}: a grid-charge window needs a battery")
        elif window.target_soc <= part.battery.reserve_soc:
            errors.append(f"{where}.target_soc: must be above the battery's reserve")
        spans.append((where, window.start, window.end))
    for i, export in enumerate(dispatch.export_windows):
        where = f"dispatch.export_windows[{i}]"
        if part is None:
            errors.append(f"{where}: an export window needs a battery")
        else:
            battery = part.battery
            if not battery.reserve_soc <= export.floor_soc < battery.saturation_soc:
                errors.append(
                    f"{where}.floor_soc: must be at or above the reserve, below saturation"
                )
            # discharge_kw is AC-side, except in the split form (DC bus to stored).
            if part.form != "split" and export.power_kw > battery.discharge_kw:
                errors.append(f"{where}.power_kw: above the battery's discharge limit")
            through = part.inverter if part.connection == "ac" else hybrid
            rating = None if through is None else through.ac_rating_kw
            if rating is not None and export.power_kw > rating:
                errors.append(f"{where}.power_kw: above its inverter's AC rating")
        if grid.export_limit_kw is not None:
            if grid.export_limit_kw == 0:
                errors.append(f"{where}: the site may not export (grid.export_limit_kw = 0)")
            elif export.power_kw > grid.export_limit_kw:
                errors.append(f"{where}.power_kw: above the grid's export limit")
        spans.append((where, export.start, export.end))
    for i, rule in enumerate(dispatch.hold_rules):
        where = f"dispatch.hold_rules[{i}]"
        if part is None:
            errors.append(f"{where}: a hold rule needs a battery")
        elif rule.start_below_soc <= part.battery.reserve_soc:
            errors.append(f"{where}.start_below_soc: must be above the battery's reserve")
        if not dc_loads:
            errors.append(f"{where}: a hold rule needs a DC-bus load (its session is one's draw)")
    for a, (first, start_a, end_a) in enumerate(spans):
        for second, start_b, end_b in spans[a + 1 :]:
            if start_a < end_b and start_b < end_a:
                errors.append(f"{second}: overlaps {first}")
    errors += _meter_set_problems(config, set(arrays), bool(dc_loads))
    return errors


def _meter_set_problems(config: PlantConfig, arrays: set[str], dc_loads: bool) -> list[str]:
    """The meters against the plant: each meters something it has, once."""
    errors: list[str] = []
    names: set[str] = set()
    once: dict[str, int] = {}
    metered: dict[str, int] = {}  # array name, or "" for every array, to its meter
    part = config.battery
    for i, meter in enumerate(config.meters or ()):
        where = f"meters[{i}]"
        if meter.name in names:
            errors.append(f"{where}.name: {meter.name!r} is used twice")
        names.add(meter.name)
        if meter.array is not None and meter.measures != "pv":
            errors.append(f"{where}.array: only a PV meter meters an array")
        if meter.at is not None and meter.measures != "battery":
            errors.append(f"{where}.at: only a battery meter has a side")
        if meter.measures == "pv":
            key = meter.array or ""
            if not config.pv:
                errors.append(f"{where}: a PV meter needs PV")
            elif meter.array is not None and meter.array not in arrays:
                errors.append(f"{where}.array: {meter.array!r} is not an array")
            elif meter.array is None and len({a.coupling for a in config.pv}) > 1:
                errors.append(
                    f"{where}: PV of both couplings is metered per array (no one unit holds both)"
                )
            elif key in metered or (key == "" and metered) or ("" in metered):
                errors.append(f"{where}: meters PV another meter meters")
            metered.setdefault(key, i)
            continue
        if meter.measures in once:
            errors.append(f"{where}: a second {meter.measures} meter")
        once.setdefault(meter.measures, i)
        if meter.measures == "battery":
            if part is None:
                errors.append(f"{where}: a battery meter needs a battery")
            elif meter.at == "ac" and part.connection != "ac":
                errors.append(f"{where}.at: a battery on a DC bus is metered at its DC side")
        if meter.measures == "dc_load" and not dc_loads:
            errors.append(f"{where}: a DC-bus load meter needs a DC-bus load")
    if config.meters is not None:
        # What the split needs: every source and sink, and the grid or the consumption:
        # either closes the balance, which gives the other, so reading both would give
        # one quantity two values.
        if "grid" not in once and "consumption" not in once:
            errors.append("meters: the grid or the consumption must be metered")
        if "grid" in once and "consumption" in once:
            errors.append(
                "meters: the grid or the consumption, not both (the balance gives the other)"
            )
        if part is not None and "battery" not in once:
            errors.append("meters: the battery must be metered")
        if dc_loads and "dc_load" not in once:
            errors.append("meters: the DC-bus loads must be metered")
        if config.pv and "" not in metered and not arrays <= set(metered):
            errors.append("meters: every array's PV must be metered, each or together")
    return errors


# Reading plain data -------------------------------------------------------------------

_NUMBER, _TEXT, _CLOCK = "number", "text", "clock"


class _Reader:
    """Reads the ``plant/2`` mapping. Each part: read its fields by kind, name the
    required ones missing, check what read soundly by the part's own rules, and build
    the part only if it raised no problem. Every problem is named with its path."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def fail(self, path: str, message: str) -> None:
        self.errors.append(f"{path or '(top level)'}: {message}")

    def table(self, value: object, path: str, keys: Sequence[str]) -> Mapping[str, object] | None:
        """``value`` as a table of ``keys``, its null values left out; None (with the
        error) if it is not a table."""
        if not isinstance(value, Mapping):
            self.fail(path, "must be a table")
            return None
        out: dict[str, object] = {}
        for key, item in value.items():
            if key not in keys:
                self.fail(f"{path}.{key}" if path else str(key), "is not a field")
            elif item is not None:
                out[cast("str", key)] = item
        return out

    def items(self, value: object, path: str) -> list[object]:
        if value is None:
            return []
        if isinstance(value, str | bytes) or not isinstance(value, Sequence):
            self.fail(path, "must be a list")
            return []
        return list(value)

    def fields(
        self,
        data: Mapping[str, object],
        path: str,
        kinds: Mapping[str, str],
        required: Sequence[str] = (),
    ) -> dict[str, object]:
        """The fields of ``data`` that read soundly as their kinds; the rest, and the
        required ones missing, are named."""
        out: dict[str, object] = {}
        for key, kind in kinds.items():
            if key not in data:
                if key in required:
                    self.fail(f"{path}.{key}", "is required")
                continue
            value = data[key]
            if kind == _NUMBER:
                if (number := _finite(value)) is None:
                    self.fail(f"{path}.{key}", "must be a finite number")
                else:
                    out[key] = number
            elif kind == _TEXT:
                if isinstance(value, str):
                    out[key] = value
                else:
                    self.fail(f"{path}.{key}", "must be a string")
            elif isinstance(value, time):
                out[key] = value
            else:
                try:
                    out[key] = time.fromisoformat(cast("str", value))
                except TypeError, ValueError:
                    self.fail(f"{path}.{key}", "must be a local time such as 12:00:00")
        return out

    def checked(self, before: int, problems: list[str]) -> bool:
        """Record ``problems``; whether the part raised none since ``before``."""
        self.errors += problems
        return len(self.errors) == before

    def plant(self, data: object, tz: ZoneInfo) -> PlantConfig:
        keys = (
            "schema",
            "timezone",
            "pv",
            "battery",
            "hybrid",
            "grid",
            "loads",
            "dispatch",
            "meters",
        )
        top = self.table(data, "", keys)
        if top is None:
            raise PlantConfigError(self.errors)
        if top.get("schema") == "plant/1":
            self.fail(
                "schema",
                f"must be {PLANT_SCHEMA!r}: plant/1's wear_charge_usd_per_kwh and "
                "wear_discharge_usd_per_kwh are wear_charge_per_kwh and wear_discharge_per_kwh",
            )
        elif top.get("schema") != PLANT_SCHEMA:
            self.fail("schema", f"must be {PLANT_SCHEMA!r}")
        zone = top.get("timezone")
        if zone is None:
            self.fail("timezone", "is required: the zone's name")
        elif not isinstance(zone, str):
            self.fail("timezone", "must be a string: the zone's name")
        elif getattr(tz, "key", None) != zone:
            self.fail("timezone", f"{zone!r} is not the given zone's key")
        pv = [
            self.array(item, f"pv[{i}]") for i, item in enumerate(self.items(top.get("pv"), "pv"))
        ]
        battery = self.battery(top["battery"], "battery") if "battery" in top else None
        hybrid = self.inverter(top["hybrid"], "hybrid") if "hybrid" in top else None
        grid = self.grid(top.get("grid", {}), "grid")
        loads = (
            [
                self.load(item, f"loads[{i}]")
                for i, item in enumerate(self.items(top["loads"], "loads"))
            ]
            if "loads" in top
            else [Load("house")]
        )
        dispatch = self.dispatch(top.get("dispatch", {}), "dispatch")
        meters = (
            [
                self.meter(item, f"meters[{i}]")
                for i, item in enumerate(self.items(top["meters"], "meters"))
            ]
            if "meters" in top
            else None
        )
        if self.errors:
            raise PlantConfigError(self.errors)
        return PlantConfig(
            tz=tz,
            pv=tuple(array for array in pv if array is not None),
            battery=battery,
            hybrid=hybrid,
            grid=grid or GridLimits(),
            loads=tuple(load for load in loads if load is not None),
            dispatch=dispatch or Dispatch(),
            meters=None if meters is None else tuple(m for m in meters if m is not None),
        )

    def meter(self, value: object, path: str) -> Meter | None:
        kinds = {"name": _TEXT, "measures": _TEXT, "array": _TEXT, "at": _TEXT, "sign": _NUMBER}
        before = len(self.errors)
        if (data := self.table(value, path, tuple(kinds))) is None:
            return None
        v = self.fields(data, path, kinds, required=("name", "measures"))
        if not self.checked(before, _meter_problems(path, v)):
            return None
        return Meter(
            name=cast("str", v["name"]),
            measures=cast("Measures", v["measures"]),
            array=cast("str | None", v.get("array")),
            at=cast("MeterAt | None", v.get("at")),
            sign=cast("float", v.get("sign", 1.0)),
        )

    def array(self, value: object, path: str) -> PvArray | None:
        kinds = {"name": _TEXT, "coupling": _TEXT, "dc_rating_kw": _NUMBER, "ac_rating_kw": _NUMBER}
        before = len(self.errors)
        if (data := self.table(value, path, tuple(kinds))) is None:
            return None
        v = self.fields(data, path, kinds, required=("name", "coupling"))
        if not self.checked(before, _array_problems(path, v)):
            return None
        return PvArray(
            name=cast("str", v["name"]),
            coupling=cast("Coupling", v["coupling"]),
            dc_rating_kw=cast("float | None", v.get("dc_rating_kw")),
            ac_rating_kw=cast("float | None", v.get("ac_rating_kw")),
        )

    def battery(self, value: object, path: str) -> BatteryPart | None:
        kinds = dict.fromkeys(BATTERY_FIELDS, _NUMBER)
        kinds |= {
            "connection": _TEXT,
            "form": _TEXT,
            "dc_charge_kw": _NUMBER,
            "dc_discharge_kw": _NUMBER,
        }
        before = len(self.errors)
        if (data := self.table(value, path, (*kinds, "inverter"))) is None:
            return None
        v = self.fields(data, path, kinds, required=(*_BATTERY_REQUIRED, "connection"))
        # What read soundly, and the defaults of those not given (a field given but
        # unsound stays out, so no rule judges it twice).
        battery: dict[str, object] = {key: v[key] for key in BATTERY_FIELDS if key in v}
        for f in fields(Battery):
            if f.name not in data and f.name not in _BATTERY_REQUIRED:
                battery[f.name] = f.default
        problems = _battery_problems(path, battery)
        problems += (
            _choice(f"{path}.connection", v["connection"], _CONNECTIONS)
            if ("connection" in v)
            else []
        )
        problems += _choice(f"{path}.form", v["form"], _FORMS) if "form" in v else []
        for key in ("dc_charge_kw", "dc_discharge_kw"):
            problems += _positive_or_none(f"{path}.{key}", v.get(key))
        inverter = (
            self.inverter(data["inverter"], f"{path}.inverter") if "inverter" in data else None
        )
        if not self.checked(before, problems):
            return None
        try:
            built = Battery(**cast("dict[str, float]", battery))
        except (ValueError, ArithmeticError) as err:  # the rules above should have said
            self.fail(path, str(err))
            return None
        return BatteryPart(
            battery=built,
            connection=cast("Connection", v["connection"]),
            form=cast("Form", v.get("form", "folded")),
            inverter=inverter,
            dc_charge_kw=cast("float | None", v.get("dc_charge_kw")),
            dc_discharge_kw=cast("float | None", v.get("dc_discharge_kw")),
        )

    def inverter(self, value: object, path: str) -> Inverter | None:
        kinds = dict.fromkeys(
            ("ac_rating_kw", "standby_kw", "working_kw", "working_threshold_kw"), _NUMBER
        )
        before = len(self.errors)
        if (data := self.table(value, path, (*kinds, "conversion"))) is None:
            return None
        v = self.fields(data, path, kinds)
        conversion = (
            self.conversion(data["conversion"], f"{path}.conversion")
            if "conversion" in data
            else None
        )
        if not self.checked(before, _inverter_problems(path, v)):
            return None
        return Inverter(
            ac_rating_kw=cast("float | None", v.get("ac_rating_kw")),
            standby_kw=cast("float", v.get("standby_kw", 0.0)),
            working_kw=cast("float", v.get("working_kw", 0.0)),
            working_threshold_kw=cast("float", v.get("working_threshold_kw", WORKING_KW)),
            conversion=conversion,
        )

    def conversion(self, value: object, path: str) -> Conversion | None:
        kinds = dict.fromkeys(("eta_in", "eta_out", "loss_in_per_kw", "loss_out_per_kw"), _NUMBER)
        before = len(self.errors)
        if (data := self.table(value, path, tuple(kinds))) is None:
            return None
        v = self.fields(data, path, kinds, required=("eta_in", "eta_out"))
        if not self.checked(before, _conversion_problems(path, v)):
            return None
        return Conversion(**cast("dict[str, float]", v))

    def grid(self, value: object, path: str) -> GridLimits | None:
        kinds = dict.fromkeys(("import_limit_kw", "export_limit_kw"), _NUMBER)
        before = len(self.errors)
        if (data := self.table(value, path, tuple(kinds))) is None:
            return None
        v = self.fields(data, path, kinds)
        if not self.checked(before, _grid_problems(path, v)):
            return None
        return GridLimits(**cast("dict[str, float]", v))

    def load(self, value: object, path: str) -> Load | None:
        kinds = {"name": _TEXT, "connection": _TEXT}
        before = len(self.errors)
        if (data := self.table(value, path, tuple(kinds))) is None:
            return None
        v = self.fields(data, path, kinds, required=("name",))
        if not self.checked(before, _load_problems(path, v)):
            return None
        return Load(
            name=cast("str", v["name"]), connection=cast("Connection", v.get("connection", "ac"))
        )

    def dispatch(self, value: object, path: str) -> Dispatch | None:
        before = len(self.errors)
        data = self.table(
            value, path, ("grid_charge", "export_windows", "charge_order", "hold_rules")
        )
        if data is None:
            return None
        windows = [
            self.window(item, f"{path}.grid_charge[{i}]")
            for i, item in enumerate(self.items(data.get("grid_charge"), f"{path}.grid_charge"))
        ]
        exports = [
            self.export(item, f"{path}.export_windows[{i}]")
            for i, item in enumerate(
                self.items(data.get("export_windows"), f"{path}.export_windows")
            )
        ]
        holds = [
            self.hold(item, f"{path}.hold_rules[{i}]")
            for i, item in enumerate(self.items(data.get("hold_rules"), f"{path}.hold_rules"))
        ]
        v = self.fields(data, path, {"charge_order": _TEXT})
        problems = (
            _choice(f"{path}.charge_order", v["charge_order"], _ORDERS)
            if "charge_order" in v
            else []
        )
        if not self.checked(before, problems):
            return None
        return Dispatch(
            grid_charge=tuple(window for window in windows if window is not None),
            export_windows=tuple(export for export in exports if export is not None),
            charge_order=cast("ChargeOrder", v.get("charge_order", "pv_first")),
            hold_rules=tuple(rule for rule in holds if rule is not None),
        )

    def hold(self, value: object, path: str) -> HoldRule | None:
        kinds = {"dc_load_above_kw": _NUMBER, "start_below_soc": _NUMBER}
        before = len(self.errors)
        if (data := self.table(value, path, tuple(kinds))) is None:
            return None
        v = self.fields(data, path, kinds, required=tuple(kinds))
        if not self.checked(before, _hold_problems(path, v)):
            return None
        return HoldRule(
            dc_load_above_kw=cast("float", v["dc_load_above_kw"]),
            start_below_soc=cast("float", v["start_below_soc"]),
        )

    def window(self, value: object, path: str) -> GridCharge | None:
        kinds = {
            "start": _CLOCK,
            "end": _CLOCK,
            "target_soc": _NUMBER,
            "cover_until": _CLOCK,
            "timing": _TEXT,
        }
        before = len(self.errors)
        if (data := self.table(value, path, tuple(kinds))) is None:
            return None
        v = self.fields(data, path, kinds, required=("start", "end"))
        if not self.checked(before, _grid_charge_problems(path, v)):
            return None
        return GridCharge(
            start=cast("time", v["start"]),
            end=cast("time", v["end"]),
            target_soc=cast("float", v.get("target_soc", 1.0)),
            cover_until=cast("time | None", v.get("cover_until")),
            timing=cast("Literal['early', 'late']", v.get("timing", "early")),
        )

    def export(self, value: object, path: str) -> ExportWindow | None:
        kinds = {
            "start": _CLOCK,
            "end": _CLOCK,
            "power_kw": _NUMBER,
            "floor_soc": _NUMBER,
            "yields": _TEXT,
        }
        before = len(self.errors)
        if (data := self.table(value, path, tuple(kinds))) is None:
            return None
        v = self.fields(data, path, kinds, required=("start", "end", "power_kw", "floor_soc"))
        if not self.checked(before, _export_problems(path, v)):
            return None
        return ExportWindow(
            start=cast("time", v["start"]),
            end=cast("time", v["end"]),
            power_kw=cast("float", v["power_kw"]),
            floor_soc=cast("float", v["floor_soc"]),
            yields=cast("Yields", v.get("yields", "pv")),
        )
