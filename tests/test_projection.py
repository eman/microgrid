"""What a projection is: flows, surplus and regime, on hand-built flows.

No plant model here: these are the rules the price model reads any plant model's
flows by. The plant model leaving a surplus is tested in ``test_plant.py``.
"""

import ast
import inspect
import subprocess
import sys
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from engine_fixtures import ENGINE, engine_imports
from microgrid.engine import _value
from microgrid.engine._timeline import SLOT
from microgrid.plant import flows
from microgrid.plant.flows import Regime, SlotDispatch, SlotFlow, Surplus, regimes, surplus

T0 = datetime(2026, 9, 23, 19, 0, tzinfo=UTC)


def _flow(index: int = 0, **kw: Any) -> SlotFlow:
    return SlotFlow(start=T0 + index * SLOT, soc_start=0.5, soc_end=0.5, **kw)


def test_moving_and_spare_pv() -> None:
    flow = _flow(pv_to_battery=1.0, grid_to_battery=0.5, battery_to_load=0.25, pv_export=2.0)
    assert flow.moving == 1.75
    assert _flow(pv_to_battery=1.0, pv_export=2.0, pv_curtail=0.5).pv_spare == 3.5
    assert _flow(pv_to_battery=1.0, pv_clipped=2.0).pv_spare == 1.0  # nothing can use it


def test_the_totals_count_every_path() -> None:
    # The battery's output and its movement count every path, and PV
    # charging on the DC bus is spare too.
    flow = _flow(
        battery_to_load=0.25,
        battery_to_dc_load=0.5,
        battery_export=1.0,
        dc_pv_to_battery=2.0,
        pv_to_battery=0.125,
    )
    assert flow.battery_out == 1.75
    assert flow.moving == 3.875
    assert flow.pv_spare == 2.125


def test_a_slot_flow_is_keyword_only_and_its_dispatch_optional() -> None:
    # No positional argument; a plant model without the dispatch's
    # bookkeeping leaves it at its default, and every path is 0 by default.
    with pytest.raises(TypeError):
        SlotFlow(T0, 0.5, 0.5)  # type: ignore[call-arg]
    flow = _flow()
    assert flow.dispatch == SlotDispatch()
    assert flow.dispatch.topup_gap is None and not flow.dispatch.held
    names = [field.name for field in fields(SlotFlow)]
    assert all(getattr(flow, name) == 0.0 for name in names[3:] if name != "dispatch")


def test_clipped_pv_is_neither_surplus_nor_spill() -> None:
    flows = [_flow(pv_clipped=3.0, pv_to_battery=1.0)]
    found = surplus(flows, [0.1])
    assert found.kw == (0.0,)
    assert regimes(flows, found) == (Regime.BATTERY,)
    assert SlotFlow(start=T0, soc_start=0.5, soc_end=0.5).pv_clipped == 0.0


def test_surplus_is_what_is_exported_or_curtailed() -> None:
    flows = [_flow(0, pv_to_battery=3.0), _flow(1, pv_export=2.0, pv_curtail=1.0)]
    found = surplus(flows)
    assert found.kw == (0.0, 3.0)
    assert found.curtailed == (False, False)  # more exported than curtailed


def test_surplus_under_a_watt_is_float_noise() -> None:
    found = surplus([_flow(pv_export=0.0009)], [0.1])
    assert found == Surplus(kw=(0.0,), curtailed=(False,), value=(None,))


def test_curtailed_surplus_is_worth_nothing() -> None:
    flows = [_flow(pv_curtail=3.0)]
    found = surplus(flows, [0.5])
    assert found.kw == (3.0,) and found.curtailed == (True,)
    assert found.value == (0.0,)
    assert regimes(flows, found) == (Regime.CURTAIL,)


def test_surplus_is_valued_at_its_own_slots_export_price() -> None:
    flows = [_flow(0, pv_export=1.5), _flow(1, pv_export=2.0, pv_curtail=4.0)]
    found = surplus(flows, [0.10, 0.20])
    assert found.value[0] == pytest.approx(0.10)  # all exported
    # 2 kW exported at 0.20, the rest curtailed at 0: weighted by energy.
    assert found.value[1] == pytest.approx(2.0 * 0.20 / 6.0)
    assert surplus(flows).value == (None, None)  # exported, but no price given


def test_export_prices_must_cover_every_flow() -> None:
    with pytest.raises(ValueError, match="p_export"):
        surplus([_flow(0), _flow(1)], [0.1])


def test_regimes() -> None:
    flows = [
        _flow(0, pv_export=1.0),  # spare PV: exported
        _flow(1, pv_curtail=1.0),  # spare PV: curtailed
        _flow(2, grid_to_load=0.5, battery_to_load=0.5),  # the grid serves: import
        _flow(3, grid_to_battery=1.0),  # topping up: import
        _flow(4, battery_to_load=1.0),  # the battery serves
        _flow(5, pv_to_battery=1.0),  # the battery charges
        _flow(6),  # nothing moves: import
    ]
    assert regimes(flows, surplus(flows)) == (
        Regime.EXPORT,
        Regime.CURTAIL,
        Regime.IMPORT,
        Regime.IMPORT,
        Regime.BATTERY,
        Regime.BATTERY,
        Regime.IMPORT,
    )


PLANT = Path(flows.__file__).parent


def _plant_imports(source: str) -> set[str]:
    """The ``microgrid`` modules ``source`` imports, a relative import as ``<relative>``."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                found.add("<relative>")
            elif node.module and node.module.split(".")[0] == "microgrid":
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.split(".")[0] == "microgrid")
    return found


def test_the_plant_model_imports_nothing_of_the_price_model_or_the_cli() -> None:
    """The plant model and its simulator are useful beyond the
    marginal price, so ``microgrid.plant`` stands alone: it imports only itself and the
    shared checks, never ``microgrid.engine`` or ``microgrid.cli``."""
    for path in PLANT.rglob("*.py"):
        for name in _plant_imports(path.read_text(encoding="utf-8")):
            assert name.startswith(("microgrid.plant", "microgrid._checks")), (path.name, name)


def test_importing_the_plant_model_loads_no_price_code() -> None:
    """In a fresh interpreter, ``import microgrid.plant`` (and its examples) loads no
    module of the engine or the CLI."""
    probe = (
        "import sys, microgrid.plant, microgrid.plant.examples\n"
        "print(sorted(m for m in sys.modules if m.startswith(('microgrid.engine', "
        "'microgrid.cli'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]", result.stdout


def test_the_rule_sees_every_form_of_import() -> None:
    assert engine_imports("from microgrid.engine import _plant") == {"microgrid.engine._plant"}
    assert engine_imports("import microgrid.engine._value") == {"microgrid.engine._value"}
    assert engine_imports("from microgrid.engine._supply import x") == {"microgrid.engine._supply"}
    assert engine_imports("from . import _plant") == {"<relative>"}
    assert engine_imports("from ._plant import simulate") == {"<relative>"}
    assert engine_imports("import numpy") == set()


def test_nothing_that_prices_spare_pv_reads_clipped_pv() -> None:
    # Clipped PV is neither spill nor surplus: spilling, _spill_value and surplus never
    # read it. Only the module that defines it, a plant model reporting it (the ``plant``
    # subpackage), and the price rule's fill event may name it: a battery on the
    # clipping hybrid's DC bus refills from it, a later use than spilling.
    for path in ENGINE.rglob("*.py"):
        relative = path.relative_to(ENGINE)
        # ``testing``'s contract checks the balance a plant model reports, clip included.
        if relative.name in ("_value.py", "testing.py"):
            continue
        assert "pv_clipped" not in path.read_text(encoding="utf-8"), relative
    for function in (_value.spilling, _value._spill_value, flows.surplus):
        assert "pv_clipped" not in inspect.getsource(function), function.__name__
