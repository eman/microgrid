# Install

microgrid is **pre-release**: its formats are versioned (`plant/2`, `engine/2`,
`dataset/1`, `sources/1`, `valuation/2`), but it is not yet published to PyPI. Install it from its git
repository.

**Requirements:** Python 3.14 or later. The library has no other runtime dependencies.

## Into your own project

With [uv](https://docs.astral.sh/uv/):

```sh
uv add git+https://github.com/eman/microgrid.git
```

With pip:

```sh
pip install "git+https://github.com/eman/microgrid.git"
```

Or list it in your `pyproject.toml`:

```toml
[project]
requires-python = ">=3.14"
dependencies = ["microgrid @ git+https://github.com/eman/microgrid.git"]
```

No extras are needed for the engine. Pin a commit (`...microgrid.git@<commit>`) while
the library is pre-release: a format can still change between versions, and its version
number says when.

This installs one distribution, `microgrid`, with two packages and one command:

| | What |
|---|---|
| `microgrid.engine` | the library: import it to value your plant from Python |
| `microgrid.cli` | the command's code, and the `dataset/1` reader and writer |
| `microgrid` | the command: the library run on a dataset of files |

```sh
microgrid --help
```

## The `sources` extra

`microgrid now`, `microgrid sources` and `microgrid fetch` read a
[`sources/1`](configuration/data-sources.md) file from live providers. They need two more
packages (`httpx`, `pydantic`), in the `sources` extra; nothing else does:

```sh
uv add "microgrid[sources] @ git+https://github.com/eman/microgrid.git"
pip install "microgrid[sources] @ git+https://github.com/eman/microgrid.git"
```

Without it, those commands say which extra to install.

## A checkout, to try the example or work on it

```sh
git clone https://github.com/eman/microgrid.git
cd microgrid
uv sync
uv run microgrid value --data examples/dataset --at 2026-10-22T13:00
```

`uv sync` installs the library, the command, the `sources` extra's packages and the
development tools into `.venv`.
`uv run mkdocs serve` serves these docs locally.

Next: [Run your first valuation](how-to/first-valuation.md).
