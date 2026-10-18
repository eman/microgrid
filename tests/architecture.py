"""The library's architecture rules, as functions a test suite calls: the one rule (no
I/O in the engine), the engine's public names and tiers, and the CLI's declared surface.
The library's own tests (``test_architecture``) hold the library to them; a client's
tests can hold the client to the public names.
"""

import ast
import importlib
from pathlib import Path

import microgrid.cli
import microgrid.engine
import microgrid.plant

#: The repository's root, whose project is the library.
REPO = Path(__file__).resolve().parents[1]
#: The library's CLI package (``microgrid.cli``): it reads files; the engine does not.
CLI_DIR = Path(microgrid.cli.__file__).parent
#: The engine package.
ENGINE_SOURCE = Path(microgrid.engine.__file__).parent
#: The plant model's package, and the checks both share.
PLANT_SOURCE = Path(microgrid.plant.__file__).parent
CHECKS_SOURCE = ENGINE_SOURCE.parent / "_checks.py"
#: The library's models: no I/O in any of them (the one rule).
MODEL_SOURCES = (ENGINE_SOURCE, PLANT_SOURCE)

#: Modules whose business is I/O: files, the network, processes, the clock, the
#: environment, the terminal, logging, loading code. Neither the library's models nor
#: a client's pure modules import one (the one rule: no I/O). A scan of import names is a
#: guard, not a proof; review covers the rest.
IO_MODULES = frozenset(
    {
        "os",
        "posix",
        "nt",
        "io",
        "_io",
        "builtins",
        "fcntl",
        "termios",
        "tty",
        "pty",
        "resource",
        "pwd",
        "grp",
        "syslog",
        "socketserver",
        "wsgiref",
        "concurrent",
        "runpy",
        "pkgutil",
        "zipimport",
        "site",
        "sysconfig",
        "linecache",
        "mimetypes",
        "netrc",
        "gettext",
        "locale",
        "faulthandler",
        "sys",
        "pathlib",
        "shutil",
        "glob",
        "tempfile",
        "fileinput",
        "mmap",
        "socket",
        "ssl",
        "select",
        "selectors",
        "asyncio",
        "http",
        "urllib",
        "ftplib",
        "smtplib",
        "imaplib",
        "poplib",
        "xmlrpc",
        "webbrowser",
        "subprocess",
        "multiprocessing",
        "signal",
        "ctypes",
        "importlib",
        "sqlite3",
        "dbm",
        "shelve",
        "pickle",
        "marshal",
        "gzip",
        "bz2",
        "lzma",
        "zipfile",
        "tarfile",
        "time",
        "logging",
        "getpass",
        "platform",
        "tkinter",
        "curses",
        "httpx",
        "requests",
        "aiohttp",
        "aiomqtt",
        "paho",
        "influxdb_client_3",
        "pydantic_settings",
    }
)


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # ``from microgrid import cli`` imports ``microgrid.cli``: both are named.
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _offenders(root: Path, forbidden: tuple[str, ...]) -> set[str]:
    return {
        f"{path.relative_to(root)}: {name}"
        for path in root.rglob("*.py")
        for name in _imports(path)
        if any(name == bad or name.startswith(f"{bad}.") for bad in forbidden)
    }


#: Calls the engine's source may not make, whatever the receiver: each reads a file,
#: the clock, the environment or the system's time zone, or prints. A scan of call
#: names is a guard, not a proof; review covers the rest.
ENGINE_BANNED_CALLS = frozenset(
    {
        "open",
        "__import__",
        "import_module",
        "now",
        "today",
        "utcnow",
        "fromtimestamp",
        "utcfromtimestamp",
        "print",
        "ZoneInfo",
        "no_cache",
        "from_file",
        "available_timezones",
        "reset_tzpath",
        # Code run from text, and a prompt.
        "exec",
        "eval",
        "compile",
        "input",
        "breakpoint",
        # A path's own file methods, on a path a client passed in.
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        # Banned outright: a name scan cannot see their argument, and over a zone
        # they can re-open its file.
        "asdict",
        "deepcopy",
    }
)


#: numpy's file functions: banned through numpy (an alias, or a name imported from
#: it), since names such as ``load`` are common; ``tofile`` is banned outright.
NUMPY_FILE_CALLS = frozenset(
    {
        "load",
        "loadtxt",
        "genfromtxt",
        "fromfile",
        "fromregex",
        "save",
        "savez",
        "savez_compressed",
        "savetxt",
        "memmap",
        "open_memmap",
        "DataSource",
    }
)


def _numpy_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """The names numpy is imported as, and the file functions imported from it."""
    modules: set[str] = set()
    functions: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.asname or alias.name for alias in node.names if alias.name == "numpy"}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "numpy":
            if any(alias.name == "*" for alias in node.names):
                functions |= NUMPY_FILE_CALLS  # every name, unseen
            functions |= {
                alias.asname or alias.name for alias in node.names if alias.name in NUMPY_FILE_CALLS
            }
    return modules, functions


def _root(node: ast.expr) -> str | None:
    """The name an attribute chain starts from (``np`` in ``np.lib.npyio.load``)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _system_zone(call: ast.Call) -> bool:
    """``astimezone()`` with no zone, or with a literal None: the system's zone."""
    given = [*call.args, *(keyword.value for keyword in call.keywords)]
    return not given or all(
        isinstance(value, ast.Constant) and value.value is None for value in given
    )


def _io_offences(root: Path, paths: list[Path] | None = None) -> list[str]:
    found = []
    for path in paths if paths is not None else sorted(root.rglob("*.py")):
        where = path.relative_to(root)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        numpy_modules, numpy_functions = _numpy_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                continue  # relative: the engine's own
            if isinstance(node, ast.Import | ast.ImportFrom):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for module in modules:
                    if module.split(".")[0] in IO_MODULES:
                        found.append(f"{where}:{node.lineno}: imports {module}")
            elif isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                through_numpy = (
                    isinstance(func, ast.Attribute)
                    and _root(func) in numpy_modules
                    and func.attr in NUMPY_FILE_CALLS
                ) or (isinstance(func, ast.Name) and func.id in numpy_functions)
                if name in ENGINE_BANNED_CALLS or name == "tofile" or through_numpy:
                    found.append(f"{where}:{node.lineno}: calls {name}")
                elif name == "astimezone" and _system_zone(node):
                    found.append(f"{where}:{node.lineno}: astimezone() reads the system zone")
    # By file and line: ast.walk goes breadth first.
    return sorted(found, key=lambda item: (item.split(":")[0], int(item.split(":")[1])))


#: The engine package's directory, to tell its modules from its other names.
ENGINE_DIR = Path(microgrid.engine.__file__).parent
PLANT_DIR = Path(microgrid.plant.__file__).parent


#: The engine's package: its modules are ``microgrid.engine`` and below it.
ENGINE = "microgrid.engine"
PLANT = "microgrid.plant"


CHECKS = "microgrid._checks"


def _library_root(dotted: str) -> str | None:
    """The library package ``dotted`` is in whose internals a client may not reach: the
    engine, the plant model, or the checks they share (private as a whole); None
    outside them."""
    for root in (ENGINE, PLANT, CHECKS):
        if dotted == root or dotted.startswith(f"{root}."):
            return root
    return None


def _engine_parts(dotted: str) -> list[str] | None:
    """What ``dotted`` names below ``microgrid.engine`` or ``microgrid.plant`` (``[]`` for
    the package itself), or in the shared checks (``["_checks", ...]``: private); None
    outside them (``microgrid.cli``, ``microgrid`` itself)."""
    root = _library_root(dotted)
    if root is None:
        return None
    if root == CHECKS:
        return ["_checks", *[p for p in dotted.removeprefix(CHECKS).split(".") if p]]
    return [] if dotted == root else dotted.removeprefix(f"{root}.").split(".")


def _private(name: str) -> bool:
    """A private engine name: ``_x``, not a dunder (``__file__``, ``__version__``)."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _private_path(dotted: str) -> bool:
    """Whether ``dotted`` names something private in the engine at any depth:
    ``microgrid.engine._api``, ``microgrid.plant._dispatch.simulate``; not
    ``microgrid.plant.__name__``."""
    rest = _engine_parts(dotted)
    return rest is not None and any(_private(part) for part in rest)


def _engine_module(dotted: str) -> bool:
    """Whether ``dotted`` (``microgrid.engine.x.y``) is a module or package of the engine."""
    rest = _engine_parts(dotted)
    if rest is None:
        return False
    root = _library_root(dotted)
    if root == CHECKS:
        return dotted == CHECKS
    base = PLANT_DIR if root == PLANT else ENGINE_DIR
    path = base.joinpath(*rest)
    return path.with_suffix(".py").exists() or (path / "__init__.py").exists()


def _chain(node: ast.expr) -> list[str] | None:
    """``a.b.c`` as ``["a", "b", "c"]``; None for anything not rooted in a plain name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return [node.id, *reversed(parts)]


#: Calls that name an attribute of their first argument by string: ``getattr`` and its
#: kin, and the test doubles' patching (``mock.patch.object``, ``monkeypatch.setattr``).
ATTRIBUTE_CALLS = frozenset({"getattr", "setattr", "hasattr", "delattr", "object"})


def _names_an_attribute(func: ast.expr) -> bool:
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in ATTRIBUTE_CALLS


def _engine_target(node: ast.expr, roots: dict[str, str]) -> str | None:
    """``node`` as text, when it is an engine module: a chain off a bound name, or
    ``import_module("microgrid.engine...")``; else None."""
    chain = _chain(node)
    if chain and chain[0] in roots:
        return ".".join(chain)
    if _imported_module(node) is not None:
        return ast.unparse(node)
    return None


def _imported_module(node: ast.expr) -> str | None:
    """The engine module ``import_module("microgrid.engine...")`` names; else None."""
    if (
        isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "import_module"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.split(".")[0] == "microgrid"
    ):
        return node.args[0].value
    return None


def _argument(call: ast.Call, index: int, *keywords: str) -> ast.expr | None:
    """A call's argument by position, else by any of its keyword names."""
    if len(call.args) > index:
        return call.args[index]
    return next((k.value for k in call.keywords if k.arg in keywords), None)


def _private_uses(tree: ast.AST) -> list[str]:
    """Every way ``tree`` reaches something private in the engine, at any depth: an
    import (any form); an attribute chain off a name bound to an engine module
    (``microgrid.plant._dispatch``, ``me._api``, ``plant._battery`` after
    ``from microgrid import plant``); a private name given as a string to
    ``getattr`` and its kin or a patch (``mock.patch.object``, ``monkeypatch.setattr``)
    on one, or on ``import_module(...)``; or a private dotted name as a string (a
    patch target, ``import_module``). A name built at run time
    (``"microgrid.engine." + "_api"``) is out of its reach."""
    roots: dict[str, str] = {}  # a bound name -> the engine module it names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "microgrid":
                    # ``import microgrid.engine`` binds ``microgrid``.
                    bound = alias.asname or "microgrid"
                    roots[bound] = alias.name if alias.asname else "microgrid"
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.split(".")[0] == "microgrid"
        ):
            for alias in node.names:
                if _engine_module(f"{node.module}.{alias.name}"):
                    roots[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if _private_path(alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _private_path(node.module):
                found.add(f"from {node.module}")
            elif node.module == "microgrid":  # ``from microgrid import _checks``
                found.update(
                    f"from microgrid import {alias.name}"
                    for alias in node.names
                    if _private_path(f"microgrid.{alias.name}")
                )
            elif _engine_parts(node.module) is not None:
                found.update(
                    f"from {node.module} import {alias.name}"
                    for alias in node.names
                    if _private(alias.name)
                )
        elif isinstance(node, ast.Attribute):
            chain = _chain(node)
            if chain and chain[0] in roots:
                dotted = [roots[chain[0]], *chain[1:]]
                if _private_path(".".join(dotted)):
                    found.add(".".join(chain))
            else:
                base: ast.expr = node
                attrs: list[str] = []
                while isinstance(base, ast.Attribute):
                    attrs.insert(0, base.attr)
                    base = base.value
                module = _imported_module(base)
                if module is not None and _private_path(".".join([module, *attrs])):
                    found.add(ast.unparse(node))
        elif isinstance(node, ast.Call) and _names_an_attribute(node.func):
            # ``monkeypatch.setattr(target=, name=)``, ``patch.object(target, attribute=)``
            obj = _argument(node, 0, "target", "obj")
            name = _argument(node, 1, "name", "attribute")
            if (
                obj is not None
                and isinstance(name, ast.Constant)
                and isinstance(name.value, str)
                and _private(name.value)
            ):
                target = _engine_target(obj, roots)
                if target is not None:
                    found.add(f"{ast.unparse(node.func)}({target}, {name.value!r})")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _private_path(node.value)
        ):
            found.add(repr(node.value))
    return sorted(found)


#: The library's public tiers: the modules a client imports.
#: Everything else is private, and a tier's names are its ``__all__`` where it has one.
TIERS = frozenset(
    {
        "microgrid.engine",
        "microgrid.engine.series",
        "microgrid.engine.units",
        "microgrid.plant",
        "microgrid.plant.examples",
        "microgrid.plant.flows",
        "microgrid.plant.port",
        "microgrid.engine.ledger",
        "microgrid.engine.analysis",
        "microgrid.engine.analysis.scoring",
        "microgrid.engine.analysis.ground_truth",
        "microgrid.engine.extend",
        "microgrid.engine.testing",
    }
)


def _tier_offences(tree: ast.Module) -> list[str]:
    """Each engine module ``tree`` imports that is not a tier, and each name it imports
    from a tier that the tier does not export."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _engine_parts(alias.name) is not None and alias.name not in TIERS:
                    found.append(f"{node.lineno}: {alias.name}")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
            and _engine_parts(node.module) is not None
        ):
            if node.module not in TIERS:
                found.append(f"{node.lineno}: {node.module}")
                continue
            exported = getattr(importlib.import_module(node.module), "__all__", None)
            for alias in node.names:
                if f"{node.module}.{alias.name}" in TIERS:
                    continue
                if _private(alias.name) or (exported is not None and alias.name not in exported):
                    found.append(f"{node.lineno}: {node.module}.{alias.name}")
    return found


#: The CLI's package: its modules are ``microgrid.cli`` and below it.
CLI = "microgrid.cli"


def cli_modules() -> list[str]:
    """Every module of the CLI, dotted (packages by their name)."""
    found = []
    for path in sorted(CLI_DIR.rglob("*.py")):
        parts = path.relative_to(CLI_DIR).with_suffix("").parts
        parts = parts[:-1] if parts[-1] == "__init__" else parts
        found.append(".".join([CLI, *parts]))
    return found


def cli_surface(module: str) -> frozenset[str] | None:
    """What ``module`` (a CLI module) declares public: its ``__all__``; None where it
    declares none."""
    exported = getattr(importlib.import_module(module), "__all__", None)
    return None if exported is None else frozenset(exported)


def _cli_offences(tree: ast.Module) -> list[str]:
    """Each name ``tree`` imports from the CLI that its module does not declare public
    (``__all__``), and each CLI module it imports that is not public (a package's
    ``__all__`` names its public modules)."""
    found = []

    def public_module(dotted: str) -> bool:
        parent, _, name = dotted.rpartition(".")
        if not parent.startswith(CLI):
            return dotted == CLI
        surface = cli_surface(parent)
        return surface is not None and name in surface

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if (name == CLI or name.startswith(f"{CLI}.")) and not public_module(name):
                    found.append(f"{node.lineno}: {name}")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
            and (node.module == CLI or node.module.startswith(f"{CLI}."))
        ):
            if not public_module(node.module):
                found.append(f"{node.lineno}: {node.module}")
                continue
            surface = cli_surface(node.module) or frozenset()
            for alias in node.names:
                if alias.name not in surface:
                    found.append(f"{node.lineno}: {node.module}.{alias.name}")
    found.extend(_cli_attribute_offences(tree))
    return found


def _cli_module(dotted: str) -> bool:
    """Whether ``dotted`` is a module of the CLI (the package itself included)."""
    if dotted != CLI and not dotted.startswith(f"{CLI}."):
        return False
    try:
        importlib.import_module(dotted)
    except ImportError:
        return False
    return True


def _cli_attribute_offences(tree: ast.Module) -> list[str]:
    """Each attribute ``tree`` reads off a CLI module that the module does not declare
    public: ``h._x`` after ``from microgrid.cli import history as h``, or
    ``microgrid.cli.history._x`` after ``import microgrid.cli.history``: the import
    rule alone lets these through."""
    roots: dict[str, str] = {}  # a bound name -> the module it names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and _cli_module(alias.name):
                    roots[alias.asname] = alias.name
                elif not alias.asname and alias.name.split(".")[0] == "microgrid":
                    roots["microgrid"] = "microgrid"
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if _cli_module(dotted):
                    roots[alias.asname or alias.name] = dotted
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        chain = _chain(node)
        if not chain or chain[0] not in roots:
            continue
        dotted = roots[chain[0]]
        for part in chain[1:]:
            nested = f"{dotted}.{part}"
            if nested == "microgrid.cli" or (dotted != "microgrid" and _cli_module(nested)):
                dotted = nested
                continue
            if dotted == "microgrid" and part != "cli":
                break  # the engine: its own rule (``_private_uses``)
            if dotted.startswith(CLI) and part not in (cli_surface(dotted) or frozenset()):
                found.add(f"{node.lineno}: {dotted}.{part}")
            break
    return sorted(found)
