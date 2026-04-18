# Contributing to Krab Ear

## Pre-commit Setup

To prevent CI failures from linting issues (F401 unused imports, W293 trailing whitespace), install the pre-commit hook framework:

```bash
pip install pre-commit
pre-commit install
```

This sets up git hooks that run `flake8`, `trailing-whitespace`, and other checks automatically before each commit. Hooks are configured in `.pre-commit-config.yaml`.

### Manual hook runs

Run all hooks on all files:
```bash
pre-commit run --all-files
```

Or just on staged files:
```bash
pre-commit run
```

## Development

See `CLAUDE.md` for project architecture, testing, and build instructions.
