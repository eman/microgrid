"""The library's architecture: no I/O in the engine (the one rule), the engine importing
nothing from the CLI, no relative imports, the CLI using the engine's public names
alone, and the CLI's public surface declared (each module's ``__all__``).
"""

import ast
import importlib
from importlib.util import find_spec
from pathlib import Path

from architecture import (
    CHECKS_SOURCE,
    CLI_DIR,
    ENGINE_SOURCE,
    MODEL_SOURCES,
    PLANT_SOURCE,
    REPO,
    _cli_offences,
    _imports,
    _io_offences,
    _offenders,
    _private_uses,
    _tier_offences,
    cli_modules,
    cli_surface,
)

#: The library's clients in this project: its CLI, its tests of the CLI, its scripts.
LIBRARY_CLIENTS = ("src/microgrid/cli", "tests/cli", "scripts")


def test_the_models_do_no_io() -> None:
    """The engine, the plant model and the checks they share: their source, not their
    tests (which build a ZoneInfo for their fixtures)."""
    for root in MODEL_SOURCES:
        assert not _io_offences(root), root
    assert not _io_offences(CHECKS_SOURCE.parent, [CHECKS_SOURCE])


def test_the_io_rule_sees_each_kind_of_offence(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import zoneinfo as z\n"
        "from datetime import datetime\n"
        "zone = z.ZoneInfo('UTC')\n"
        "stamp = datetime.now()\n"
        "local = stamp.astimezone()\n"
        "text = open('x').read()\n"
        "mod = __import__('os')\n"
        "copy = asdict(zone)\n"
        "local = stamp.astimezone(None)\n"
        "local = stamp.astimezone(tz=None)\n"
        "body = eval('1')\n"
        "body = Path('x').read_text()\n"
        "import numpy as np\n"
        "from numpy import loadtxt\n"
        "table = np.load('x.npy')\n"
        "rows = loadtxt('x.txt')\n"
        "table.tofile('y')\n"
        "table = np.lib.npyio.load('a')\n"
        "table = np.lib.format.open_memmap('a')\n",
        encoding="utf-8",
    )
    (tmp_path / "star.py").write_text("from numpy import *\ntable = load('a')\n", encoding="utf-8")
    (tmp_path / "good.py").write_text(
        "from __future__ import annotations\n"
        "from datetime import UTC, datetime\n"
        "from . import sibling\n"
        "from ._private import helper\n"
        "from microgrid._checks import require_utc\n"
        "local = datetime(2026, 1, 1, tzinfo=UTC).astimezone(UTC)\n"
        "same = isinstance(local, datetime)\n"
        "part = reader.load(item)\n"
        "import json, statistics\n"
        "import scipy\n",
        encoding="utf-8",
    )
    assert _io_offences(tmp_path) == [
        "bad.py:1: imports os",
        "bad.py:2: imports pathlib",
        "bad.py:5: calls ZoneInfo",
        "bad.py:6: calls now",
        "bad.py:7: astimezone() reads the system zone",
        "bad.py:8: calls open",
        "bad.py:9: calls __import__",
        "bad.py:10: calls asdict",
        "bad.py:11: astimezone() reads the system zone",
        "bad.py:12: astimezone() reads the system zone",
        "bad.py:13: calls eval",
        "bad.py:14: calls read_text",
        "bad.py:17: calls load",
        "bad.py:18: calls loadtxt",
        "bad.py:19: calls tofile",
        "bad.py:20: calls load",
        "bad.py:21: calls open_memmap",
        "star.py:2: calls load",
    ]


def test_the_engine_imports_nothing_from_the_cli() -> None:
    # The CLI (``microgrid.cli``) reads files, and the engine reaches none of it.
    assert not _offenders(ENGINE_SOURCE, ("microgrid.cli",))
    # The plant model stands alone: not even the engine.
    assert not _offenders(PLANT_SOURCE, ("microgrid.cli", "microgrid.engine"))
    assert not [name for name in _imports(CHECKS_SOURCE) if name.startswith("microgrid")]


def test_the_import_rule_sees_a_package_imported_from_its_parent(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("from microgrid import cli\n", encoding="utf-8")
    assert _offenders(tmp_path, ("microgrid.cli",)) == {"bad.py: microgrid.cli"}


def test_the_package_root_imports_nothing() -> None:
    """``microgrid`` itself is a docstring: importing ``microgrid.engine`` loads no CLI."""
    root = CLI_DIR.parent / "__init__.py"
    assert not [
        n
        for n in ast.walk(ast.parse(root.read_text()))
        if isinstance(n, ast.Import | ast.ImportFrom)
    ]


def test_the_library_uses_no_relative_import() -> None:
    """Every import in the engine and the CLI names its module, so the rules above see
    it: a relative one could reach the CLI (``from .. import cli``) unseen."""
    found = [
        f"{path.relative_to(root)}:{node.lineno}"
        for root in (ENGINE_SOURCE, PLANT_SOURCE, CLI_DIR)
        for path in sorted(root.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]
    assert found == []


def test_the_cli_uses_the_engines_public_names_alone() -> None:
    """The CLI, its tests and the library's scripts are clients of
    the engine: no private module or name, and only the tiers' exported names."""
    offenders = []
    for root in LIBRARY_CLIENTS:
        files = sorted((REPO / root).rglob("*.py"))
        assert files, f"no Python files under {root}: the rule would pass vacuously"
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            uses = _private_uses(tree) + _tier_offences(tree)
            offenders += [f"{path.relative_to(REPO)}: {use}" for use in uses]
    assert offenders == []


def test_every_cli_module_declares_its_public_surface() -> None:
    """Each CLI module says what a client may import (``__all__``), and each name it
    declares exists."""
    missing, unknown = [], []
    for module in cli_modules():
        surface = cli_surface(module)
        if surface is None:
            missing.append(module)
            continue
        loaded = importlib.import_module(module)
        unknown += [
            f"{module}.{name}"
            for name in surface
            if not hasattr(loaded, name) and find_spec(f"{module}.{name}") is None
        ]
    assert missing == [] and unknown == []


def test_the_cli_surface_rule_sees_each_kind_of_offence() -> None:
    source = (
        "from microgrid.cli.history import Bindings, _HOUR\n"
        "from microgrid.cli.main import main, _value\n"
        "import microgrid.cli.offline\n"
        "from microgrid.cli import history\n"
        "from microgrid.cli.sources import SourceError, read\n"
    )
    assert _cli_offences(ast.parse(source)) == [
        "1: microgrid.cli.history._HOUR",
        "2: microgrid.cli.main._value",
    ]
    through = (
        "from microgrid.cli import history as h\n"
        "import microgrid.cli.offline\n"
        "from microgrid.cli import sources\n"
        "h.read_recorded\n"
        "h._HOUR\n"
        "microgrid.cli.offline._private\n"
        "sources.read.read_now\n"
        "sources.read._hidden\n"
    )
    assert _cli_offences(ast.parse(through)) == [
        "5: microgrid.cli.history._HOUR",
        "6: microgrid.cli.offline._private",
        "8: microgrid.cli.sources.read._hidden",
    ]


def test_the_contract_rule_sees_a_private_module_at_any_depth() -> None:
    def uses(source: str) -> list[str]:
        return _private_uses(ast.parse(source))

    for reaching in (
        "from microgrid.plant._dispatch import simulate",
        "import microgrid.plant._meters",
        "from microgrid.plant import _battery",
        "from microgrid.engine import _api",
        "x = 'microgrid.plant._dispatch.simulate'",
        "import microgrid.plant\nmicrogrid.plant._dispatch.simulate",
        "import microgrid.plant as mp\nmp._dispatch.simulate",
        "from microgrid._checks import require_utc",
        "import microgrid._checks",
        "from microgrid import _checks",
        "from microgrid import plant\nplant._battery.Battery",
        "import microgrid.plant as mp\nmp._meters",
        "import microgrid.plant\ngetattr(microgrid.plant, '_dispatch')",
        "import microgrid.engine\nmock.patch.object(microgrid.engine, '_api')",
        "import microgrid.plant as mp\nmonkeypatch.setattr(mp, '_dispatch', 1)",
        "from microgrid import plant\nhasattr(plant, '_meters')",
        "getattr(importlib.import_module('microgrid.plant'), '_dispatch')",
        "import microgrid.engine as me\nmonkeypatch.setattr(target=me, name='_api', value=1)",
        "import microgrid.engine\nmock.patch.object(microgrid.engine, attribute='_api')",
        "importlib.import_module('microgrid.plant')._dispatch",
        "import_module('microgrid.engine')._api.x",
        "import microgrid.engine\nmicrogrid.engine._api",
    ):
        assert uses(reaching), reaching
    for public in (
        "from microgrid.plant import Plant",
        "from microgrid.engine import Plant, simulate",
        "import microgrid.engine\nmicrogrid.plant.Plant",
        "x = 'microgrid.engine.__version__'",
        "import microgrid.plant\nmicrogrid.plant.__name__",
        "from microgrid.engine import simulate\nsimulate.__name__",
        "class A:\n    _x = 1\nA()._x",
        "import microgrid.engine\nmonkeypatch.setattr(microgrid.engine, 'MODEL_VERSION', 'x')",
        "getattr(importlib.import_module('json'), '_default_encoder')",
        "monkeypatch.setattr(foo('microgrid.engine'), '_x', 1)",
        "importlib.import_module('microgrid.plant').Plant",
    ):
        assert not uses(public), public


def test_the_tier_rule_sees_each_kind_of_offence() -> None:
    source = (
        "from microgrid.engine import Engine, Plant\n"
        "from microgrid.engine._api import Engine\n"
        "import microgrid.plant._paths\n"
        "from microgrid.plant import _plant, Plant, examples\n"
        "from microgrid.engine.series import SLOT, _align\n"
        "from microgrid.engine.extend import SlotFlow\n"
        "from microgrid.engine.analysis import scoring\n"
        "import microgrid.engine.testing\n"
    )
    assert _tier_offences(ast.parse(source)) == [
        "1: microgrid.engine.Plant",
        "2: microgrid.engine._api",
        "3: microgrid.plant._paths",
        "4: microgrid.plant._plant",
        "5: microgrid.engine.series._align",
    ]


def test_the_plant_models_public_modules_are_tiers() -> None:
    """``microgrid.plant.flows`` and ``.port`` are public: a client follows the docs and
    imports them."""
    source = (
        "from microgrid.plant.flows import SlotFlow, surplus\n"
        "from microgrid.plant.port import PlantModel\n"
        "import microgrid.plant.flows\n"
    )
    assert _tier_offences(ast.parse(source)) == []
