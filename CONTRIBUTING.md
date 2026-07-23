# Contributing to railmux

Thanks for your interest in railmux. Issues and pull requests are welcome.

## Dev setup

railmux targets Python 3.9+ and requires `tmux` on `PATH`.

```bash
git clone https://github.com/Rightglow/Railmux
cd railmux
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
ruff check src tests tools
pytest -q
python -m build
twine check dist/*
python tools/wheel_smoke.py dist/*.whl
```

Tests live in `tests/` and run against the package installed in editable mode. Please add a test alongside any bugfix or new behavior.

The real-tmux smoke test is opt-in and always uses a private tmux socket:

```bash
RAILMUX_RUN_TMUX_INTEGRATION=1 pytest -q tests/test_tmux_integration.py
```

## Running locally

```bash
railmux
```

The entry point is `railmux.cli:main`. Source lives under `src/railmux/`.

## Architecture and design notes

Before changing providers, recovery state, session indexing, tmux pane
ownership, preview/restore behavior, or multi-agent layout, read the relevant
entry in [`docs/README.md`](docs/README.md). Keep durable invariants and runtime
evidence there; completed task prompts and generated diffs should not become
parallel sources of truth.

Keep the root [`README.md`](README.md) focused on installing and using Railmux.
Implementation rationale, recovery rules, compatibility boundaries, and other
maintainer-only detail belong under [`docs/`](docs/README.md).

## Pull requests

- Open an issue first for non-trivial changes so we can agree on the approach before you write code.
- Keep PRs focused — one logical change per PR.
- Make sure Ruff, pytest, package validation, and a local TUI smoke check pass
  before pushing.
- Commit messages: short imperative subject (e.g. `discovery: handle empty projects dir`); reference the issue in the body when relevant.

## Reporting bugs

File an issue at https://github.com/Rightglow/Railmux/issues with:

- output from `railmux doctor` (designed to omit private environment data)
- Steps to reproduce and what you expected vs. what happened
