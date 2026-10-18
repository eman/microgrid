#!/usr/bin/env bash
# The CI gate, run by GitHub Actions (.github/workflows/ci.yml) and by the
# pre-push hook (scripts/install-hooks.sh installs it), so a green hook predicts
# a green run.
set -euo pipefail
# A hook pushed from a linked worktree runs with GIT_DIR set to that worktree's git dir,
# and a git run under it acts on the repository: uv, fetching a git dependency into its
# cache, runs `git init --bare` there, which made the repository bare. The gate needs
# none of git's variables.
unset $(git rev-parse --local-env-vars)
cd "$(dirname "$0")/.."

step() { printf '\n==> %s\n' "$*"; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

docs() {
  step "docs build (strict)"
  # Not --quiet: it raises the log level past the warnings --strict counts, so a broken
  # link would pass (review).
  NO_MKDOCS_2_WARNING=1 uv run --locked mkdocs build --strict --site-dir "$tmp/site"
}

step "uv sync --locked"
uv sync --locked --quiet

# --docs: a change to docs alone (docs/, Markdown, mkdocs.yml; the pre-push hook decides)
# needs only the docs built.
if [ "${1:-}" = "--docs" ]; then
  docs
  printf '\nlocal CI (docs only): all checks passed\n'
  exit 0
fi

step "ruff check"
uv run --locked ruff check src tests scripts

step "ruff format --check"
uv run --locked ruff format --check src tests scripts

step "mypy (strict)"
uv run --locked mypy

docs

step "pytest"
uv run --locked pytest -m "not live" --cov --cov-report=term

printf '\nlocal CI: all checks passed\n'
