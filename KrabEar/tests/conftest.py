"""pytest conftest: captures [BENCH] output and appends to .benchmarks/history.jsonl."""
from __future__ import annotations

# Wave 58 ext CI fix: pre-import numpy.exceptions to dodge an infinite
# recursion bug in numpy.__getattr__ that surfaces under pytest-xdist (-n auto)
# when several worker processes import numpy concurrently. Without this,
# `np.testing.assert_array_equal(...)` fails on Python 3.12 with
# RecursionError: maximum recursion depth exceeded.
# Anchoring numpy.exceptions in sys.modules BEFORE any test imports prevents
# the lazy-load loop in numpy/__init__.py:730 __getattr__.
import numpy  # noqa: F401,E402
import numpy.exceptions  # noqa: F401,E402

import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_BENCH_RE = re.compile(r"\[BENCH\]\s+(.+?):\s+([\d.]+)s")
_HISTORY_FILE = Path(__file__).resolve().parents[2] / ".benchmarks" / "history.jsonl"


def _git_commit() -> str:
    """Return HEAD commit SHA (7 chars) or 'unknown'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _append_entry(entry: dict) -> None:
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _HISTORY_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _extract_bench_pairs(text: str) -> list[tuple[str, float]]:
    """Parse all [BENCH] name: Xs lines from captured stdout."""
    pairs = []
    for line in text.splitlines():
        m = _BENCH_RE.search(line)
        if m:
            pairs.append((m.group(1).strip(), float(m.group(2))))
    return pairs


def pytest_sessionfinish(session: pytest.Session, exitstatus: object) -> None:
    """Session-end backstop: reap any orphaned MLX/GigaAM subprocess workers.

    This is an xdist-safe safety net — each xdist worker calls this at the end
    of its own session, after all its tests have finished.  The primary cleanup
    is the per-test ``addCleanup`` registered in subprocess-spawning tests; this
    hook is a last-resort backstop in case a test crashes before cleanup runs.

    The pkill pattern matches the gigaam_worker.py launch cmdline:
        .../venv_gigaam/bin/python -u .../gigaam_worker.py
    Shell-script stubs spawned by test_runtime_self_redirect.py exit or are
    killed by their own finally blocks; they do not match this pattern.
    """
    import subprocess as _sp

    for pattern in ("gigaam_worker.py", "import sys;ex"):
        try:
            _sp.run(
                ["pkill", "-9", "-f", pattern],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_logreport(report: pytest.TestReport) -> None:  # type: ignore[override]
    """Save [BENCH] results from test stdout into .benchmarks/history.jsonl."""
    yield
    if report.when != "call":
        return
    for section_name, content in report.sections:
        if "stdout" not in section_name.lower():
            continue
        pairs = _extract_bench_pairs(content)
        if not pairs:
            continue
        commit = _git_commit()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
        os_name = platform.system().lower()
        for bench_name, elapsed_sec in pairs:
            entry = {
                "ts": ts,
                "commit": commit,
                "bench_name": bench_name,
                "elapsed_sec": elapsed_sec,
                "test_node_id": getattr(report, "nodeid", ""),
                "os": os_name,
                "python": py_ver,
            }
            _append_entry(entry)
