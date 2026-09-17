# 1. Use uv for package management

Date: 2026-09-17

## Status

Accepted

## Context

`ragbridge` needs a way to manage the Python version, dependencies, and the virtual
environment for a project that will grow over several phases (web framework, database
drivers, LLM libraries, agent frameworks). The classic PHP-world equivalent is Composer:
one tool, one lock file, one command to install everything.

In the Python ecosystem there is no single default tool. Common options are:

- `pip` + `requirements.txt`: the oldest approach. No built-in lock file, no built-in
  virtual environment management, slow dependency resolution.
- `poetry`: popular, has a lock file and dependency groups, but is slower than newer
  tools and mixes packaging concepts with project management in ways that can be
  confusing for a src-layout service.
- `uv`: a newer tool from Astral (also known for `ruff`), written in Rust. It manages
  the Python version, the virtual environment, dependencies, and a lock file, all
  through one CLI and one `pyproject.toml`.

## Decision

Use `uv` for everything related to Python versions, dependencies, and running project
commands (`uv run ...`). Do not use plain `pip` or a `requirements.txt` file.

## Consequences

- One tool covers what Composer covers in PHP: install dependencies, lock exact
  versions (`uv.lock`, comparable to `composer.lock`), and run scripts.
- `uv sync --locked` in CI guarantees the same dependency versions as local
  development, the same way `composer install` with a committed `composer.lock` does.
- Because `uv` is newer than `pip` or `poetry`, it has a smaller community and fewer
  Stack Overflow answers. This is an accepted trade-off for the speed and simplicity
  it gives day to day.
