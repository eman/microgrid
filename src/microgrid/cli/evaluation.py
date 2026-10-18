"""Measure the energy context against what happened.

The questions: how far the forecast surplus is from
the surplus that followed, by how far ahead it was forecast; how often the
pessimistic surplus was in fact met (it should be about 90 %, tariffkit#67
R-13); how the live value compares with the ten minutes that followed it;
and whether consumers were ever left without a valid issue.

**Realised surplus** is PV the site sent out: grid export beyond the battery's
discharge (:func:`pv_export_kw`), as a valuation's surplus is; the plant's battery export
is priced, never surplus. Curtailment
would count too, but no export limit binds at the reference site (its measured export
has gone well above the array's peak without flattening) and nothing reports it, so it
is not added.

Only ``stored_value_v1`` issues are forecasts; a ``fallback_import`` issue
says surplus 0 by rule (a fallback has none) and is counted, not
scored.

**What a kWh realised** is the library's: ``microgrid.engine.analysis.scoring``
(``realise``, ``observed_flows``, ``observed_regime``). The scores here hold the issues'
claims to it.

Pure: ``microgrid replay`` prints these over a dataset.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from microgrid.engine import Regime
from microgrid.engine.analysis.scoring import (
    BUCKETS,
    bucket_label,
    hours_ahead_bucket,
)
from microgrid.engine.series import require_utc
from microgrid.plant import Battery

__all__ = [
    "SPILL_SIDE",
    "Calibration",
    "Errors",
    "IssueSlot",
    "PriceErrors",
    "RegimeAgreement",
    "calibration",
    "complete_issues",
    "lapses",
    "live_errors",
    "price_errors",
    "pv_export_kw",
    "regime_agreement",
    "surplus_errors",
]

#: A slot is on the wrong side when the published ``certainty_spill`` is at least
#: this and the kWh was not paid for with spilled PV, or below it and it was. A
#: price gap no longer tells the sides apart: through the battery's losses and wear a
#: fill-backed kWh reaches ~0.25 per kWh on an evening export, while PV diverted from a
#: battery that runs empty costs less than the ~0.37 import.
SPILL_SIDE: Final = 0.5


def pv_export_kw(grid_kw: float, battery_kw: float, *, battery: Battery) -> float:
    """PV the site sent out: grid export beyond the battery's AC output, kW.

    Sign convention of the plant's sensors: grid power > 0 is importing, battery
    power > 0 is charging. The battery's meter is DC side (its readings fit the
    SOC's change, measured on the reference plant), so its discharge is converted to
    AC (``Battery.discharge_ac``).
    """
    delivered = max(0.0, battery.discharge_ac(max(0.0, -battery_kw)))
    return max(0.0, max(0.0, -grid_kw) - delivered)


@dataclass(frozen=True, slots=True)
class IssueSlot:
    """One slot of one issue: what it said about ``start``, issued at ``issued_at``."""

    issued_at: datetime
    start: datetime
    surplus_kw: float
    surplus_kw_p10: float
    marginal_cost: float | None = None
    regime: Regime | None = None
    certainty_spill: float | None = None
    certainty_covered: float | None = None

    def __post_init__(self) -> None:
        require_utc(self.issued_at, self.start)


def complete_issues(expected: Mapping[str, int], counted: Mapping[str, int]) -> set[str]:
    """The issues (by ``issued_at`` tag) whose slot rows number ``n_points``.

    A recorder that writes an issue row and then its slot rows is read by this rule: an
    issue counts only if its issue row exists
    (``expected``, tag to ``n_points``) and its slot rows (``counted``) number
    exactly that; fewer is a write that failed part way, more a tag reused.
    """
    return {tag for tag, n in expected.items() if counted.get(tag, 0) == n}


@dataclass(frozen=True, slots=True)
class Errors:
    """Forecast against realised over a set of slots (kW)."""

    label: str
    count: int
    #: Mean of forecast minus realised: positive means surplus was over-forecast.
    bias_kw: float
    mae_kw: float
    #: Slots with a pessimistic surplus above zero: the ones that test it.
    p10_count: int
    #: Share of those whose realised surplus met the pessimistic surplus.
    p10_met: float


def _errors(label: str, pairs: Sequence[tuple[float, float, float]]) -> Errors:
    """``pairs`` are (forecast, pessimistic, realised).

    The pessimistic surplus is scored only where it says something (above
    zero): at night it is 0 and so is the export, which would count as met and
    swamp the rate. Realised must reach it exactly: kW are
    published to 0.1 W (contract), so no tolerance is owed.
    """
    if not pairs:
        return Errors(label, 0, 0.0, 0.0, 0, 0.0)
    count = len(pairs)
    tested = [(p, r) for _, p, r in pairs if p > 0.0]
    return Errors(
        label=label,
        count=count,
        bias_kw=sum(f - r for f, _, r in pairs) / count,
        mae_kw=sum(abs(f - r) for f, _, r in pairs) / count,
        p10_count=len(tested),
        p10_met=sum(r >= p for p, r in tested) / len(tested) if tested else 0.0,
    )


def surplus_errors(
    slots: Iterable[IssueSlot], realised_kw: Mapping[datetime, float]
) -> list[Errors]:
    """Forecast surplus against realised surplus, per horizon bucket.

    A slot counts when its realised surplus is known and it lies within a
    bucket of its issue's time. Buckets are measured from ``issued_at``, not
    from the slot index: an issue made mid-slot has its slot already under way
    at 0 h ahead, and its ``0-1h`` bucket takes the slot that starts 57
    minutes later too.
    """
    by_bucket: dict[str, list[tuple[float, float, float]]] = {
        bucket_label(low, high): [] for low, high in BUCKETS
    }
    for slot in slots:
        actual = realised_kw.get(slot.start)
        if actual is None:
            continue
        label = hours_ahead_bucket(max(slot.start - slot.issued_at, timedelta(0)))
        if label is not None:
            by_bucket[label].append((slot.surplus_kw, slot.surplus_kw_p10, actual))
    return [_errors(label, pairs) for label, pairs in by_bucket.items()]


def live_errors(
    live_kw: Iterable[tuple[datetime, float]], realised_kw: Mapping[datetime, float]
) -> Errors:
    """The live surplus against the realised surplus keyed by the same times.

    The caller supplies, per live value, the realised surplus over the ten
    minutes that followed it; a live value without one is left out. The
    live value carries no pessimistic case, so ``p10_count`` is 0.
    """
    pairs = []
    for at, kw in live_kw:
        require_utc(at)
        if at in realised_kw:
            pairs.append((kw, 0.0, realised_kw[at]))
    return _errors("live", pairs)


def lapses(
    issues: Sequence[tuple[datetime, datetime]], *, until: datetime | None = None
) -> list[tuple[datetime, datetime]]:
    """Gaps in which no issue was valid: ``(from, to)`` per gap.

    ``issues`` are ``(issued_at, valid_until)`` pairs in any order; validity
    is half-open, so an issue made exactly when the last one expires leaves
    no gap. The gap before the first issue is not one; with ``until``, a last
    issue that expired before it is. This is what reached the recorder: the persist
    queue can drop a write, so a record of what consumers saw (a health topic's history,
    say) is the authoritative one.
    """
    for issued_at, valid_until in issues:
        require_utc(issued_at, valid_until)
    ordered = sorted(issues)
    gaps = []
    covered_until: datetime | None = None
    for issued_at, valid_until in ordered:
        if covered_until is not None and issued_at > covered_until:
            gaps.append((covered_until, issued_at))
        covered_until = valid_until if covered_until is None else max(covered_until, valid_until)
    if until is not None and covered_until is not None and until > covered_until:
        gaps.append((covered_until, until))
    return gaps


@dataclass(frozen=True, slots=True)
class PriceErrors:
    """Published ``marginal_cost`` against the realised price (per kWh)."""

    label: str
    count: int
    #: Mean of published minus realised: positive means the kWh was priced dearer.
    bias: float
    mae: float
    #: Share of the slots with a published ``certainty_spill`` and a realised side
    #: whose side was wrong (:data:`SPILL_SIDE`); 0 without any.
    wrong_side: float
    #: Distinct outcomes among them (``analysis.scoring.Realised.outcome``): one fill prices
    #: every slot before it and the afternoon's spill after it, so this, not
    #: ``count``, is roughly how many independent outcomes were scored.
    outcomes: int | None = None


def _by_bucket[T](
    slots: Iterable[IssueSlot], value: Callable[[IssueSlot], T | None]
) -> dict[str, list[tuple[IssueSlot, T]]]:
    """Each slot with a value, in its horizon bucket (as :func:`surplus_errors`)."""
    found: dict[str, list[tuple[IssueSlot, T]]] = {
        bucket_label(low, high): [] for low, high in BUCKETS
    }
    for slot in slots:
        got = value(slot)
        label = hours_ahead_bucket(max(slot.start - slot.issued_at, timedelta(0)))
        if got is not None and label is not None:
            found[label].append((slot, got))
    return found


def _outcomes(slots: Sequence[IssueSlot], events: Mapping[datetime, object] | None) -> int | None:
    """How many distinct outcomes ``slots`` rest on (None: not given)."""
    if events is None:
        return None
    return len({events[slot.start] for slot in slots if slot.start in events})


def price_errors(
    slots: Iterable[IssueSlot],
    realised: Mapping[datetime, float],
    events: Mapping[datetime, object] | None = None,
    spilled: Mapping[datetime, bool] | None = None,
) -> list[PriceErrors]:
    """``marginal_cost`` against the realised price, per horizon bucket.

    ``events`` (``analysis.scoring.Realised.outcome``) counts the distinct outcomes behind them;
    ``spilled`` (``analysis.scoring.Realised.spilled``) scores the side (:data:`SPILL_SIDE`).
    """
    result = []
    for label, pairs in _by_bucket(
        slots, lambda slot: realised.get(slot.start) if slot.marginal_cost is not None else None
    ).items():
        diffs = [(slot.marginal_cost or 0.0) - actual for slot, actual in pairs]
        count = len(diffs)
        sides = [
            (slot.certainty_spill >= SPILL_SIDE) != spilled[slot.start]
            for slot, _ in pairs
            if spilled is not None and slot.certainty_spill is not None and slot.start in spilled
        ]
        result.append(
            PriceErrors(
                label=label,
                count=count,
                bias=sum(diffs) / count if count else 0.0,
                mae=sum(abs(d) for d in diffs) / count if count else 0.0,
                wrong_side=sum(sides) / len(sides) if sides else 0.0,
                outcomes=_outcomes([slot for slot, _ in pairs], events),
            )
        )
    return result


@dataclass(frozen=True, slots=True)
class RegimeAgreement:
    """Published ``regime`` against the metered one."""

    label: str
    count: int
    agreed: int
    #: (published, metered) to slots.
    pairs: Counter[tuple[Regime, Regime]] = field(default_factory=Counter)


def regime_agreement(
    slots: Iterable[IssueSlot], realised: Mapping[datetime, Regime]
) -> list[RegimeAgreement]:
    """How often the published regime was the one the plant then ran in, per bucket.

    The published regime is the expected (p50) case's; ``curtail`` cannot be
    metered here and is compared as ``export``.
    """

    def said_and_metered(slot: IssueSlot) -> tuple[Regime, Regime] | None:
        metered = realised.get(slot.start)
        if slot.regime is None or metered is None:
            return None
        return (Regime.EXPORT if slot.regime is Regime.CURTAIL else slot.regime, metered)

    result = []
    for label, pairs in _by_bucket(slots, said_and_metered).items():
        counted = Counter(pair for _, pair in pairs)
        agreed = sum(n for (said, actual), n in counted.items() if said is actual)
        result.append(RegimeAgreement(label, len(pairs), agreed, counted))
    return result


@dataclass(frozen=True, slots=True)
class Calibration:
    """A published probability against what happened."""

    label: str
    count: int
    #: Mean squared difference between the probability and the outcome (0 or 1).
    brier: float
    #: Probability, to a hundredth (``k/n`` for up to 50 scenarios, ten by
    #: default), to (slots, share that happened).
    by_value: dict[float, tuple[int, float]]
    #: Distinct realised events among the slots (as :attr:`PriceErrors.outcomes`).
    outcomes: int | None = None


def calibration(
    slots: Iterable[IssueSlot],
    probability: Callable[[IssueSlot], float | None],
    happened: Mapping[datetime, bool],
    events: Mapping[datetime, object] | None = None,
) -> list[Calibration]:
    """``probability`` of each slot against ``happened``, per bucket.

    With ten equally likely PV scenarios the certainties are tenths; a
    calibrated one comes true about that share of the time. The scenarios are
    whole days (the library's valuation: a day is bright or dull as a whole), so the
    slots before one fill are nearly one trial: ``outcomes`` says how many.
    """
    result = []
    for label, pairs in _by_bucket(
        slots,
        lambda slot: (
            (probability(slot), happened[slot.start])
            if probability(slot) is not None and slot.start in happened
            else None
        ),
    ).items():
        outcomes = [(p or 0.0, float(hit)) for _, (p, hit) in pairs]
        grouped: dict[float, list[float]] = {}
        for p, hit in outcomes:
            grouped.setdefault(round(p, 2), []).append(hit)
        count = len(outcomes)
        result.append(
            Calibration(
                label=label,
                count=count,
                brier=sum((p - hit) ** 2 for p, hit in outcomes) / count if count else 0.0,
                by_value={
                    p: (len(hits), sum(hits) / len(hits)) for p, hits in sorted(grouped.items())
                },
                outcomes=_outcomes([slot for slot, _ in pairs], events),
            )
        )
    return result
