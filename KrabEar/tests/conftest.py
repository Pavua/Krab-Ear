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


# ---------------------------------------------------------------------------
# Wave 1705: disable the background LLM warmup thread during tests.
#
# BackendService.__init__ spawns a daemon thread running LLMRewriter.warmup_sync
# (60 s timeout). With LM Studio offline — the norm in CI/test environments —
# each of the ~90 BackendService constructions in test_backend_service.py (and
# every other test that builds a BackendService) leaks a thread that keeps
# retrying the connection, spams "LLM warmup failed: ConnectionError", and
# touches the test's already-deleted temp StateStore, raising
# PytestUnhandledThreadExceptionWarning on history.lock. This slows the suite
# and adds CI flakiness.
#
# Patching warmup_sync to a no-op lets the daemon thread start and exit
# instantly — no retries, no spam, no dangling store access. Tests that assert
# real warmup behaviour (test_rewriter_warmup, test_stt_warmup) are skipped via
# the nodeid guard so their coverage is preserved.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _disable_llm_warmup(request):
    if "warmup" in request.node.nodeid.lower():
        yield
        return
    try:
        from unittest.mock import patch
        from backend.llm_rewriter import LLMRewriter

        with patch.object(LLMRewriter, "warmup_sync", lambda self, **kw: None):
            yield
    except Exception:
        yield


# ---------------------------------------------------------------------------
# Wave 1748: suppress startup diagnostics error-bus pushes during tests.
#
# BackendService.__init__ calls startup_diagnostics.run_all_checks() which
# (in CI) pushes startup.stt_model_cache_miss and other startup warnings to
# the service's own _error_bus.  Tests that assert list_recent_errors == []
# on a fresh BackendService fail because the ring buffer is non-empty.
#
# Patching run_all_checks to return a minimal "ready" report keeps the
# diagnostics wiring intact while suppressing the CI-specific warnings.
# Tests that explicitly exercise startup_diagnostics are excluded via nodeid.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _suppress_startup_diagnostics(request):
    """Suppress startup diagnostic error-bus pushes during most tests.

    Also suppresses the LM Studio background reachability check started in
    StartupDiagnostics.__init__.  With ~90 BackendService instances in
    test_backend_service.py, 90 simultaneous background threads firing
    network probes causes intermittent CI failures (resource exhaustion +
    spurious warmup log noise).  Patching to a no-op eliminates the leak.
    """
    if "startup_diagnostics" in request.node.nodeid.lower():
        yield
        return
    try:
        from unittest.mock import patch, MagicMock
        from backend.startup_diagnostics import StartupDiagnostics

        _fake_report = MagicMock()
        _fake_report.status = "ready"
        _fake_report.errors = []
        _fake_report.warnings = []
        _fake_report.startup_time_ms = 0.0
        _fake_report.checks = []

        with patch.object(StartupDiagnostics, "run_all_checks", return_value=_fake_report), \
             patch.object(StartupDiagnostics, "_start_lm_studio_background_check",
                          lambda self: None):
            yield
    except Exception:
        yield


# ---------------------------------------------------------------------------
# Wave 1748: reset global ErrorBus ring-buffer + WarnBatcher state after each
# test.  BackendService wires a per-instance ErrorBus, but the module-level
# singleton (backend.event_bus.bus / backend.error_bus module state) can
# accumulate entries across tests in the same xdist worker.  The ring buffer
# is cleared via .clear() which handles the deque + dedupe dict + WarnBatcher.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_error_bus_singleton():
    """Clear the module-level ErrorBus ring buffer after each test."""
    yield
    try:
        import sys as _sys
        _ebus_mod = _sys.modules.get("backend.error_bus")
        if _ebus_mod is not None:
            _bus_singleton = getattr(_ebus_mod, "bus", None)
            if _bus_singleton is not None and hasattr(_bus_singleton, "clear"):
                try:
                    _bus_singleton.clear()
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Wave 1746: stub-purge backstop fixture.
#
# Many tests install bare ModuleType stubs or MagicMock objects into
# sys.modules to replace heavy optional dependencies (mlx, sounddevice,
# gigaam_worker, etc.) without properly restoring sys.modules afterward.
# In single-process runs this rarely matters — but under pytest-xdist with
# -n 2, workers share the same Python interpreter process across test files,
# so a stub installed by file A leaks into file B and replaces real attributes
# (e.g. sounddevice.InputStream disappears).
#
# This fixture runs AFTER every test and removes:
#   - bare ModuleType stubs (no __file__ / no __spec__.origin)
#   - MagicMock / Mock instances sitting in sys.modules
#   …BUT ONLY for known-leaky namespaces, to avoid accidentally removing
#   real modules that tests legitimately cached.
#
# Package roots (bare "backend", "core", "contracts") are never removed — only
# dotted sub-modules.
#
# Wave 1748: "sounddevice" REMOVED from the never-purge list so that bare
# ModuleType stubs (no InputStream) installed by test_pipeline_v2_gate_W1275.py
# are cleaned up.  Purging the bare stub causes the next import to reload the
# real sounddevice (which IS installed in CI via requirements.txt), restoring
# the InputStream attribute needed by test_recorder.py.
# ---------------------------------------------------------------------------
_STUB_PURGE_PREFIXES = ("backend.", "core.", "contracts.")
_STUB_PURGE_EXTERNAL = frozenset({"websockets", "mlx", "mlx.core", "sentry_sdk"})


@pytest.fixture(autouse=True)
def _purge_leaked_module_stubs():
    """Remove bare-stub and Mock entries from sys.modules after each test."""
    yield
    import types
    from unittest.mock import MagicMock, Mock

    for name in list(sys.modules.keys()):
        # Only process known namespaces.
        if not (
            any(name.startswith(p) for p in _STUB_PURGE_PREFIXES)
            or name in _STUB_PURGE_EXTERNAL
        ):
            continue
        mod = sys.modules.get(name)
        if mod is None:
            continue
        # Real modules have __file__ or a proper __spec__.origin.
        is_bare_stub = (
            isinstance(mod, types.ModuleType)
            and getattr(mod, "__file__", None) is None
            and getattr(getattr(mod, "__spec__", None), "origin", None) in (None, "")
        )
        is_mock = isinstance(mod, (Mock, MagicMock))
        if is_bare_stub or is_mock:
            del sys.modules[name]

    # Wave 1748: also purge bare "sounddevice" stub (was in _STUB_PURGE_EXTERNAL,
    # now handled separately so the logic stays readable).
    _sd = sys.modules.get("sounddevice")
    if _sd is not None:
        import types as _types
        from unittest.mock import Mock as _Mock, MagicMock as _MagicMock
        _is_sd_bare = (
            isinstance(_sd, _types.ModuleType)
            and getattr(_sd, "__file__", None) is None
            and getattr(getattr(_sd, "__spec__", None), "origin", None) in (None, "")
        )
        _is_sd_mock = isinstance(_sd, (_Mock, _MagicMock))
        if _is_sd_bare or _is_sd_mock:
            del sys.modules["sounddevice"]
