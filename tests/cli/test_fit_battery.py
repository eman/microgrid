"""``microgrid fit-battery``'s least squares: numpy's answer without numpy."""

import math

from microgrid.cli.fit_battery import FORMS, _fit, _parameters


def test_a_full_rank_fit_solves_the_normal_equations() -> None:
    rows = [([1.0, x], 2.0 + 3.0 * x) for x in (0.0, 1.0, 2.0, 5.0)]
    assert [round(v, 12) for v in _fit(rows, (0, 1))] == [2.0, 3.0]


def test_dependent_terms_take_the_minimum_norm_solution() -> None:
    """Two terms that never differ split the fit, as ``numpy.linalg.lstsq`` does, and a
    term always 0 is fitted as 0."""
    same = [([x, x], 2 * x) for x in (1.0, 2.0, 3.0)]
    assert [round(v, 12) for v in _fit(same, (0, 1))] == [1.0, 1.0]
    zero = [([x, 0.0], 2 * x) for x in (1.0, 2.0, 3.0)]
    assert [round(v, 12) for v in _fit(zero, (0, 1))] == [2.0, 0.0]


def test_a_window_without_a_discharge_hour_leaves_its_efficiency_unknown() -> None:
    """No discharge hour, so ``b`` is 0; the discharge efficiency is inf, not a crash
    (numpy's float64 gave inf)."""
    losses = [
        ([1.0, 1.0, kw, kw * kw, 0.0, 0.0, 0.0], 0.05 + 0.03 * kw)
        for kw in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    ]
    hours = [([kwh, 0.0], 0.95 * kwh) for kwh in (1.0, 2.0, 3.0)]
    found = _parameters(losses, hours)
    assert found["b"] == 0.0 and math.isinf(found["eta_discharge"])
    assert len(FORMS["full"]) == 6
