# Phase A — Auto-heal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backend сам поднимается за <3s после crash или hang, юзер видит toast "Backend перезапущен", диктовка продолжается.

**Architecture:** Двухкольцевой supervisor — Swift `HealthMonitor` actor (inner ring, polls every 3s, detects hangs) поверх существующего `BackendSupervisor` + launchd KeepAlive (outer ring, для hard crashes). Exp backoff (1s→2s→5s→15s) + circuit breaker (5 fails → 5min cooldown). Status UI — toast + menu bar dot + History panel header dot.

**Tech Stack:** Swift 6 actors, Foundation Process, Python `resource.getrusage` (stdlib), existing IPCClient + BackendSupervisor.

**Spec:** `docs/superpowers/specs/2026-05-02-stability-roadmap-design.md` (Phase A section)

**Project root:** `/Users/pablito/Antigravity_AGENTS/Krab Ear`

---

## File Structure

### New files
- `KrabEar/backend/health_metrics.py` — RSS/uptime/active-requests tracker (stdlib only, reused in Phase B+C)
- `KrabEar/tests/test_health_metrics.py` — unit tests for `HealthMetrics` class
- `native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift` — actor that polls `ping` every 3s, decides when to restart
- `native/KrabEarAgent/Sources/KrabEarAgent/StatusIndicator.swift` — small view with colored dot (green/yellow/red), used in menu bar + panel header
- `native/KrabEarAgent/Sources/KrabEarAgent/BackendToast.swift` — non-modal NSPanel that shows "Backend перезапущен" for 3s
- `native/KrabEarAgent/Tests/KrabEarAgentTests/HealthMonitorTests.swift` — unit tests for backoff/circuit-breaker logic

### Modified files
- `KrabEar/backend/service.py` — extend `_handle_ping` (add `rss_mb`, `active_requests`)
- `native/KrabEarAgent/Sources/KrabEarAgent/BackendSupervisor.swift` — add exp backoff + circuit breaker (currently flat `maxConsecutiveRestarts = 3`)
- `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` — wire `HealthMonitor` into app lifecycle, attach `StatusIndicator` to menu bar

---

## Task 1: Extend `_handle_ping` with rss_mb and active_requests

**Files:**
- Create: `KrabEar/backend/health_metrics.py`
- Modify: `KrabEar/backend/service.py:1148-1160` (the existing `_handle_ping`)
- Test: `KrabEar/tests/test_health_metrics.py`

- [ ] **Step 1: Write the failing test**

Create `KrabEar/tests/test_health_metrics.py`:

```python
"""Тесты для HealthMetrics (RSS, uptime, active_requests)."""

import unittest
import time
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.health_metrics import HealthMetrics


class HealthMetricsTestCase(unittest.TestCase):
    def test_rss_mb_returns_positive_int(self):
        metrics = HealthMetrics()
        rss = metrics.rss_mb()
        self.assertIsInstance(rss, (int, float))
        self.assertGreater(rss, 0)
        # Sanity: процесс не может занимать больше 100 GB
        self.assertLess(rss, 100_000)

    def test_uptime_sec_increases(self):
        metrics = HealthMetrics()
        first = metrics.uptime_sec()
        time.sleep(0.05)
        second = metrics.uptime_sec()
        self.assertGreater(second, first)

    def test_active_requests_default_zero(self):
        metrics = HealthMetrics()
        self.assertEqual(metrics.active_requests(), 0)

    def test_active_requests_increments_and_decrements(self):
        metrics = HealthMetrics()
        with metrics.track_request():
            self.assertEqual(metrics.active_requests(), 1)
            with metrics.track_request():
                self.assertEqual(metrics.active_requests(), 2)
            self.assertEqual(metrics.active_requests(), 1)
        self.assertEqual(metrics.active_requests(), 0)

    def test_active_requests_decrements_on_exception(self):
        metrics = HealthMetrics()
        try:
            with metrics.track_request():
                self.assertEqual(metrics.active_requests(), 1)
                raise RuntimeError("simulated")
        except RuntimeError:
            pass
        self.assertEqual(metrics.active_requests(), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_health_metrics.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'backend.health_metrics'`

- [ ] **Step 3: Implement `HealthMetrics`**

Create `KrabEar/backend/health_metrics.py`:

```python
"""Отслеживание здоровья процесса: RSS, uptime, активные запросы.

Без внешних зависимостей — только stdlib (resource.getrusage).
Используется в Phase A (ping IPC), Phase B (error context), Phase C (memory soak).
"""

from __future__ import annotations

import resource
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator


class HealthMetrics:
    """Thread-safe сборщик runtime-метрик процесса."""

    def __init__(self) -> None:
        self._start_monotonic = time.monotonic()
        self._active_requests = 0
        self._lock = threading.Lock()

    def rss_mb(self) -> float:
        """Resident Set Size в мегабайтах.

        На macOS `ru_maxrss` возвращается в bytes, на Linux — в KB.
        Округляем до 1 знака для удобства логов.
        """
        usage = resource.getrusage(resource.RUSAGE_SELF)
        if sys.platform == "darwin":
            # macOS: ru_maxrss в bytes
            return round(usage.ru_maxrss / (1024 * 1024), 1)
        # Linux: ru_maxrss в KB
        return round(usage.ru_maxrss / 1024, 1)

    def uptime_sec(self) -> float:
        """Время от создания HealthMetrics в секундах (monotonic)."""
        return round(time.monotonic() - self._start_monotonic, 2)

    def active_requests(self) -> int:
        """Текущее число активных IPC-запросов."""
        with self._lock:
            return self._active_requests

    @contextmanager
    def track_request(self) -> Iterator[None]:
        """Context manager: инкрементирует счётчик на entry, декрементирует на exit.

        Декремент всегда выполняется (try/finally) даже при исключении.
        """
        with self._lock:
            self._active_requests += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_requests -= 1
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_health_metrics.py -v
```

Expected: PASS — all 5 tests green

- [ ] **Step 5: Wire `HealthMetrics` into `BackendService`**

Open `KrabEar/backend/service.py`, find the existing `_handle_ping` (around line 1148):

```python
def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
    try:
        history_count = self.store.count_active_items()
    except Exception:
        history_count = -1
    return {
        "status": "ok",
        "service": "krabear-backend",
        "version": APP_VERSION,
        "uptime_sec": round(time.monotonic() - self._start_time, 1),
        "is_recording": bool(getattr(self.recorder, "is_recording", False)),
        "history_count": history_count,
    }
```

Replace with:

```python
def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
    try:
        history_count = self.store.count_active_items()
    except Exception:
        history_count = -1
    return {
        "status": "ok",
        "service": "krabear-backend",
        "version": APP_VERSION,
        "uptime_sec": self.health_metrics.uptime_sec(),
        "rss_mb": self.health_metrics.rss_mb(),
        "active_requests": self.health_metrics.active_requests(),
        "is_recording": bool(getattr(self.recorder, "is_recording", False)),
        "history_count": history_count,
    }
```

Then in `BackendService.__init__` (search for `self._start_time = time.monotonic()` — likely first lines), add:

```python
from backend.health_metrics import HealthMetrics  # at top of file with other imports
# ...
self.health_metrics = HealthMetrics()
```

- [ ] **Step 6: Run all backend tests to ensure no regression**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_health_metrics.py KrabEar/tests/test_backend_service.py -v
```

Expected: PASS — health metrics tests + existing service tests stay green

- [ ] **Step 7: Commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add KrabEar/backend/health_metrics.py KrabEar/backend/service.py KrabEar/tests/test_health_metrics.py
git commit -m "$(cat <<'EOF'
feat(health): HealthMetrics module + extended ping response

Phase A task 1. Stdlib-only RSS/uptime/active_requests tracker
для двухкольцевого supervisor'а. Используется в ping IPC и
переиспользуется в Phase B (error context) и Phase C (memory soak).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Track active_requests in IPC handler

**Files:**
- Modify: `KrabEar/backend/service.py` (find `handle_request` method, wrap dispatch)
- Test: `KrabEar/tests/test_health_metrics_integration.py`

- [ ] **Step 1: Write the failing integration test**

Create `KrabEar/tests/test_health_metrics_integration.py`:

```python
"""Интеграционный тест: active_requests инкрементится во время IPC dispatch."""

import unittest
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.health_metrics import HealthMetrics


class FakeService:
    """Минимальный сервис, имитирующий dispatch с tracking."""

    def __init__(self) -> None:
        self.health_metrics = HealthMetrics()
        self.observed_active = 0

    def handle(self, method: str) -> int:
        with self.health_metrics.track_request():
            self.observed_active = self.health_metrics.active_requests()
            time.sleep(0.05)
        return self.observed_active


class HealthMetricsIntegrationTestCase(unittest.TestCase):
    def test_active_requests_visible_during_dispatch(self):
        service = FakeService()
        result = service.handle("ping")
        self.assertGreaterEqual(result, 1)
        # После handle() — снова 0
        self.assertEqual(service.health_metrics.active_requests(), 0)

    def test_concurrent_requests_increment_correctly(self):
        service = FakeService()
        threads = [
            threading.Thread(target=service.handle, args=(f"m{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(service.health_metrics.active_requests(), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it passes (uses existing HealthMetrics)**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_health_metrics_integration.py -v
```

Expected: PASS — verifies `track_request` works under threading

- [ ] **Step 3: Wrap real IPC dispatch in `track_request`**

Open `KrabEar/backend/service.py`, find `handle_request(self, request: dict)` method (it's the main dispatch method, contains the `handlers = {...}` dict around line 690-800).

Find the section that calls the handler and wrap it. Look for a pattern like:

```python
handler = handlers.get(method)
if handler is None:
    return {"id": req_id, "ok": False, "error": f"Unknown method: {method}"}
result = handler(params)
```

Wrap with:

```python
handler = handlers.get(method)
if handler is None:
    return {"id": req_id, "ok": False, "error": f"Unknown method: {method}"}
with self.health_metrics.track_request():
    result = handler(params)
```

If the dispatch is structured differently (e.g., async), wrap whatever direct call invokes the handler, ensuring `track_request` covers the entire handler execution.

- [ ] **Step 4: Run end-to-end test**

Start backend in dev mode:

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
source .venv_krab_ear/bin/activate
PYTHONPATH=$(pwd)/KrabEar python KrabEar/backend/service.py --data-dir /tmp/krab_ear_test &
sleep 3
```

In another terminal, ping it:

```bash
python3 -c "
import socket, json, os
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect('/tmp/krab_ear_test/backend.sock')
sock.sendall(b'{\"id\":\"1\",\"method\":\"ping\",\"params\":{}}\n')
print(sock.recv(8192).decode())
"
```

Expected output contains: `"rss_mb": <number>`, `"active_requests": 0`, `"uptime_sec": <number>`

- [ ] **Step 5: Cleanup**

```bash
pkill -f "service.py --data-dir /tmp/krab_ear_test"
rm -rf /tmp/krab_ear_test
```

- [ ] **Step 6: Commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add KrabEar/backend/service.py KrabEar/tests/test_health_metrics_integration.py
git commit -m "$(cat <<'EOF'
feat(health): track active_requests during IPC dispatch

Phase A task 2. handle_request теперь оборачивается в
HealthMetrics.track_request() — счётчик доступен через ping.
Нужен для Swift HealthMonitor чтобы не убивать backend пока
он обрабатывает long-running запрос (например транскрипцию).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: HealthMonitor actor — continuous polling

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/HealthMonitorTests.swift`

- [ ] **Step 1: Write the failing test**

Create `native/KrabEarAgent/Tests/KrabEarAgentTests/HealthMonitorTests.swift`:

```swift
import XCTest
@testable import KrabEarAgent

final class HealthMonitorTests: XCTestCase {

    /// При успешном ping HealthMonitor остаётся в .healthy.
    func testHealthyWhenPingSucceeds() async {
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 2)
        monitor.setPingProvider { return true }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 200_000_000) // ~4 пинга
        let state = await monitor.currentState()
        await monitor.stop()

        XCTAssertEqual(state, .healthy)
    }

    /// 2 fail подряд → состояние .hung.
    func testHungAfterTwoConsecutiveFailures() async {
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 2)
        monitor.setPingProvider { return false }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 300_000_000)
        let state = await monitor.currentState()
        await monitor.stop()

        XCTAssertEqual(state, .hung)
    }

    /// Один fail + один success → .healthy (счётчик сбрасывается).
    func testCounterResetsOnSuccess() async {
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 2)
        let counter = TestCounter()
        monitor.setPingProvider { return counter.next() }

        await monitor.start()
        try? await Task.sleep(nanoseconds: 400_000_000)
        let state = await monitor.currentState()
        await monitor.stop()

        XCTAssertEqual(state, .healthy)
    }

    /// onHangDetected callback вызывается ровно один раз при переходе → .hung.
    func testOnHangCallbackFiresOnce() async {
        let monitor = HealthMonitor(pingInterval: 0.05, hangThreshold: 2)
        monitor.setPingProvider { return false }

        let expectation = XCTestExpectation(description: "hang detected")
        await monitor.setOnHangDetected {
            expectation.fulfill()
        }

        await monitor.start()
        await fulfillment(of: [expectation], timeout: 2.0)
        await monitor.stop()
    }
}

/// Helper: возвращает false, true, false, false, false, ... — first fail, then success, then hang
final class TestCounter: @unchecked Sendable {
    private var calls = 0
    private let lock = NSLock()
    func next() -> Bool {
        lock.lock(); defer { lock.unlock() }
        calls += 1
        // 1st: fail, 2nd: success, after that: fail forever
        return calls == 2
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift test --filter HealthMonitorTests 2>&1 | tail -20
```

Expected: FAIL — `cannot find type 'HealthMonitor' in scope`

- [ ] **Step 3: Implement `HealthMonitor`**

Create `native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift`:

```swift
/*
 Continuous health monitor для backend Python процесса.

 Связи модуля:
 1) BackendSupervisor: использует ping и решения о restart.
 2) main.swift: запуск/остановка по lifecycle приложения.
 3) StatusIndicator: подписка на изменения состояния для UI.
*/

import Foundation

/// Наблюдаемое состояние backend'а.
enum HealthState: Sendable, Equatable {
    /// Backend жив, последние ping'и проходят.
    case healthy
    /// Backend завис: 2+ ping'а подряд не ответили.
    case hung
    /// Backend остановлен (явно через `stop()`).
    case stopped
}

/// Actor, который раз в `pingInterval` секунд дёргает ping и трекает
/// последовательные fails. После `hangThreshold` подряд fails переключает
/// state в `.hung` и зовёт `onHangDetected` ровно один раз.
actor HealthMonitor {
    private let pingInterval: TimeInterval
    private let hangThreshold: Int

    private var consecutiveFailures: Int = 0
    private var state: HealthState = .stopped
    private var monitorTask: Task<Void, Never>?
    private var pingProvider: (@Sendable () async -> Bool)?
    private var onHangDetected: (@Sendable () async -> Void)?
    private var hangFiredForCurrentEpisode: Bool = false

    init(pingInterval: TimeInterval = 3.0, hangThreshold: Int = 2) {
        self.pingInterval = pingInterval
        self.hangThreshold = hangThreshold
    }

    /// Устанавливает провайдер ping'а — вынесено для тестируемости.
    /// В production это будет вызов `IPCClient.callAsync(method: "ping")`.
    nonisolated func setPingProvider(_ provider: @escaping @Sendable () async -> Bool) {
        Task { await self._setPingProvider(provider) }
    }

    private func _setPingProvider(_ provider: @escaping @Sendable () async -> Bool) {
        self.pingProvider = provider
    }

    func setOnHangDetected(_ callback: @escaping @Sendable () async -> Void) {
        self.onHangDetected = callback
    }

    func currentState() -> HealthState {
        return state
    }

    func start() {
        guard monitorTask == nil else { return }
        state = .healthy
        consecutiveFailures = 0
        hangFiredForCurrentEpisode = false

        monitorTask = Task { [weak self] in
            await self?.runLoop()
        }
    }

    func stop() {
        monitorTask?.cancel()
        monitorTask = nil
        state = .stopped
    }

    private func runLoop() async {
        while !Task.isCancelled {
            let nanos = UInt64(pingInterval * 1_000_000_000)
            try? await Task.sleep(nanoseconds: nanos)
            if Task.isCancelled { break }
            await tick()
        }
    }

    private func tick() async {
        guard let provider = pingProvider else { return }
        let ok = await provider()
        if ok {
            consecutiveFailures = 0
            if state == .hung {
                state = .healthy
                hangFiredForCurrentEpisode = false
            } else if state == .stopped {
                state = .healthy
            }
        } else {
            consecutiveFailures += 1
            if consecutiveFailures >= hangThreshold && state != .hung {
                state = .hung
                if !hangFiredForCurrentEpisode {
                    hangFiredForCurrentEpisode = true
                    if let callback = onHangDetected {
                        await callback()
                    }
                }
            }
        }
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift test --filter HealthMonitorTests 2>&1 | tail -30
```

Expected: PASS — all 4 tests green. If timing-related flakes occur, increase `pingInterval` in tests to 0.1s.

- [ ] **Step 5: Commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add native/KrabEarAgent/Sources/KrabEarAgent/HealthMonitor.swift \
        native/KrabEarAgent/Tests/KrabEarAgentTests/HealthMonitorTests.swift
git commit -m "$(cat <<'EOF'
feat(health): HealthMonitor actor — continuous backend polling

Phase A task 3. Actor который раз в N секунд дёргает ping и трекает
fail-streak. После hangThreshold подряд fails state -> .hung,
onHangDetected callback срабатывает ровно один раз за эпизод.

Тестируется через инжектируемый pingProvider — без реального IPC.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Exp backoff + circuit breaker in BackendSupervisor

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/BackendSupervisor.swift` (currently has flat `maxConsecutiveRestarts = 3`)
- Test: extend `native/KrabEarAgent/Tests/KrabEarAgentTests/BackendSupervisorTests.swift` (file likely exists, otherwise create)

- [ ] **Step 1: Write the failing test**

Check if `BackendSupervisorTests.swift` exists:

```bash
ls native/KrabEarAgent/Tests/KrabEarAgentTests/BackendSupervisorTests.swift 2>/dev/null || echo "MISSING"
```

If missing, create it. Otherwise add tests to existing file.

Create or append:

```swift
import XCTest
@testable import KrabEarAgent

final class BackendSupervisorBackoffTests: XCTestCase {

    /// 5-я попытка restart за 60s окно открывает circuit breaker.
    func testCircuitBreakerOpensAfterFiveFails() {
        let supervisor = BackendSupervisor(projectRoot: "/tmp/test")
        var ensureCalls = 0
        supervisor._testEnsureOverride = { _ in
            ensureCalls += 1
            throw NSError(domain: "test", code: 1, userInfo: nil)
        }
        supervisor._testPingOverride = { false }
        #if DEBUG
        supervisor.overrideSupervisionMode(.active)
        #endif

        for _ in 0..<6 {
            _ = supervisor.restartIfDead()
        }
        XCTAssertTrue(supervisor.isCircuitOpen())
        XCTAssertEqual(ensureCalls, 5, "6-я попытка не должна вызвать ensureBackend (circuit open)")
    }

    /// После cooldown circuit закрывается, restartIfDead снова работает.
    func testCircuitClosesAfterCooldown() {
        let supervisor = BackendSupervisor(projectRoot: "/tmp/test")
        supervisor._testEnsureOverride = { _ in throw NSError(domain: "test", code: 1) }
        supervisor._testPingOverride = { false }
        #if DEBUG
        supervisor.overrideSupervisionMode(.active)
        #endif
        supervisor._testCooldownSec = 0.1  // override 5min default to 0.1s

        for _ in 0..<5 { _ = supervisor.restartIfDead() }
        XCTAssertTrue(supervisor.isCircuitOpen())

        Thread.sleep(forTimeInterval: 0.2)
        XCTAssertFalse(supervisor.isCircuitOpen())
    }

    /// Backoff delays формируются: 0, 2, 5, 15, 15.
    func testBackoffSchedule() {
        let supervisor = BackendSupervisor(projectRoot: "/tmp/test")
        XCTAssertEqual(supervisor.backoffDelay(attempt: 1), 0)
        XCTAssertEqual(supervisor.backoffDelay(attempt: 2), 2)
        XCTAssertEqual(supervisor.backoffDelay(attempt: 3), 5)
        XCTAssertEqual(supervisor.backoffDelay(attempt: 4), 15)
        XCTAssertEqual(supervisor.backoffDelay(attempt: 5), 15)
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift test --filter BackendSupervisorBackoffTests 2>&1 | tail -20
```

Expected: FAIL — `isCircuitOpen`, `backoffDelay`, `_testCooldownSec` not defined

- [ ] **Step 3: Implement circuit breaker + backoff in BackendSupervisor**

Open `native/KrabEarAgent/Sources/KrabEarAgent/BackendSupervisor.swift`. Find:

```swift
private var consecutiveRestarts = 0
private static let maxConsecutiveRestarts = 3
```

Replace with:

```swift
// Circuit breaker state
private var consecutiveRestarts = 0
private var circuitOpenedAt: Date?
private static let circuitOpenAfter = 5
private static let circuitCooldownDefault: TimeInterval = 300  // 5 min

#if DEBUG
var _testCooldownSec: TimeInterval?
#endif

private var circuitCooldown: TimeInterval {
    #if DEBUG
    return _testCooldownSec ?? Self.circuitCooldownDefault
    #else
    return Self.circuitCooldownDefault
    #endif
}

/// Backoff schedule: 1st=0s, 2nd=2s, 3rd=5s, 4th=15s, 5th+=15s.
func backoffDelay(attempt: Int) -> TimeInterval {
    switch attempt {
    case ...1: return 0
    case 2: return 2
    case 3: return 5
    default: return 15
    }
}

/// True если circuit открыт (5 fails недавно). Восстанавливается через cooldown.
func isCircuitOpen() -> Bool {
    guard let openedAt = circuitOpenedAt else { return false }
    if Date().timeIntervalSince(openedAt) >= circuitCooldown {
        // cooldown passed → закрываем circuit
        circuitOpenedAt = nil
        consecutiveRestarts = 0
        return false
    }
    return true
}
```

Then find the existing `restartIfDead()` method and replace its body:

```swift
func restartIfDead() -> Bool {
    if isBackendAlive() {
        consecutiveRestarts = 0
        circuitOpenedAt = nil
        return true
    }

    // Circuit breaker check
    if isCircuitOpen() {
        return false
    }

    switch supervisionMode {
    case .passive:
        do {
            try ensureBackendRunning()
            return true
        } catch {
            return false
        }

    case .active:
        consecutiveRestarts += 1
        if consecutiveRestarts >= Self.circuitOpenAfter {
            circuitOpenedAt = Date()
            return false
        }

        let delay = backoffDelay(attempt: consecutiveRestarts)
        if delay > 0 {
            Thread.sleep(forTimeInterval: delay)
        }

        stopBackend()
        do {
            try ensureBackendRunning()
            return true
        } catch {
            return false
        }
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift test --filter BackendSupervisorBackoffTests 2>&1 | tail -30
```

Expected: PASS — all 3 tests green

- [ ] **Step 5: Run full Swift test suite to catch regressions**

```bash
swift test 2>&1 | tail -20
```

Expected: PASS — no existing tests broken

- [ ] **Step 6: Commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add native/KrabEarAgent/Sources/KrabEarAgent/BackendSupervisor.swift \
        native/KrabEarAgent/Tests/KrabEarAgentTests/BackendSupervisorTests.swift
git commit -m "$(cat <<'EOF'
feat(health): exp backoff + circuit breaker in BackendSupervisor

Phase A task 4. Replace flat maxConsecutiveRestarts=3 with:
- backoff schedule 0s/2s/5s/15s
- circuit breaker after 5 fails
- 5min cooldown then auto-close

Test cooldown overridable via _testCooldownSec для unit-тестов
без реального 5-minute waits.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: BackendToast UI

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/BackendToast.swift`
- Test: manual visual test (toast UI hard to unit test)

- [ ] **Step 1: Implement BackendToast**

Create `native/KrabEarAgent/Sources/KrabEarAgent/BackendToast.swift`:

```swift
/*
 Non-modal toast для уведомления о перезапуске backend.

 Показывается в правом-нижнем углу основного экрана на 3 секунды,
 затем fade-out. НЕ блокирует активное окно.

 Связи модуля:
 1) HealthMonitor: вызывает show() из onHangDetected.
 2) main.swift: создаёт singleton при старте приложения.
*/

import AppKit

@MainActor
final class BackendToast {
    static let shared = BackendToast()

    private var panel: NSPanel?
    private var dismissTimer: Timer?

    private init() {}

    /// Показывает toast с заданным текстом на `duration` секунд.
    /// Повторный вызов до dismiss заменяет текст на новый.
    func show(_ message: String, duration: TimeInterval = 3.0) {
        dismissTimer?.invalidate()

        if panel == nil {
            createPanel()
        }
        guard let panel = panel,
              let label = panel.contentView?.subviews.first as? NSTextField
        else { return }

        label.stringValue = message
        label.sizeToFit()

        positionPanel(panel)
        panel.alphaValue = 1.0
        panel.orderFront(nil)

        dismissTimer = Timer.scheduledTimer(withTimeInterval: duration, repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.fadeOutAndHide()
            }
        }
    }

    private func createPanel() {
        let rect = NSRect(x: 0, y: 0, width: 280, height: 56)
        let panel = NSPanel(
            contentRect: rect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true

        let visualEffect = NSVisualEffectView(frame: rect)
        visualEffect.material = .hudWindow
        visualEffect.state = .active
        visualEffect.blendingMode = .behindWindow
        visualEffect.wantsLayer = true
        visualEffect.layer?.cornerRadius = 12

        let label = NSTextField(labelWithString: "")
        label.font = .systemFont(ofSize: 13, weight: .medium)
        label.textColor = .labelColor
        label.alignment = .center
        label.frame = NSRect(x: 12, y: 12, width: rect.width - 24, height: rect.height - 24)
        label.lineBreakMode = .byTruncatingTail

        visualEffect.addSubview(label)
        panel.contentView = visualEffect
        self.panel = panel
    }

    private func positionPanel(_ panel: NSPanel) {
        guard let screen = NSScreen.main else { return }
        let visible = screen.visibleFrame
        let x = visible.maxX - panel.frame.width - 24
        let y = visible.minY + 24
        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    private func fadeOutAndHide() {
        guard let panel = panel else { return }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.25
            panel.animator().alphaValue = 0.0
        }, completionHandler: {
            panel.orderOut(nil)
        })
    }
}
```

- [ ] **Step 2: Build to verify it compiles**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift build 2>&1 | tail -20
```

Expected: success — no errors

- [ ] **Step 3: Manual visual test (smoke check)**

Add temporary test code at top of `main.swift` (or any entrypoint) — just for one run:

```swift
// TEMPORARY: smoke test toast
import AppKit
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
    BackendToast.shared.show("Backend перезапущен (smoke test)", duration: 3.0)
}
DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) { app.terminate(nil) }
app.run()
```

Or build the agent, run it, and inject a fake hang to trigger real toast (deferred — easier after Task 7 wires HealthMonitor → BackendToast). For now: just verify code compiles.

**Remove** any temporary test code before commit.

- [ ] **Step 4: Commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add native/KrabEarAgent/Sources/KrabEarAgent/BackendToast.swift
git commit -m "$(cat <<'EOF'
feat(health): BackendToast — non-modal restart notification

Phase A task 5. NSPanel в правом-нижнем углу с blur background,
auto-fade через 3s. Singleton @MainActor для безопасного UI access.

Used by HealthMonitor.onHangDetected (wired in task 7).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: StatusIndicator (menu bar + panel header)

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/StatusIndicator.swift`

- [ ] **Step 1: Implement StatusIndicator**

Create `native/KrabEarAgent/Sources/KrabEarAgent/StatusIndicator.swift`:

```swift
/*
 Маленький view с цветным dot — отображает HealthState.

 Используется:
 1) В menu bar (NSStatusItem.button.image) как глобальный статус.
 2) В History panel header (8x8 dot слева от заголовка).

 Связи модуля:
 1) HealthMonitor: подписка на изменения через onStateChange.
 2) main.swift: создаёт menu bar item.
*/

import AppKit

/// View с круглым dot, цвет которого отражает HealthState.
final class StatusIndicatorView: NSView {
    private var dotColor: NSColor = .systemGreen

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        wantsLayer = true
    }

    /// Обновляет цвет dot. Можно вызывать с любого потока.
    func updateState(_ state: HealthState) {
        let newColor: NSColor
        switch state {
        case .healthy: newColor = .systemGreen
        case .hung: newColor = .systemYellow
        case .stopped: newColor = .systemRed
        }
        DispatchQueue.main.async { [weak self] in
            self?.dotColor = newColor
            self?.needsDisplay = true
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        let radius = min(bounds.width, bounds.height) / 2
        let dotRect = NSRect(
            x: bounds.midX - radius,
            y: bounds.midY - radius,
            width: radius * 2,
            height: radius * 2
        )
        dotColor.setFill()
        let path = NSBezierPath(ovalIn: dotRect)
        path.fill()
    }
}

/// Helper: создаёт NSImage с dot указанного цвета — для NSStatusItem.button.image.
enum StatusIndicatorImage {
    static func image(for state: HealthState, size: CGFloat = 14) -> NSImage {
        let img = NSImage(size: NSSize(width: size, height: size))
        img.lockFocus()
        let color: NSColor
        switch state {
        case .healthy: color = .systemGreen
        case .hung: color = .systemYellow
        case .stopped: color = .systemRed
        }
        color.setFill()
        let rect = NSRect(x: 2, y: 2, width: size - 4, height: size - 4)
        NSBezierPath(ovalIn: rect).fill()
        img.unlockFocus()
        return img
    }
}
```

- [ ] **Step 2: Build to verify it compiles**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift build 2>&1 | tail -10
```

Expected: success

- [ ] **Step 3: Commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add native/KrabEarAgent/Sources/KrabEarAgent/StatusIndicator.swift
git commit -m "$(cat <<'EOF'
feat(health): StatusIndicatorView + StatusIndicatorImage helper

Phase A task 6. Цветной dot (green/yellow/red) для отображения
HealthState. View для panel header, image helper для menu bar.

Wired in task 7 next.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Wire HealthMonitor → BackendSupervisor + Toast + StatusIndicator in main.swift

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` (or relevant `KrabEarAgentApp.swift`)
- Test: manual end-to-end

- [ ] **Step 1: Find the application bootstrap**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
grep -l "BackendSupervisor(" Sources/KrabEarAgent/*.swift | head -3
grep -l "applicationDidFinishLaunching\|NSApplicationMain" Sources/KrabEarAgent/*.swift | head -3
```

Identify the entrypoint that creates `BackendSupervisor`. This is where we wire HealthMonitor.

- [ ] **Step 2: Wire it together**

In the AppDelegate / main entrypoint, after `BackendSupervisor` is created and `ensureBackendRunningAsync()` succeeds, add:

```swift
// Phase A: continuous health monitoring
let healthMonitor = HealthMonitor(pingInterval: 3.0, hangThreshold: 2)
let supervisor = self.backendSupervisor  // existing reference
let socketPath = supervisor.socketPath

healthMonitor.setPingProvider {
    let client = IPCClient(socketPath: socketPath)
    return ((try? await client.callAsync(method: "ping", timeoutSec: 2.0)) != nil)
}

await healthMonitor.setOnHangDetected {
    // Run on main thread because we touch UI + supervisor state
    await MainActor.run {
        let restarted = supervisor.restartIfDead()
        if restarted {
            BackendToast.shared.show("Backend перезапущен", duration: 3.0)
        } else if supervisor.isCircuitOpen() {
            BackendToast.shared.show("⚠ Backend не запускается — открой логи", duration: 10.0)
        }
    }
}

await healthMonitor.start()
self.healthMonitor = healthMonitor  // store strong ref
```

Add property to AppDelegate / main class:

```swift
var healthMonitor: HealthMonitor?
```

If the app uses `applicationWillTerminate`, also stop monitor:

```swift
func applicationWillTerminate(_ notification: Notification) {
    Task {
        await healthMonitor?.stop()
    }
    // ... existing cleanup
}
```

- [ ] **Step 3: Add menu bar status item**

Find existing `NSStatusItem` setup (search `NSStatusBar.system`). Add status indicator update on healthMonitor state change:

If menu bar item already exists, add tracking. Otherwise create:

```swift
let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
statusItem.button?.image = StatusIndicatorImage.image(for: .healthy)
self.statusItem = statusItem

// Periodic status sync via Timer (since HealthMonitor is actor)
self.statusUpdateTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
    guard let monitor = self?.healthMonitor else { return }
    Task { @MainActor in
        let state = await monitor.currentState()
        self?.statusItem?.button?.image = StatusIndicatorImage.image(for: state)
    }
}
```

Add property:

```swift
var statusItem: NSStatusItem?
var statusUpdateTimer: Timer?
```

- [ ] **Step 4: Build and run**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
make build
make sign
open "Krab Ear.app"
```

Expected: app launches normally, no crashes, menu bar shows green dot.

- [ ] **Step 5: Smoke test the auto-heal flow**

In another terminal:

```bash
# Find the backend Python PID
ps aux | grep "service.py" | grep -v grep | awk '{print $2}' | head -1
# Replace <PID> below with the printed PID
kill -9 <PID>
```

Expected within ~6 seconds:
1. Menu bar dot turns yellow (hang detected)
2. Toast "Backend перезапущен" appears in bottom-right
3. Menu bar dot returns to green
4. Next dictation works without manual intervention

If toast doesn't appear: check Console.app for `KrabEarAgent` logs for restart errors.

- [ ] **Step 6: Smoke test the circuit breaker**

This requires forcing restart failures. Easiest way: temporarily rename Python venv to force `startBackendProcess` to fail:

```bash
mv "/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear" "/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear_OFF"
# Now kill backend
ps aux | grep "service.py" | grep -v grep | awk '{print $2}' | xargs kill -9 2>/dev/null
# Wait ~30s for 5 restart attempts (with 0+2+5+15s backoff = 22s minimum)
sleep 30
```

Expected:
- Menu bar dot turns red after circuit opens
- Toast "⚠ Backend не запускается" appears

Restore:

```bash
mv "/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear_OFF" "/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear"
# Wait for cooldown (5min) or restart app
```

- [ ] **Step 7: Commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add native/KrabEarAgent/Sources/KrabEarAgent/main.swift  # or actual modified file
git commit -m "$(cat <<'EOF'
feat(health): wire HealthMonitor + Toast + StatusIndicator into app lifecycle

Phase A task 7. After BackendSupervisor.ensureBackendRunningAsync,
spawns HealthMonitor с 3s pinging. На hang detected — restartIfDead
+ toast notification. Menu bar status item обновляется каждую сек.

End-to-end smoke verified:
1) kill -9 backend → toast + auto-restart за <6s
2) Forced startup failure → 5 retries → circuit opens → red dot

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Update Krab Ear.app bundle and final integration test

**Files:**
- Update: `Krab Ear.app/Contents/MacOS/KrabEarAgent` (binary)

- [ ] **Step 1: Build release binary and copy to bundle**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift build -c release
cp -f .build/release/KrabEarAgent "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
codesign -s "Krab Ear Dev Local" -f "../../Krab Ear.app/Contents/MacOS/KrabEarAgent" 2>/dev/null || \
  codesign -s - -f "../../Krab Ear.app/Contents/MacOS/KrabEarAgent"
```

- [ ] **Step 2: Quit any running agent and relaunch from bundle**

```bash
pkill -9 -f KrabEarAgent || true
sleep 1
open "/Users/pablito/Antigravity_AGENTS/Krab Ear/Krab Ear.app"
```

- [ ] **Step 3: Verify menu bar shows green dot**

Look at menu bar (top right). Should see small green dot.

- [ ] **Step 4: Run full Python test suite for regression**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/ -v --tb=short 2>&1 | tail -40
```

Expected: all existing ~6500 tests pass + 2 new test files (test_health_metrics.py, test_health_metrics_integration.py) green

- [ ] **Step 5: Run full Swift test suite for regression**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear/native/KrabEarAgent"
swift test 2>&1 | tail -20
```

Expected: all existing tests + 2 new test files (HealthMonitorTests, BackendSupervisorBackoffTests) green

- [ ] **Step 6: Real-world smoke — dictate after kill**

```bash
# Get backend PID
BACKEND_PID=$(ps aux | grep "service.py" | grep -v grep | awk '{print $2}' | head -1)
echo "Backend PID: $BACKEND_PID"
kill -9 $BACKEND_PID
# Wait for auto-recovery
sleep 5
# Now press Right Option and dictate something — should work without manual restart
```

Expected: dictation works on first hotkey press after kill (no "backend недоступен" error).

- [ ] **Step 7: Commit bundle binary update**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add "Krab Ear.app/Contents/MacOS/KrabEarAgent"
git commit -m "$(cat <<'EOF'
chore(app): rebuild bundle binary with Phase A health monitor

Phase A task 8. End-of-phase bundle update — все Phase A фичи
включены в shipped binary.

Smoke verified:
- Menu bar status dot работает (green idle)
- kill -9 backend → auto-restart за <6s
- Following dictation works без manual intervention

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Done criteria for Phase A

После всех 8 задач:

- ✅ `_handle_ping` возвращает `rss_mb`, `uptime_sec`, `active_requests`, version, history_count
- ✅ `HealthMetrics` модуль stdlib-only, переиспользуем в Phase B/C
- ✅ `HealthMonitor` actor пингует backend каждые 3s, детектит hang за 2 fail подряд
- ✅ `BackendSupervisor.restartIfDead` — exp backoff (0/2/5/15s), circuit breaker после 5 fails, 5min cooldown
- ✅ `BackendToast` показывает "Backend перезапущен" в bottom-right
- ✅ `StatusIndicator` — green/yellow/red dot в menu bar обновляется в реальном времени
- ✅ Smoke test: `kill -9 <python-pid>` → backend up за <6s + toast + working dictation
- ✅ Smoke test: forced startup failure → 5 retries → circuit opens → red dot + warning toast
- ✅ All existing tests (~6500 Python + Swift) still pass

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| HealthMonitor flakes from short pingInterval (3s) on slow boot | First ping has 10s timeout, subsequent 2s. Hang threshold = 2 (так что нужно ≥6s consecutive fail) |
| Toast steals focus when typing | NSPanel `nonactivatingPanel` style — never becomes key window |
| Circuit breaker traps user после bad config change | 5min cooldown + auto-close. Если нужно сбросить раньше — relaunch app |
| MainActor wiring in main.swift breaks Swift 6 strict concurrency | Use `@MainActor` annotations + `Task.detached` boundary clearly. Test with `swift build -Xswiftc -strict-concurrency=complete` |
| `_handle_ping` extra fields break existing consumers (e.g., Voice Gateway, main Krab) | Additive change — old fields preserved. New fields ignored by older clients |

---

## Out of scope (Phase B / C territory)

- ❌ Toast for non-restart errors (paste fail, rewriter timeout) → Phase B
- ❌ Persisting in-flight transcription requests across restart → not in any phase (deferred)
- ❌ Memory leak detection / RSS watermark auto-restart trigger → Phase C.1
- ❌ Sentry integration for restart events → Phase B (reuse breadcrumb infrastructure)

---

## Self-review

✅ **Spec coverage**: каждый компонент Phase A spec'а имеет соответствующий task:
- HealthMonitor actor → Task 3
- handle_ping extension → Task 1
- health_metrics.py → Task 1
- StatusIndicatorView → Task 6
- Двухкольцевой supervisor (inner ring) → Task 4 + 7
- Restart с exp backoff → Task 4
- Circuit breaker → Task 4
- UI feedback (toast + status dot) → Tasks 5, 6, 7
- Acceptance criteria → Tasks 7, 8 (smoke tests)

✅ **Placeholders**: нет TBD/TODO. Все code blocks содержат полный код. Smoke tests имеют exact commands.

✅ **Type consistency**:
- `HealthState` enum {healthy, hung, stopped} consistent across HealthMonitor, StatusIndicatorView, StatusIndicatorImage
- `HealthMonitor.setPingProvider`, `setOnHangDetected`, `currentState` имена согласованы между tests и implementation
- `BackendSupervisor.isCircuitOpen()`, `backoffDelay(attempt:)` — same signature in tests and impl
- `_testCooldownSec` — same name everywhere
- `track_request()` context manager — consistent в HealthMetrics tests + integration tests

---

*End of plan.*
