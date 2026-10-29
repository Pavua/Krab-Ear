"""Полные исходные cache suites: разрешены только их 41 явный test thread."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    scratch = Path(sys.argv[1]).resolve()
    assert Path.home() == scratch / "home" and Path.cwd() == scratch
    guards = load("component_guards", ROOT / "tests/translation_components_probe.py")
    # IPCThrottle импортирует только threading/time/typing, без workers или I/O.
    guards.LOCAL_MODULES.add("backend.ipc_throttle")
    start = threading.Thread.start
    violations = guards.install_guards(scratch)
    sys.path.insert(0, str(ROOT))
    targets = {
        "TestTranslationCacheConcurrency.test_concurrent_put_get.<locals>.worker",
        "TestTranslationCacheWave103.test_concurrent_set_thread_safe.<locals>.worker",
        "TestTranslationCacheW938Fsync.test_persist_with_explicit_snapshot_skips_lock.<locals>.hold_lock",
    }
    started = []

    def controlled_start(thread):
        target = thread._target
        if (getattr(target, "__module__", "") != "test_translation_cache"
                or getattr(target, "__qualname__", "") not in targets or len(started) >= 41):
            violations.append("unexpected thread target")
            raise PermissionError("unexpected thread target")
        started.append(thread)
        return start(thread)

    threading.Thread.start = controlled_start
    suite = unittest.TestSuite()
    for name in ("test_translation_cache", "test_translation_cache_caps_w23"):
        module = load(name, ROOT / ("tests/" + name + ".py"))
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    assert suite.countTestCases() == 70, suite.countTestCases()
    try:
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        for thread in started:
            thread.join(timeout=2)
    alive = [thread.name for thread in started if thread.is_alive()]
    receipt = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   skipped=len(result.skipped), violations=violations, threads=threading.active_count(),
                   started_threads=len(started), alive_test_threads=alive, excluded=[],
                   backend_service_imported="backend.service" in sys.modules)
    print("COMPONENT_RECEIPT=" + json.dumps(receipt), flush=True)
    return 0 if (result.wasSuccessful() and not result.skipped and not violations
                 and len(started) == 41 and not alive and threading.active_count() == 1) else 1


if __name__ == "__main__":
    sys.exit(main())
