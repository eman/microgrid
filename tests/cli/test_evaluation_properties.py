"""What holds for any input: lapses in validity."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

from hypothesis import given
from hypothesis import strategies as st

from microgrid.cli.evaluation import lapses

T0 = datetime(2026, 9, 25, 7, 0, tzinfo=UTC)


@given(
    st.lists(
        st.tuples(st.integers(0, 3000), st.integers(1, 120)),  # (issued minute, valid minutes)
        max_size=30,
    ),
    st.integers(0, 3200),
)
def test_lapses_are_disjoint_ordered_and_outside_every_validity(
    spec: list[tuple[int, int]], until_minute: int
) -> None:
    minute = timedelta(minutes=1)
    issues = [(T0 + a * minute, T0 + (a + d) * minute) for a, d in spec]
    until = T0 + until_minute * minute
    gaps = lapses(issues, until=until)
    assert all(since < to for since, to in gaps)
    assert all(a[1] <= b[0] for a, b in pairwise(gaps))
    for since, to in gaps:
        # No issue is valid anywhere inside the gap.
        assert not any(issued < to and since < valid for issued, valid in issues)
    assert lapses(list(reversed(issues)), until=until) == gaps
