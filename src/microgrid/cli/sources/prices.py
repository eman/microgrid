"""Import and export prices put together as one forecast of prices.

Many integrations publish the import price and the export (feed-in) price apart, each
its own list of intervals, or the import price alone. The engine takes both per
interval (:class:`~microgrid.engine.series.Price`), so a reader joins them here:

- **Two forecasts:** split at every boundary of either, and kept where both are known.
  Where one has a gap, so does the result.
- **A fixed export price:** the same export price for every interval of the import
  forecast (a feed-in tariff that does not change, from the configuration).

The result is issued at the earlier of the two issues: prices are only as fresh as
their older part, and the engine ages them from it.
"""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise

from microgrid.cli.sources import SourceError
from microgrid.engine.series import Forecast, Interval, Price

__all__ = [
    "PriceJoinError",
    "join_prices",
]


class PriceJoinError(SourceError):
    """The import and export prices share no interval."""


def _value_at(
    intervals: tuple[Interval[float], ...], start: datetime, end: datetime
) -> float | None:
    """The value of the interval holding all of ``[start, end)``, else None."""
    for interval in intervals:
        if interval.start <= start and end <= interval.end:
            return interval.value
        if interval.start >= end:
            break
    return None


def join_prices(
    imports: Forecast[float], exports: Forecast[float] | float, *, source: str = ""
) -> Forecast[Price]:
    """The import forecast joined with the export forecast, or a fixed export price.
    Raises :class:`PriceJoinError` when no interval has both."""
    if not isinstance(exports, Forecast):
        fixed = float(exports)
        return Forecast(
            tuple(Interval(i.start, i.end, Price(i.value, fixed)) for i in imports.intervals),
            imports.issued_at,
            source or imports.source,
        )
    bounds = sorted(
        {i.start for i in imports.intervals}
        | {i.end for i in imports.intervals}
        | {e.start for e in exports.intervals}
        | {e.end for e in exports.intervals}
    )
    joined: list[Interval[Price]] = []
    for start, end in pairwise(bounds):
        p_import = _value_at(imports.intervals, start, end)
        p_export = _value_at(exports.intervals, start, end)
        if p_import is not None and p_export is not None:
            joined.append(Interval(start, end, Price(p_import, p_export)))
    if not joined:
        raise PriceJoinError(
            f"the import ({imports.source or 'prices'}) and export"
            f" ({exports.source or 'prices'}) prices share no interval"
        )
    return Forecast(
        tuple(joined),
        min(imports.issued_at, exports.issued_at),
        source or f"{imports.source}+{exports.source}",
    )
