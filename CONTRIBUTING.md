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
pytest
```

Tests live in `tests/` and run against the package installed in editable mode. Please add a test alongside any bugfix or new behavior.

## Running locally

```bash
railmux
```

The entry point is `railmux.cli:main`. Source lives under `src/railmux/`.

## Pull requests

- Open an issue first for non-trivial changes so we can agree on the approach before you write code.
- Keep PRs focused — one logical change per PR.
- Make sure `pytest` passes and the TUI still launches cleanly before pushing.
- Commit messages: short imperative subject (e.g. `discovery: handle empty projects dir`); reference the issue in the body when relevant.

## Reporting bugs

File an issue at https://github.com/Rightglow/Railmux/issues with:

- railmux version (`railmux --version` or check `src/railmux/__init__.py`)
- Python version, OS, and tmux version (`tmux -V`)
- Steps to reproduce and what you expected vs. what happened
