"""When to publish a valuation, why, and for how long.

**Why a valuation is published:**

- ``forecast_basis``: the values rest on something new, so a consumer may want to
  re-plan. The first valuation; a new PV forecast (its issue time changed); a new load
  forecast (its issue time changed); new prices (a slot both valuations cover is priced
  differently); a change in which inputs are usable (a fallback began, ended or changed
  cause); or the previous valuation expired before this one.
- ``live``: the timer, so ``valid_until`` never lapses while the inputs are healthy.

Valuations are at least :attr:`IssuePolicy.min_interval` apart, except the first; a
reason held back by that is found again at the next check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from microgrid.engine import Assessment
from microgrid.engine.series import require_utc

#: Prices closer than this are the same price.
_PRICE_TOLERANCE = 1e-6


class Reason(StrEnum):
    """Why a valuation was published."""

    FORECAST_BASIS = "forecast_basis"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class IssuePolicy:
    """Timing of valuations."""

    #: How long a valuation stays valid; a consumer treats an expired one as absent.
    valid_for: timedelta = timedelta(minutes=30)
    #: Published again on this timer (``live``), so validity never lapses while healthy.
    refresh: timedelta = timedelta(minutes=10)
    #: Never published more often than this, except the first.
    min_interval: timedelta = timedelta(minutes=1)

    def __post_init__(self) -> None:
        if not timedelta(0) < self.min_interval <= self.refresh < self.valid_for:
            raise ValueError("need 0 < min_interval <= refresh < valid_for")


@dataclass(frozen=True, slots=True)
class Basis:
    """What a valuation's forecasts rest on; a change is a new forecast basis."""

    pv_issued_at: datetime | None
    #: (import, export) per kWh by slot start, compared on the slots both cover.
    prices: Mapping[datetime, tuple[float, float]]
    load_issued_at: datetime | None = None

    def same_as(self, other: Basis) -> bool:
        """True when nothing the values rest on changed."""
        if self.pv_issued_at != other.pv_issued_at or self.load_issued_at != other.load_issued_at:
            return False
        shared = self.prices.keys() & other.prices.keys()
        return all(
            abs(a - b) <= _PRICE_TOLERANCE
            for slot in shared
            for a, b in zip(self.prices[slot], other.prices[slot], strict=True)
        )


@dataclass(frozen=True, slots=True)
class LastIssue:
    """What the policy remembers about the previous valuation."""

    issued_at: datetime
    valid_until: datetime
    basis: Basis
    assessment: Assessment


def decide(
    policy: IssuePolicy,
    now: datetime,
    last: LastIssue | None,
    basis: Basis,
    assessment: Assessment,
) -> Reason | None:
    """Whether to publish now, and why; None to wait."""
    require_utc(now)
    if last is None:
        return Reason.FORECAST_BASIS
    if now >= last.valid_until:
        return Reason.FORECAST_BASIS  # consumers have been without a valid valuation
    if now - last.issued_at < policy.min_interval:
        return None
    if not basis.same_as(last.basis) or assessment != last.assessment:
        return Reason.FORECAST_BASIS
    if now - last.issued_at >= policy.refresh:
        return Reason.LIVE
    return None
