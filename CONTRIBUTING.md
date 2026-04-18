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

## Testing

Run the full test suite from the repo root:

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/ -v
```

**pytest tmp_path disk usage:** `pytest.ini` configures `tmp_path_retention_policy = failed` and `tmp_path_retention_count = 1`, so pytest keeps temp files only from the last failed run and discards everything else. Without this, pytest accumulates audio files (wav, m4a) across runs and the directory `/private/var/folders/.../pytest-of-<user>/` can grow to 40+ GB. If you need longer retention for debugging a specific test, pass `--basetemp=/tmp/my-debug-run` to override.

## Development

See `CLAUDE.md` for project architecture, testing, and build instructions.
