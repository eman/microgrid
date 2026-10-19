# Install

**Requirements:** Python 3.14 or later. The library has no other runtime dependencies.

## Into your own project

With [uv](https://docs.astral.sh/uv/):

```sh
uv add microgrid
```

With pip:

```sh
pip install microgrid
```

Or list it in your `pyproject.toml`:

```toml
[project]
requires-python = ">=3.14"
dependencies = ["microgrid ~= 0.1.0"]
```

Before 1.0 a minor release may change a format, the API or the model, so pin the minor
release you tested with ([Versions](reference/versions.md)).

This installs one distribution, `microgrid`, with three packages and one command:

| | What |
|---|---|
| `microgrid.plant` | the plant model |
| `microgrid.engine` | the prices: import it to value your plant from Python |
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
uv add "microgrid[sources]"
pip install "microgrid[sources]"
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

Next: [Run your first valuation](how-to/first-valuation.md).
