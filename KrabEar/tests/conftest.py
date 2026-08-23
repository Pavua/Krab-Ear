"""Общие pytest-фикстуры и сохранение результатов [BENCH] в историю проекта."""
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

import importlib
import importlib.util
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# W957 network guard (добавлено 2026-06-05 из Krab MAIN). Unit-тесты НЕ должны
# выходить в реальную сеть: сетевые тесты без моков (напр. webhook SSRF
# test_webhook_redirect_ssrf_W1355) висели на реальном connect без таймаута →
# процесс копил RAM/потоки часами (S73: 57 ГБ / 2083 потока / 14ч → MacBook
# freeze; повтор 2026-06-05: runaway-loop pytest KrabEar, swap 30 ГБ).
# Guard патчит socket.socket.connect: не-loopback connect → мгновенный
# RuntimeError. Escape: marker `live`/`acceptance` или env
# KRAB_ALLOW_TEST_NETWORK=1 (integration-прогон).
# ---------------------------------------------------------------------------
import os  # noqa: E402
import socket  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from typing import Any  # noqa: E402

# ---------------------------------------------------------------------------
# Изоляция privacy_audit.log (инцидент 2026-08-23). Боевой compliance-журнал
# ~/Library/Application Support/KrabEar/privacy_audit.log набрал 44 907 из
# 50 041 записи тестовым мусором: PrivacyAuditLogger — синглтон с захардкоженным
# home-rooted путём, и 17 из 20 файлов, зовущих handle_purge_all_data, его не
# патчили. Реальный purge владельца стал неотличим от тестового.
#
# Правило CLAUDE.md для КАЖДОГО persistence-пути: env-переменная для базового
# пути + throwaway, принудительно выставленный в conftest ДО импорта приложения.
# Именно ПРИСВАИВАНИЕ, не setdefault: унаследованное из оболочки значение не
# должно побеждать изоляцию.
#
# Под pytest -n auto каждый xdist-воркер — отдельный процесс, импортирует
# conftest сам и получает СВОЙ mkdtemp; гонки за один файл не возникает.
# ---------------------------------------------------------------------------
import atexit  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

_PRIVACY_AUDIT_TMPDIR = tempfile.mkdtemp(prefix="krab_ear_privacy_audit_")
os.environ["KRAB_EAR_PRIVACY_AUDIT_DIR"] = _PRIVACY_AUDIT_TMPDIR
atexit.register(shutil.rmtree, _PRIVACY_AUDIT_TMPDIR, True)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "0.0.0.0", "localhost"})
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect


def _is_loopback_address(address: Any) -> bool:
    """True, если адрес соединения — loopback/localhost или Unix-сокет."""
    if isinstance(address, (str, bytes)):
        return True  # Unix domain socket — всегда локальный
    if isinstance(address, tuple) and address:
        host = address[0]
        if isinstance(host, str):
            return host in _LOOPBACK_HOSTS or host.startswith("127.")
    return True


@pytest.fixture(autouse=True)
def _block_real_network(request: pytest.FixtureRequest) -> Iterator[None]:
    """Блокирует исходящие соединения на не-loopback адреса в unit-тестах (W957)."""
    if os.environ.get("KRAB_ALLOW_TEST_NETWORK") == "1":
        yield
        return
    if request.node.get_closest_marker("live") or request.node.get_closest_marker(
        "acceptance"
    ):
        yield
        return

    def _guarded_connect(self: socket.socket, address: Any) -> Any:
        if _is_loopback_address(address):
            return _ORIGINAL_SOCKET_CONNECT(self, address)
        raise RuntimeError(
            "W957: реальный сетевой вызов заблокирован в unit-тесте "
            f"(connect → {address!r}). Замокай httpx/requests/socket "
            "или пометь @pytest.mark.live / выставь KRAB_ALLOW_TEST_NETWORK=1."
        )

    original = socket.socket.connect
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = original  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def _isolate_privacy_audit_singleton() -> Iterator[None]:
    """Сбрасывает синглтон логгера и чистит throwaway-журнал после теста.

    Два эффекта связаны. Сброс не даёт тесту пронести путь и _last_hash в
    соседний. Удаление файла держит журнал пустым: иначе _read_chain_tip() в
    конструкторе стал бы O(N) по растущему за прогон файлу, и к концу сьюта
    каждое создание инстанса платило бы за десятки тысяч строк.

    Сброс перестал быть УСЛОВИЕМ корректности после того, как соседняя волна
    научила log_event перечитывать tip цепочки из файла под локом: удалённый
    журнал теперь честно даёт None вместо повисшего _last_hash. Остаётся как
    гигиена — тест не должен проносить состояние синглтона в соседний. Ключ
    privacy_audit.key не трогаем: он переиспользуется, генерировать 32 байта
    энтропии на каждый тест незачем.

    Модуль берётся из sys.modules, а не импортируется: если тест его не
    импортировал, создавать инстанс незачем; если тест подменил backend.* стабом,
    getattr-проверки не дадут упасть на чужом объекте.
    """
    yield
    mod = sys.modules.get("backend.privacy_audit")
    if mod is not None:
        cls = getattr(mod, "PrivacyAuditLogger", None)
        reset = getattr(cls, "reset_instance", None)
        if callable(reset):
            reset()
    Path(_PRIVACY_AUDIT_TMPDIR, "privacy_audit.log").unlink(missing_ok=True)


_BENCH_RE = re.compile(r"\[BENCH\]\s+(.+?):\s+([\d.]+)s")
_HISTORY_FILE = Path(__file__).resolve().parents[2] / ".benchmarks" / "history.jsonl"


# ---------------------------------------------------------------------------
# W1759 — macOS-only test gating (CI policy, user decision 2026-05-31).
#
# Krab Ear is a macOS application. MLX (mlx_whisper / mlx.core) is Apple-Silicon
# only; GigaAM runs in a separate macOS venv (pins torch<=2.5.1); the real STT
# adapters (voxtral/parakeet/sensevoice/whisperx) need torch-MPS. On a Linux CI
# runner these families RE-IMPORT C-extension modules — unsafe, can SIGSEGV the
# interpreter and corrupt state, poisoning later files in the same chunk — or hang
# on macOS-only resources. They pass on macOS, and the macOS `CI` workflow runs
# the FULL suite, so coverage is not lost. On a non-darwin runner we therefore do
# not COLLECT (import) these files at all. Intentionally NOT gated: pure-logic
# concurrency tests (test_mlx_lock = RLock, test_mlx_inter_lock = flock) and
# translator cache tests — they are platform-agnostic and must run on Linux.
_MACOS_ONLY_TEST_SUBSTRINGS = (
    "gigaam",
    "audio_lang_id",
    "lang_id_hook",
    "clear_cache_called_after_lid",
    "mx_clear_cache",
    "whisper_mlx",
    "whisperx",
    "voxtral",
    "parakeet",
    "sensevoice",
    "memory_cleanup",
    "mlx_subprocess",
    "mlx_recovery",
    "mlx_concurrency",
    "mlx_thread_safety",
    "mlx_cache_clear",
    "engine_mlx",
    "engine_gigaam",
)


def pytest_ignore_collect(collection_path, config):  # noqa: ARG001
    """Do not collect macOS-only C-extension test families on non-darwin runners.

    Returning True prevents the module from being imported at all (the crash /
    cross-file contamination happens at import + execution, so an item-level skip
    is too late). macOS returns None and collects everything.
    """
    if sys.platform == "darwin":
        return None
    name = getattr(collection_path, "name", str(collection_path))
    if name.startswith("test_") and any(s in name for s in _MACOS_ONLY_TEST_SUBSTRINGS):
        return True
    return None


# ---------------------------------------------------------------------------
# Wave 1751: ANCHOR-RESTORE real heavy external modules BEFORE every test.
#
# Root cause of the recurring "module 'sounddevice' has no attribute
# 'InputStream'" / BackendService-collaborator-None / xdist-worker-crash class
# of failures:
#
#   1. A test file (e.g. test_pipeline_v2_gate_W1275.py) installs a *bare*
#      types.ModuleType stub for "sounddevice" (no InputStream) into
#      sys.modules at MODULE-IMPORT (collection) time — before any test runs.
#   2. backend.recorder does `import sounddevice as sd` at module top-level and
#      caches that reference in its OWN module global `sd`.  If backend.recorder
#      is (re)imported while the bare stub is in sys.modules, `backend.recorder.sd`
#      is now permanently the bare stub — restoring sys.modules["sounddevice"]
#      alone does NOT fix it (the stale module global still points at the stub).
#   3. Any later test that calls `sd.InputStream(...)` (recorder capture loop,
#      BackendService.test_microphone, etc.) raises AttributeError inside a
#      background thread → the test fails *or* the whole xdist worker crashes
#      ("node down: Not properly terminated"), cascading to every other test
#      on that worker (the BackendService "unexpectedly None" symptoms).
#
# The existing _purge_leaked_module_stubs runs AFTER each test (on yield), so a
# victim that runs *before* the polluter's teardown still sees the stub.
#
# Fix: capture the REAL heavy modules once at session import, then BEFORE every
# test (before yield) guarantee the real module is back in sys.modules *and*
# re-point any already-imported module global that aliased it (backend.recorder.sd
# and friends).  Tests that legitimately stub these in their OWN setUp still
# work — setUp runs AFTER this fixture's before-phase.
# ---------------------------------------------------------------------------

# External modules whose *real* implementation must be present before each test.
# Only modules that are actually installed in the environment are anchored; a
# missing module is simply skipped (its absence is the correct state).
#
# IMPORTANT: only anchor modules that are (a) SAFE to keep loaded and (b) subject
# to the module-global stale-binding poison (a top-level `import X as y` in a
# source module that a later test calls).  sounddevice qualifies: backend.recorder
# caches `import sounddevice as sd` and the hardware calls are neutralized
# separately (_neutralize_sounddevice_hardware), so a real sounddevice is safe.
#
# mlx / mlx.core / mlx_whisper are deliberately NOT anchored: MLX is not
# thread/process-safe (concurrent GPU access SIGSEGVs — see CLAUDE.md "MLX
# thread-safety"), so force-loading the *real* MLX into sys.modules before every
# test could trigger a real GPU init / crash under pytest-xdist.  MLX tests stub
# mlx in their own setUp, and the after-each _purge_leaked_module_stubs already
# cleans up mlx / mlx.core stubs.
_ANCHOR_EXTERNAL_NAMES = (
    "sounddevice",
    "soundfile",
)

# Modules that bind a heavy external at *module top-level* into their own global
# namespace (alias -> external module name).  When we restore the real external
# we must also re-point these stale globals, otherwise they keep the leaked stub.
# Format: {already_imported_module_name: {global_attr: external_module_name}}
_ANCHOR_REBIND_GLOBALS = {
    "backend.recorder": {"sd": "sounddevice"},
}


def _capture_real_modules() -> dict:
    """Import and cache the real heavy external modules (those that exist)."""
    real: dict = {}
    for name in _ANCHOR_EXTERNAL_NAMES:
        existing = sys.modules.get(name)
        # If something already imported the real module, trust it only when it
        # is NOT a bare stub / mock (real modules have a __file__ or spec origin).
        if existing is not None and _looks_real(existing):
            real[name] = existing
            continue
        try:
            if importlib.util.find_spec(name) is None:
                continue
        except Exception:
            continue
        try:
            real[name] = importlib.import_module(name)
        except Exception:
            # Native lib (e.g. PortAudio) genuinely unavailable — leave unset so
            # the anchor simply never forces this module.  The real source code
            # already guards `import sounddevice` with try/except.
            pass
    return real


def _looks_real(mod: object) -> bool:
    """Heuristic: a real module has __file__ or a non-empty __spec__.origin."""
    import types as _t
    from unittest.mock import Mock as _Mock, MagicMock as _MagicMock

    if isinstance(mod, (_Mock, _MagicMock)):
        return False
    if not isinstance(mod, _t.ModuleType):
        return False
    if getattr(mod, "__file__", None):
        return True
    origin = getattr(getattr(mod, "__spec__", None), "origin", None)
    return bool(origin) and origin not in ("namespace",)


# Captured once at conftest import (before the bulk of test collection installs
# any stubs — conftest is imported very early by pytest).
_REAL_MODULES: dict = _capture_real_modules()


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
# 2026-08-05: LLMRewriter.ping()/warmup_probe()/summarize() hit real LM Studio
# (loopback 127.0.0.1) via requests.Session — _block_real_network above
# deliberately allows loopback (it would otherwise break legitimate local
# mock servers), so this specific hole stayed open. When LM Studio is
# unreachable/slow, the underlying socket.recv has no timeout the guard
# enforces, and tests hang for minutes instead of failing fast — hit
# repeatedly across unrelated test files (test_backend_service.py,
# test_error_bus_integration.py, test_dispatch_complete.py, and others) on a
# session where LM Studio was down all day. Neither of the two fixtures above
# catches this: _disable_llm_warmup patches warmup_sync (a different method);
# _suppress_startup_diagnostics patches StartupDiagnostics, not
# BackendService._init_llm_rewriter()'s direct rewriter.ping() call.
#
# Scope deliberately does NOT include rewrite()/_rewrite_impl() — that's
# LLMRewriter's primary API with the widest test surface (chatbot guard,
# length-ratio guard, circuit breaker, fallback chain); blanket-patching it
# here risks silently defeating existing per-test requests.Session mocks
# that assert on real call behavior, without a full audit of every
# rewrite-touching test. Only the entry points actually observed hanging
# today are covered — extend deliberately, not preemptively, if a new one
# surfaces.
#
# Exclusion matches the FILE component only (nodeid.split("::")[0]), not a
# whole-nodeid substring — a whole-nodeid check would (and once did, by
# coincidence) also match unrelated test METHODS merely named
# "test_llm_rewriter_..." in a different file, silently exempting them from
# this guard for the wrong reason. A single test living OUTSIDE those files
# but still needing the real implementation (e.g. proving a requests.Session
# mock actually intercepts the call, not decorative) opts out via the
# `llm_network_live` marker instead of another filename special-case.
@pytest.fixture(autouse=True)
def _block_real_lm_studio_calls(request):
    file_stem = Path(request.node.nodeid.split("::")[0]).stem.lower()
    if (
        file_stem.startswith("test_llm_rewriter")
        or file_stem == "test_rewriter_warmup"
        or request.node.get_closest_marker("llm_network_live")
    ):
        yield
        return
    try:
        from unittest.mock import patch
        from backend.llm_rewriter import LLMRewriter, LLMRewriteResult

        with (
            patch.object(LLMRewriter, "ping", lambda self: False),
            patch.object(
                LLMRewriter,
                "warmup_probe",
                lambda self, timeout_sec=None: {
                    "ok": False,
                    "latency_ms": 0,
                    "error": "test_network_blocked",
                },
            ),
            patch.object(
                LLMRewriter,
                "summarize",
                lambda self, text, max_sentences=3: LLMRewriteResult(
                    ok=False, text=None, fallback_reason="test_network_blocked",
                    latency_ms=0,
                ),
            ),
        ):
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
def _anchor_real_heavy_modules():
    """Restore real heavy external modules in sys.modules BEFORE every test.

    Wave 1751 systemic backstop.  See the long comment near _REAL_MODULES.

    Runs in the *before* phase (before yield), so it executes before each test's
    own setUp.  For every heavy external whose real implementation we captured at
    session start, if a stub has leaked into sys.modules we put the real module
    back, and we re-point any stale module global that aliased it at top-level
    import time (e.g. backend.recorder.sd).

    This defeats cross-file stub leaks regardless of pytest-xdist test ordering:
    the real module is guaranteed present before the test body runs, so
    `patch("sounddevice.InputStream", ...)` and `sd.InputStream(...)` both resolve
    against the real module.  Tests that intentionally stub these modules in their
    own setUp/with-block still work — their stub is installed after this fixture's
    before-phase and is cleaned up by _purge_leaked_module_stubs after the test.
    """
    if _REAL_MODULES:
        for name, real_mod in _REAL_MODULES.items():
            current = sys.modules.get(name)
            if current is not real_mod:
                sys.modules[name] = real_mod
        # Re-point stale module globals that cached a now-replaced external.
        for owner_name, attr_map in _ANCHOR_REBIND_GLOBALS.items():
            owner = sys.modules.get(owner_name)
            if owner is None:
                continue
            for attr, ext_name in attr_map.items():
                real_ext = _REAL_MODULES.get(ext_name)
                if real_ext is None:
                    continue
                # Only rebind if the owner currently holds a non-real reference
                # (a leaked stub).  Never clobber a deliberate per-test patch:
                # this runs before setUp, so any current value is either the real
                # module (no-op) or a leaked stub (must fix).
                if getattr(owner, attr, None) is not real_ext:
                    setattr(owner, attr, real_ext)
    yield


# Hardware-level sounddevice functions that block on / probe real PortAudio.
# No unit test should ever exercise these against real hardware: under
# pytest-xdist with concurrent workers on a headless macOS runner, real
# sd.rec()/sd.query_devices()/sd.InputStream() can hang or segfault the worker
# ("node down: Not properly terminated"), cascading to unrelated tests on the
# same worker.  Several test files invoke the IPC methods that reach these
# (test_dispatch_complete.py, BackendServiceInitTestCase, etc.) without
# patching them — so we neutralize them once, centrally, for every test.
_SD_HARDWARE_NEUTRALIZE = ("rec", "wait", "query_devices", "InputStream", "play")


@pytest.fixture(autouse=True)
def _neutralize_sounddevice_hardware():
    """Patch real sounddevice hardware calls to safe no-ops for every test.

    Wave 1751.  Runs after _anchor_real_heavy_modules (so the *real* sounddevice
    module is in place) and wraps the test, restoring the originals afterward.

    A test that needs specific device data simply patches sd.query_devices (or
    the service's _list_audio_inputs) itself — that patch nests on top of this
    one and is restored independently.  If a test replaces the whole sounddevice
    module with its own stub, this fixture's patches sit on the real module
    object and are harmless.
    """
    import types as _t
    from unittest.mock import patch as _patch, MagicMock as _MagicMock

    real_sd = (_REAL_MODULES.get("sounddevice")
               if _REAL_MODULES else None)
    # Only neutralize a genuine, real sounddevice module.
    if real_sd is None or not isinstance(real_sd, _t.ModuleType):
        yield
        return

    import numpy as _np

    _patchers = []
    try:
        for _attr in _SD_HARDWARE_NEUTRALIZE:
            if not hasattr(real_sd, _attr):
                continue
            if _attr == "rec":
                ret = _np.zeros((1, 1), dtype=_np.float32)
                p = _patch.object(real_sd, _attr, return_value=ret)
            elif _attr == "query_devices":
                p = _patch.object(real_sd, _attr, return_value=[])
            elif _attr == "InputStream":
                p = _patch.object(real_sd, _attr, return_value=_MagicMock())
            else:  # wait / play
                p = _patch.object(real_sd, _attr, return_value=None)
            p.start()
            _patchers.append(p)
        yield
    finally:
        for p in reversed(_patchers):
            try:
                p.stop()
            except Exception:
                pass


@pytest.fixture(autouse=True)
def _neutralize_keychain_security():
    """Crypto-audit (2026-06-20): ни один тест не должен трогать РЕАЛЬНЫЙ macOS
    Keychain через `security` CLI.

    handle_purge_all_data теперь вызывает delete_history_key() (ротация ключа
    шифрования при wipe).  Без этой нейтрализации каждый purge-тест на dev-macOS
    удалял бы реальный ключ KrabEar из Keychain → история разработчика с
    включённым шифрованием стала бы недешифруемой.

    Хирургично: патчим subprocess.run глобально, но дивертим ТОЛЬКО команды
    `security` → FileNotFoundError (которую _run_security оборачивает в
    KeystoreUnavailable = «нет Keychain», как на ubuntu CI).  Любая другая
    subprocess-команда проходит к реальному run.  Локальные patch(subprocess.run)
    /patch(_run_security) в тестах самого keystore нестятся ПОВЕРХ и переопределяют
    эту фикстуру, поэтому функцио­нальные тесты keystore не ломаются.
    """
    import subprocess as _sp
    from unittest.mock import patch as _patch
    _real_run = _sp.run

    def _guarded_run(args, *a, **kw):
        if isinstance(args, (list, tuple)) and args and args[0] == "security":
            raise FileNotFoundError("security CLI нейтрализован в тестах (Keychain)")
        return _real_run(args, *a, **kw)

    with _patch("subprocess.run", side_effect=_guarded_run):
        yield


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


@pytest.fixture(autouse=True)
def _memory_ledger_tmp_path(tmp_path):
    """🔴 Финальный гейт волны Memory Conductor (HIGH-3): без подмены каждый тест
    с BackendService писал/стирал записи в РЕАЛЬНОМ ~/.openclaw/memory_ledger.json
    владельца (conductor.stop() -> remove_own()). Обратимо в finally."""
    from backend import memory_ledger as _ml
    prev = _ml._TEST_PATH_OVERRIDE
    _ml._TEST_PATH_OVERRIDE = tmp_path
    try:
        yield
    finally:
        _ml._TEST_PATH_OVERRIDE = prev
