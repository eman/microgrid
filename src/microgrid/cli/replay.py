"""``microgrid replay``: valuations at past times, scored against what the meters
realised. Each time in the span is valued from what a client had received by then
(``offline.value``), and its slots are scored as a client scores the valuations it
published (:mod:`microgrid.cli.evaluation`): the surplus against the PV the plant
exported or curtailed, the marginal cost against the price a kWh realised by today's
rule (``microgrid.engine.analysis.scoring.realise``), the regime against the metered one.

The span's inputs and meters come from a dataset (``--data``). Only slots inside the
span are scored: past its end, nothing is metered.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta

from microgrid.cli.dataset import Dataset
from microgrid.cli.evaluation import (
    IssueSlot,
    price_errors,
    pv_export_kw,
    regime_agreement,
    surplus_errors,
)
from microgrid.cli.history import DAY, Bindings, History, Limits, read_recorded, stored_costs
from microgrid.cli.inputs import currency
from microgrid.cli.offline import Setup, dataset_lots_async, value_with
from microgrid.engine import ValuationKind
from microgrid.engine.analysis.ground_truth import meters_plant
from microgrid.engine.analysis.scoring import (
    battery_exports,
    observed_flows,
    realise,
    storage_of,
)
from microgrid.engine.series import means, slot_starts

__all__ = [
    "add_arguments",
    "run",
]


async def run(
    args: argparse.Namespace,
    bindings: Bindings,
    history: History,
    setup: Setup,
    dataset: Dataset,
    start: datetime,
    end: datetime,
    *,
    limits: Limits | None = None,
) -> int:
    """Value every ``args.every`` in ``[start, end)`` on the engine's slot, and print the
    scores; the record judged by ``limits`` (``offline.limits_of``)."""
    slots: list[IssueSlot] = []
    valued = 0
    unpriced = 0
    #: Times that fell back, by cause: counted, not scored (the fallback is no forecast;
    #: R-7).
    fell_back: Counter[str] = Counter()
    lots = await dataset_lots_async(setup, dataset, limits=limits)
    at = start
    while at < end:
        valuation = value_with(setup, dataset, at, lots)
        if valuation.kind is ValuationKind.UNPRICED:
            unpriced += 1
        elif valuation.kind is not ValuationKind.STORED_VALUE_V1:
            fell_back[",".join(valuation.degraded) or valuation.kind.value] += 1
        else:
            valued += 1
            slots += [
                IssueSlot(
                    issued_at=at,
                    start=slot.start,
                    surplus_kw=slot.surplus_kw,
                    surplus_kw_p10=slot.surplus_kw_p10,
                    marginal_cost=slot.marginal_cost,
                    regime=slot.regime,
                    certainty_spill=slot.certainty_spill,
                    certainty_covered=slot.certainty_covered,
                )
                for slot in valuation.slots
                if slot.start < end
            ]
        at += args.every

    # What was metered: the meters split through the
    # plant's conversion without its DC-bus loads (a load like any other).
    step = setup.engine.config.slot
    meters = replace(bindings, plant=meters_plant(bindings.plant), dc_load=None)
    recorded = await read_recorded(history, meters, start, end, step=step)
    starts = list(slot_starts(start, end, step))
    grid = means(
        await history.numeric_series(bindings.grid, start, end, carry_in=DAY),
        starts,
        step,
    )
    battery = means(
        await history.numeric_series(bindings.battery, start, end, carry_in=DAY),
        starts,
        step,
    )
    plant_battery = bindings.plant.battery
    if plant_battery is None:
        raise SystemExit("the plant has no battery: replay scores a plant with one")
    surplus = {
        at: pv_export_kw(g, b, battery=plant_battery)
        for at, g, b in zip(starts, grid, battery, strict=True)
        if g is not None and b is not None
    }
    flows = observed_flows(
        recorded.starts,
        recorded.flows,
        recorded.stored_after,
        recorded.stored_before,
        plant=meters.plant,
    )
    realised = realise(
        storage_of(meters.plant),  # the meters' plant (meters_plant)
        flows,
        recorded.p_import,
        recorded.p_export,
        step=step,
        unscored=battery_exports(recorded.starts, recorded.flows),
        # What a battery kWh realised by the rule the valuations ran: its stored cost
        # where they priced one so (``Pricing.battery_value``).
        stored_cost=(
            # The valuations' plant: what they priced a stored kWh through (the meters'
            # plant drops what the meters cannot see, such as a DC charge limit).
            stored_costs(recorded, setup.plant, lots)
            if setup.engine.config.pricing.battery_value == "stored_cost"
            else None
        ),
    )

    print(
        f"{valued} valuations every {args.every} scored, {len(slots)} slots inside the span;"
        f" fell back (not scored): {sum(fell_back.values())}"
        + "".join(f", {cause} {n}" for cause, n in sorted(fell_back.items()))
        + f"; no prices: {unpriced}"
    )
    print("\nsurplus kW against the PV exported or curtailed, by hours ahead:")
    print("bucket       n     bias     MAE   p10 n   p10 met")
    for errors in surplus_errors(slots, surplus):
        print(
            f"{errors.label:8s} {errors.count:6d}  {errors.bias_kw:+7.3f} {errors.mae_kw:7.3f}"
            f"   {errors.p10_count:5d}   {errors.p10_met:6.1%}"
        )
    money = currency(dataset) or "price"  # the prices' own: none is converted
    print(f"\nmarginal cost against the price a kWh realised ({money}/kWh), by hours ahead:")
    print("bucket       n  outcomes     bias     MAE    off")
    for price in price_errors(slots, realised.price, realised.outcome, realised.spilled):
        print(
            f"{price.label:8s} {price.count:6d}  {price.outcomes or 0:8d}"
            f"  {price.bias:+7.4f} {price.mae:7.4f}  {price.wrong_side:5.1%}"
        )
    print("\nregime against the metered regime:")
    for agreement in regime_agreement(slots, realised.regime):
        if agreement.count:
            print(f"  {agreement.label:8s} {agreement.agreed} of {agreement.count} agreed")
    return 0


def add_arguments(parser: argparse.ArgumentParser, duration: object) -> None:
    """The command's flags (beside the source's)."""
    parser.add_argument(
        "--every",
        type=duration,  # type: ignore[arg-type]
        default=timedelta(hours=1),
        metavar="D",
        help="a valuation every D (default 1h)",
    )
