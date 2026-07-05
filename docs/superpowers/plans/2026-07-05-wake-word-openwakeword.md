# Живой Wake Word (openWakeWord, IPC-поллинг) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Слово-пробуждение реально запускает «Разговор с AI»: Python-адаптер openWakeWord слушает микрофон, Swift-агент поллит `wake_word_status` по IPC и триггерит разговор — мёртвый Porcupine-код удаляется.

**Architecture:** Микрофоном владеет Python-бэкенд (`backend/openwakeword_adapter.py`, уже готов и укреплён). Агент шлёт `wake_word_start/stop` по IPC и раз в 0.75с поллит `wake_word_status`; новое поле `last_detection.ts` (монотонное) растёт → триггер. Два процесса прода (IPC-бэкенд и REST) имеют раздельные EventBus, поэтому SSE не используется — только Unix-IPC. Спека: `docs/superpowers/specs/2026-07-05-wake-word-openwakeword-design.md`.

**Tech Stack:** Python 3.12+ (openwakeword — опциональная зависимость, есть stub-режим), Swift 6 (AppKit, Timer + DispatchQueue.global идиом как в main+RealtimeOverlay), unittest/pytest, XCTest.

**Контекст-правила репо (обязательны):**
- Тесты Python гонять с `PYTHONPATH=$(pwd)/KrabEar`, из корня репо.
- Перед PR: ubuntu-parity `bash scripts/pre_merge_py312_check.sh <тест-файлы>` (openwakeword там ОТСУТСТВУЕТ — тесты не должны импортировать реальную либу) + flake8 CI-командой (см. Task 10).
- Swift: `cd native/KrabEarAgent && swift build -c release`; глиф-гейт — не вводить новые non-ASCII глифы (`●▶✓` и т.п. запрещены; `«»—·` уже установлены).
- НЕ инстанцировать `BackendService` в тестах (daemon-треды → exit(1) чанка); наш файл конструирует адаптер напрямую.
- Коммиты: trailer `Co-Authored-By:` по текущей модели.

---

## File Structure (карта изменений)

| Действие | Файл | Ответственность |
|---|---|---|
| Modify | `KrabEar/backend/openwakeword_adapter.py` | `_last_detection` состояние + `_record_detection()` + сбросы + `last_detection` в status + privacy loop-guard |
| Modify | `KrabEar/backend/service.py` (~строка 973) | Пробросить `settings_get=self._get_runtime_setting` (фикс декоративного privacy-гейта) |
| Create | `KrabEar/tests/test_wake_word_polling_contract.py` | Все Python-тесты волны (state, contract, wiring, privacy) |
| Create | `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift` | `WakeWordDetectionTracker` (чистая логика) + `WakeWordPoller` (Timer+IPC) + `WakeWordPauseReason` |
| Create | `native/KrabEarAgent/Tests/KrabEarAgentTests/WakeWordDetectionTrackerTests.swift` | XCTest трекера |
| Delete | `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordListener.swift` | Мёртвый Porcupine-путь (никогда не работал) |
| Delete | `native/KrabEarAgent/Tests/KrabEarAgentTests/WakeWordListenerTests.swift` | Тесты мёртвого пути (MockPorcupine — тот самый шум в agent.log) |
| Modify | `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` | Свойство poller вместо listener (~стр.149), rewiring `setupWakeWordListenerIfEnabled`/`applyWakeWordEnabled` (~стр.474–507), observers разговора |
| Modify | `native/KrabEarAgent/Sources/KrabEarAgent/main+RealtimeOverlay.swift` | pause/resume(.recording) рядом с `recordingDidStart/Stop` (стр. ~17/~50) |
| Modify | `native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController.swift` | Notification.Name + post в `startConversation()` (:119) / `stopConversation()` (:130) |
| Modify | `native/KrabEarAgent/Sources/KrabEarAgent/main+HealthMonitor.swift` | Хук в `setPrivacyMode(_:)` (~стр.48) |
| Modify | `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift` | Обновить wake-word ряд (убрать Porcupine-текст) + статус/модель/порог в `buildVoiceAssistantSection()` (~стр.1240–1305) + хендлеры |
| Modify | `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift` | Объявления 3 новых контролов рядом с `vaWakeWordToggle` |
| Create | `KrabEar/requirements-wakeword.txt` | Опциональная зависимость (намеренно НЕ в requirements.txt — ubuntu-CI ставит его целиком) |
| Modify | `scripts/bootstrap_backend.command` | Необязательная установка openwakeword + `download_models()` |
| Modify | `docs/IPC_API_REFERENCE.md`, `CLAUDE.md`, `docs/USER_MANUAL.md` | Контракт status, замена Porcupine-упоминаний |

Примечание по спеке: спека упоминала доустановку и через `Start Krab Ear.command`, но проверка показала — в нём нет pip-секции (venv ставится не там). Покрытие: bootstrap-инсталлятор + шаг деплоя + docs.

---

### Task 0: Ветка

**Files:** нет (git)

- [ ] **Step 0.1: Свежий main и ветка**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git checkout codex/krab-ear-v2 && git pull --ff-only
git checkout -b feature/wake-word-openwakeword
```
Expected: `Switched to a new branch 'feature/wake-word-openwakeword'`

---

### Task 1: Backend — состояние `last_detection` (TDD)

**Files:**
- Create: `KrabEar/tests/test_wake_word_polling_contract.py`
- Modify: `KrabEar/backend/openwakeword_adapter.py`

- [ ] **Step 1.1: Написать падающие тесты**

Создать `KrabEar/tests/test_wake_word_polling_contract.py` целиком:

```python
"""Контракт IPC-поллинга wake word (spec 2026-07-05-wake-word-openwakeword).

Агент поллит wake_word_status и триггерит разговор по росту last_detection.ts.
Здесь: состояние last_detection в адаптере, контракт status, сбросы start/stop,
privacy loop-guard, и source-контракт проводки settings_get в service.py
(до фикса гейт privacy в handle_wake_word_start был ДЕКОРАТИВНЫМ в проде —
адаптер конструировался без settings_get).

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_wake_word_polling_contract.py -v
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
BACKEND_DIR = _PROJECT_ROOT / "KrabEar"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402


class _NoLoopAdapter(OpenWakeWordAdapter):
    """Адаптер с no-op слушателем: start() спавнит поток, который сразу выходит.

    Позволяет тестировать сбросы состояния в start()/stop() без sounddevice
    и без установленного openwakeword.
    """

    def _listen_loop(self, **kwargs):  # noqa: D401
        return


class TestLastDetectionState(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)

    def test_initially_none(self) -> None:
        status = self.adapter.handle_wake_word_status({})
        self.assertIn("last_detection", status)
        self.assertIsNone(status["last_detection"])

    def test_record_detection_appears_in_status(self) -> None:
        self.adapter._record_detection("hey_jarvis", 0.91)
        status = self.adapter.handle_wake_word_status({})
        det = status["last_detection"]
        self.assertIsNotNone(det)
        self.assertEqual(det["model"], "hey_jarvis")
        self.assertAlmostEqual(det["score"], 0.91, places=6)
        self.assertIsInstance(det["ts"], float)

    def test_ts_monotonically_increases(self) -> None:
        self.adapter._record_detection("hey_jarvis", 0.8)
        ts1 = self.adapter.handle_wake_word_status({})["last_detection"]["ts"]
        self.adapter._record_detection("hey_jarvis", 0.85)
        ts2 = self.adapter.handle_wake_word_status({})["last_detection"]["ts"]
        self.assertGreater(ts2, ts1)

    def test_status_keeps_existing_contract_keys(self) -> None:
        status = self.adapter.handle_wake_word_status({})
        for key in ("ok", "running", "active_model", "engine_available"):
            self.assertIn(key, status)


class TestStartStopReset(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def test_stop_clears_last_detection(self) -> None:
        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._record_detection("hey_jarvis", 0.9)
        adapter.stop()  # потока нет — early return, но состояние чистится
        self.assertIsNone(adapter.handle_wake_word_status({})["last_detection"])

    def test_start_clears_last_detection(self) -> None:
        adapter = _NoLoopAdapter(data_dir=self.tmp)
        adapter._oww_available = True  # обходим проверку установленности либы
        adapter._record_detection("stale", 0.7)
        with patch.object(adapter, "_load_model", return_value=MagicMock()):
            adapter.start("hey_jarvis", on_detected=lambda n, s: None)
        try:
            self.assertIsNone(
                adapter.handle_wake_word_status({})["last_detection"]
            )
        finally:
            adapter.stop()


class TestPrivacyLoopGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def test_blocked_when_privacy_on(self) -> None:
        adapter = OpenWakeWordAdapter(
            data_dir=self.tmp, settings_get=lambda k, d: True
        )
        self.assertTrue(adapter._privacy_blocked())

    def test_not_blocked_when_privacy_off(self) -> None:
        adapter = OpenWakeWordAdapter(
            data_dir=self.tmp, settings_get=lambda k, d: False
        )
        self.assertFalse(adapter._privacy_blocked())

    def test_settings_exception_fails_open_to_false(self) -> None:
        def _boom(k, d):
            raise RuntimeError("settings unavailable")

        adapter = OpenWakeWordAdapter(data_dir=self.tmp, settings_get=_boom)
        self.assertFalse(adapter._privacy_blocked())


class TestServiceWiringSourceContract(unittest.TestCase):
    """Гейт privacy в handle_wake_word_start работает ТОЛЬКО если service.py
    пробросил settings_get. До фикса конструкция была декоративной."""

    def test_service_passes_runtime_settings_get(self) -> None:
        src = (BACKEND_DIR / "backend" / "service.py").read_text(encoding="utf-8")
        pattern = (
            r"OpenWakeWordAdapter\(\s*data_dir=self\.store\.data_dir,"
            r"\s*settings_get=self\._get_runtime_setting,?\s*\)"
        )
        self.assertRegex(src, pattern)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 1.2: Прогнать — убедиться, что падают правильно**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_wake_word_polling_contract.py -v -p no:cacheprovider
```
Expected: FAIL — `AttributeError: ... has no attribute '_record_detection'`, `KeyError/AssertionError: last_detection`, `no attribute '_privacy_blocked'`, и `AssertionError` в source-контракте. (TestStartStopReset.test_start тоже упадёт на отсутствии сброса.)

- [ ] **Step 1.3: Реализация в `openwakeword_adapter.py`**

(a) Импорт `time` — в блок импортов после `import threading`:
```python
import time
```

(b) В `__init__` после `self._active_model: str | None = None`:
```python
        # Последняя детекция для IPC-поллинга агента (wake_word_status).
        # Монотонный ts — агент дебаунсит по росту, wall-clock не нужен.
        self._last_detection: dict[str, Any] | None = None
```

(c) Новый метод сразу после `active_model()`:
```python
    def _record_detection(self, model_name: str, score: float) -> None:
        """Фиксирует последнюю детекцию для wake_word_status (IPC-поллинг)."""
        with self._lock:
            self._last_detection = {
                "model": model_name,
                "score": float(score),
                "ts": time.monotonic(),
            }
```

(d) В `start()` — сразу после `self._stop_event.clear()` (мы уже под `self._lock`):
```python
            self._last_detection = None  # свежая сессия — стейл-детекция не триггерит
```

(e) В `stop()` — первой строкой внутри `with self._lock:` (ДО early-return):
```python
            self._last_detection = None
```

(f) `handle_wake_word_status` — заменить целиком (внимание: `self._lock` — обычный `Lock`, НЕ вкладывать в него вызовы `is_running()`/`active_model()`, они сами берут лок):
```python
    def handle_wake_word_status(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """IPC: статус адаптера + последняя детекция для поллинга агента."""
        with self._lock:
            last = dict(self._last_detection) if self._last_detection else None
        return {
            "ok": True,
            "running": self.is_running(),
            "active_model": self.active_model(),
            "engine_available": self._oww_available,
            "last_detection": last,
        }
```

(g) В `_listen_loop` — заменить детекционную ветку:
```python
                    prediction = oww.predict(flat)
                    for mdl_name, score in prediction.items():
                        if score >= threshold and self._on_detected is not None:
                            self._on_detected(mdl_name, float(score))
```
на:
```python
                    prediction = oww.predict(flat)
                    for mdl_name, score in prediction.items():
                        if score >= threshold:
                            self._record_detection(mdl_name, float(score))
                            if self._on_detected is not None:
                                self._on_detected(mdl_name, float(score))
```

(h) Privacy loop-guard — новый метод после `_record_detection`:
```python
    def _privacy_blocked(self) -> bool:
        """True если privacy mode включён — держать микрофон wake word нельзя.

        Fail-open к False: сломанный settings-провайдер не должен «ронять»
        слушатель, за выключение отвечает и агент (setPrivacyMode → stop).
        """
        try:
            return bool(self._settings_get("privacy_mode_enabled", False))
        except Exception:
            return False
```
и в `_listen_loop`, первой проверкой внутри `while not self._stop_event.is_set():`:
```python
                while not self._stop_event.is_set():
                    if self._privacy_blocked():
                        logger.info(
                            "OpenWakeWordAdapter: privacy mode включён — "
                            "слушатель остановлен"
                        )
                        break
```

- [ ] **Step 1.4: Прогнать новый файл — зелёный, кроме source-контракта**

```bash
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_wake_word_polling_contract.py -v -p no:cacheprovider
```
Expected: всё PASS, кроме `TestServiceWiringSourceContract` (фикс — Task 2).

- [ ] **Step 1.5: Существующие wake-word тесты не сломаны**

```bash
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_openwakeword_adapter.py KrabEar/tests/test_openwakeword_security_W1210.py KrabEar/tests/test_wave35_wakeword_misc.py -q -p no:cacheprovider
```
Expected: все PASS.

- [ ] **Step 1.6: Commit**

```bash
git add KrabEar/backend/openwakeword_adapter.py KrabEar/tests/test_wake_word_polling_contract.py
git commit -m "feat(wake-word): last_detection состояние + privacy loop-guard в адаптере

Агент будет поллить wake_word_status по IPC (spec 2026-07-05): детекция
фиксируется в last_detection {model, score, ts=monotonic} под локом,
start()/stop() сбрасывают. _privacy_blocked() проверяется каждый чанк
_listen_loop — микрофон не переживает включение privacy mode."
```
(+ трейлер Co-Authored-By текущей модели.)

---

### Task 2: Backend — фикс декоративной проводки settings_get

**Files:**
- Modify: `KrabEar/backend/service.py` (~строка 973)

- [ ] **Step 2.1: Фикс**

В `service.py` найти (якорь, ~стр. 973):
```python
        # openWakeWord adapter (default disabled via WAKE_WORD_ENGINE setting)
        self._oww_adapter = OpenWakeWordAdapter(data_dir=self.store.data_dir)
```
заменить на:
```python
        # openWakeWord adapter (default disabled via WAKE_WORD_ENGINE setting).
        # settings_get ОБЯЗАТЕЛЕН: без него privacy-гейт в handle_wake_word_start
        # и loop-guard читают дефолт (False) и являются декоративными.
        self._oww_adapter = OpenWakeWordAdapter(
            data_dir=self.store.data_dir,
            settings_get=self._get_runtime_setting,
        )
```

- [ ] **Step 2.2: Source-контракт зелёный + дым сервиса**

```bash
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_wake_word_polling_contract.py -v -p no:cacheprovider
PYTHONPATH=$(pwd)/KrabEar python3 -c "import backend.service; print('service.py import OK')"
```
Expected: 12/12 PASS; `service.py import OK`.

- [ ] **Step 2.3: Аудит декоративной проводки не регрессировал**

```bash
python3 scripts/audit_decorative_wiring.py --strict | tail -5
```
Expected: без новых findings (фикс только УЛУЧШАЕТ картину).

- [ ] **Step 2.4: Commit**

```bash
git add KrabEar/backend/service.py
git commit -m "fix(wake-word): пробросить settings_get в OpenWakeWordAdapter

Найдено при подготовке волны: адаптер конструировался БЕЗ settings_get,
поэтому privacy-гейт handle_wake_word_start в проде всегда читал дефолт
False — декоративная проводка. Теперь гейт и loop-guard живые
(_get_runtime_setting → кэш настроек 5s TTL). Source-контракт в
test_wake_word_polling_contract.py защищает от отката."
```

---

### Task 3: Backend-гейты волны (flake8 + ubuntu-parity)

**Files:** нет новых

- [ ] **Step 3.1: flake8 CI-командой по изменённым py**

```bash
source .venv_krab_ear/bin/activate
python3 -m flake8 KrabEar/backend/openwakeword_adapter.py KrabEar/backend/service.py KrabEar/tests/test_wake_word_polling_contract.py \
  --max-line-length=150 --extend-ignore=E501 \
  --per-file-ignores='KrabEar/tests/*:F401,F541,F841,E203,E301,E302,E303,E305,E306,E401,E402,W391' \
  --statistics
```
Expected: пустой вывод, exit 0.

- [ ] **Step 3.2: ubuntu-parity (py3.12, mlx/openwakeword отсутствуют)**

```bash
bash scripts/pre_merge_py312_check.sh KrabEar/tests/test_wake_word_polling_contract.py KrabEar/tests/test_openwakeword_adapter.py KrabEar/tests/test_openwakeword_security_W1210.py
```
Expected: `=== ALL GREEN (ubuntu-parity py3.12, ...) ===` (передавать ТОЛЬКО тест-файлы — исходники harness считает FAIL по «0 collected»).

---

### Task 4: Swift — WakeWordDetectionTracker (TDD)

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift` (пока только трекер+enum, поллер добавит Task 5)
- Create: `native/KrabEarAgent/Tests/KrabEarAgentTests/WakeWordDetectionTrackerTests.swift`

- [ ] **Step 4.1: Тесты**

`WakeWordDetectionTrackerTests.swift` целиком:
```swift
import XCTest
@testable import KrabEarAgent

/// Контракт дебаунса поллинга wake word (spec 2026-07-05):
/// триггер ровно один раз на новую детекцию; первый снапшот — только baseline.
final class WakeWordDetectionTrackerTests: XCTestCase {

    func testFirstPollWithValueBaselinesWithoutTrigger() {
        let t = WakeWordDetectionTracker()
        // Агент перезапустился, а у бэкенда осталась старая детекция —
        // она НЕ должна выстрелить.
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: 123.45))
    }

    func testNilThenValueTriggers() {
        let t = WakeWordDetectionTracker()
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: nil))   // baseline: пусто
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 10.0))   // появилась → триггер
    }

    func testSameValueDoesNotRetrigger() {
        let t = WakeWordDetectionTracker()
        _ = t.shouldTrigger(lastDetectionTs: nil)
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 10.0))
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: 10.0))
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: 10.0))
    }

    func testIncreasedValueTriggersAgain() {
        let t = WakeWordDetectionTracker()
        _ = t.shouldTrigger(lastDetectionTs: nil)
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 10.0))
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 11.5))
    }

    func testNilAfterValueDoesNotTriggerUntilNewValue() {
        let t = WakeWordDetectionTracker()
        _ = t.shouldTrigger(lastDetectionTs: nil)
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 10.0))
        // Бэкенд перезапустился: start() сбросил last_detection в None.
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: nil))
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 3.0)) // новый monotonic-отсчёт
    }

    func testResetRearmsBaseline() {
        let t = WakeWordDetectionTracker()
        _ = t.shouldTrigger(lastDetectionTs: nil)
        XCTAssertTrue(t.shouldTrigger(lastDetectionTs: 10.0))
        t.reset()
        XCTAssertFalse(t.shouldTrigger(lastDetectionTs: 10.0)) // снова baseline
    }
}
```

ВНИМАНИЕ на `testNilAfterValueDoesNotTriggerUntilNewValue`: после рестарта бэкенда monotonic начинается заново — новый ts может быть МЕНЬШЕ старого. Поэтому контракт: `nil` re-arm'ит baseline (сбрасывает), и любой следующий ts триггерит.

- [ ] **Step 4.2: Убедиться, что не компилируется (нет типа)**

```bash
cd native/KrabEarAgent && swift build 2>&1 | tail -3
```
Expected: build OK (тест-таргет не собирается в build), но:
```bash
swift test --filter WakeWordDetectionTrackerTests 2>&1 | tail -5
```
Expected: FAIL компиляции — `cannot find 'WakeWordDetectionTracker' in scope`.

- [ ] **Step 4.3: Реализация (создать `WakeWordPoller.swift`, пока трекер+enum)**

```swift
/*
 WakeWordPoller.swift — wake word через IPC-поллинг backend'а.

 Архитектура (spec docs/superpowers/specs/2026-07-05-wake-word-openwakeword-design.md):
 - Микрофоном владеет Python-бэкенд (backend/openwakeword_adapter.py, openWakeWord).
 - Агент шлёт wake_word_start/stop по IPC и раз в 0.75с поллит wake_word_status.
 - Рост last_detection.ts → триггер «Разговор с AI».
 - SSE НЕ используется: прод = два процесса (IPC-бэкенд и REST) с раздельными
   EventBus, событие из service.py до SSE на :5005 не доходит.

 WakeWordDetectionTracker — чистая, тестируемая логика дебаунса (без IPC/таймеров).
 WakeWordPoller — тонкая обвязка: Timer на main + sync IPC на global queue
 (идиом main+RealtimeOverlay.refreshRealtimeOverlay, AGENT-3: без sync IPC на main).
*/

import AppKit
import Foundation

// MARK: - Причины паузы (идемпотентны по причине — Set, не счётчик)

enum WakeWordPauseReason: String, CaseIterable, Sendable {
    case recording      // идёт диктовка — слушатель поймал бы её же
    case conversation   // идёт «Разговор с AI» — микрофон занят разговором
    case privacyMode    // privacy mode — микрофон wake word держать нельзя
}

// MARK: - Чистая логика дебаунса

/// Решает «была ли НОВАЯ детекция» по последовательности значений last_detection.ts.
/// Первый вызов только устанавливает baseline (стейл-детекция прошлой сессии
/// или живого бэкенда при перезапуске агента не триггерит). nil re-arm'ит
/// baseline: после рестарта бэкенда monotonic-отсчёт начинается заново и новый
/// ts может быть меньше старого.
final class WakeWordDetectionTracker {
    private var initialized = false
    private var baselineTs: Double?

    /// true ровно один раз на каждую новую детекцию.
    func shouldTrigger(lastDetectionTs ts: Double?) -> Bool {
        if !initialized {
            initialized = true
            baselineTs = ts
            return false
        }
        guard let ts else {
            baselineTs = nil   // backend сбросил состояние (рестарт/новая сессия)
            return false
        }
        if let base = baselineTs, ts <= base { return false }
        baselineTs = ts
        return true
    }

    func reset() {
        initialized = false
        baselineTs = nil
    }
}
```

- [ ] **Step 4.4: Тесты зелёные**

```bash
swift test --filter WakeWordDetectionTrackerTests 2>&1 | tail -5
```
Expected: `Executed 6 tests, with 0 failures`.

- [ ] **Step 4.5: Commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift native/KrabEarAgent/Tests/KrabEarAgentTests/WakeWordDetectionTrackerTests.swift
git commit -m "feat(wake-word): WakeWordDetectionTracker — чистая логика дебаунса поллинга"
```

---

### Task 5: Swift — WakeWordPoller + rewiring main.swift + удаление Porcupine

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift` (добавить поллер)
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` (~стр.149, ~474–507)
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+HealthMonitor.swift` (`setPrivacyMode`, ~стр.48)
- Delete: `WakeWordListener.swift`, `Tests/.../WakeWordListenerTests.swift`

- [ ] **Step 5.1: Дописать поллер в `WakeWordPoller.swift`** (после трекера):

```swift
// MARK: - Поллер

@MainActor
final class WakeWordPoller {
    static let pollInterval: TimeInterval = 0.75
    /// Мин. пауза между self-heal попытками wake_word_start (backend мог
    /// перезапуститься launchd'ом — сессия адаптера пропадает).
    static let restartMinGapSec: TimeInterval = 10.0

    private let ipcProvider: () -> IPCClient?
    private let isToggleEnabled: () -> Bool
    private let onDetection: () -> Void

    private let tracker = WakeWordDetectionTracker()
    private var timer: Timer?
    private var pausedReasons: Set<WakeWordPauseReason> = []
    private var inFlight = false
    private var lastStartAttempt: TimeInterval = 0
    /// Последний известный engine_available (для Settings-статуса).
    private(set) var lastEngineAvailable: Bool?

    init(
        ipcProvider: @escaping () -> IPCClient?,
        isToggleEnabled: @escaping () -> Bool,
        onDetection: @escaping () -> Void
    ) {
        self.ipcProvider = ipcProvider
        self.isToggleEnabled = isToggleEnabled
        self.onDetection = onDetection
    }

    var isActive: Bool { timer != nil }

    /// Включить: wake_word_start в backend + периодический поллинг статуса.
    func activate() {
        guard timer == nil else { return }
        tracker.reset()
        sendStart(force: true)
        let t = Timer.scheduledTimer(withTimeInterval: Self.pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        AgentLogger.shared.info("[WakeWord] Поллинг запущен (интервал \(Self.pollInterval)s)")
    }

    /// Выключить: остановить поллинг + wake_word_stop в backend.
    func deactivate() {
        guard timer != nil else { return }
        timer?.invalidate()
        timer = nil
        pausedReasons.removeAll()
        sendStop()
        AgentLogger.shared.info("[WakeWord] Поллинг остановлен")
    }

    /// Пауза по причине (запись/разговор/privacy). Идемпотентна по причине.
    func pause(_ reason: WakeWordPauseReason) {
        guard timer != nil else { return }
        let wasEmpty = pausedReasons.isEmpty
        pausedReasons.insert(reason)
        if wasEmpty {
            sendStop()
            AgentLogger.shared.info("[WakeWord] Пауза: \(reason.rawValue)")
        }
    }

    /// Снять паузу по причине; возобновляет только когда причин не осталось.
    func resume(_ reason: WakeWordPauseReason) {
        pausedReasons.remove(reason)
        guard pausedReasons.isEmpty, timer != nil, isToggleEnabled() else { return }
        tracker.reset()
        sendStart(force: true)
        AgentLogger.shared.info("[WakeWord] Возобновлён после: \(reason.rawValue)")
    }

    // MARK: - Внутренние

    private func tick() {
        guard timer != nil, pausedReasons.isEmpty, !inFlight,
              let ipc = ipcProvider() else { return }
        inFlight = true
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let resp = try? ipc.call(method: "wake_word_status", params: [:])
            DispatchQueue.main.async {
                guard let self else { return }
                self.inFlight = false
                // Backend down → nil; HealthMonitor чинит сам, мы просто ждём.
                guard let result = resp?["result"] as? [String: Any] else { return }
                let engineAvailable = result["engine_available"] as? Bool ?? false
                self.lastEngineAvailable = engineAvailable
                let running = result["running"] as? Bool ?? false
                let ts = (result["last_detection"] as? [String: Any])?["ts"] as? Double
                if self.tracker.shouldTrigger(lastDetectionTs: ts) {
                    AgentLogger.shared.info("[WakeWord] Детекция — запускаю разговор")
                    self.onDetection()
                    return
                }
                // Self-heal: launchd перезапустил backend → сессия адаптера пропала.
                if !running && engineAvailable {
                    self.sendStart(force: false)
                }
            }
        }
    }

    private func sendStart(force: Bool) {
        let now = ProcessInfo.processInfo.systemUptime
        if !force && now - lastStartAttempt < Self.restartMinGapSec { return }
        lastStartAttempt = now
        guard let ipc = ipcProvider() else { return }
        let model = UserDefaults.standard.string(forKey: "KrabEar_WakeWordModel") ?? "hey_jarvis"
        var threshold = UserDefaults.standard.double(forKey: "KrabEar_WakeWordThreshold")
        if threshold <= 0 { threshold = 0.5 }
        DispatchQueue.global(qos: .utility).async {
            let resp = try? ipc.call(
                method: "wake_word_start",
                params: ["model": model, "threshold": threshold]
            )
            let result = resp?["result"] as? [String: Any]
            let ok = result?["ok"] as? Bool ?? false
            if !ok {
                let why = (result?["error"] as? String)
                    ?? (result?["reason"] as? String) ?? "нет ответа от backend"
                AgentLogger.shared.warn("[WakeWord] wake_word_start не удался: \(why)")
            }
        }
    }

    private func sendStop() {
        guard let ipc = ipcProvider() else { return }
        DispatchQueue.global(qos: .utility).async {
            _ = try? ipc.call(method: "wake_word_stop", params: [:])
        }
    }
}
```

- [ ] **Step 5.2: main.swift — свойство** (строка ~149)

Было: `private var wakeWordListener: WakeWordListener?`
Стало:
```swift
    var wakeWordPoller: WakeWordPoller?
    private var wakeWordConversationObservers: [NSObjectProtocol] = []
```
(не private: к poller обращаются main+RealtimeOverlay/main+HealthMonitor/Settings.)

- [ ] **Step 5.3: main.swift — rewiring функций** (~стр. 474–507)

Заменить тела `setupWakeWordListenerIfEnabled()` и `applyWakeWordEnabled(_:)` целиком:
```swift
    /// Wake word через IPC-поллинг backend'а (openWakeWord).
    /// Дефолт: выключен (приватность). Включается в Settings → «Разговор с AI».
    /// Порядок и имена функций сохранены для минимального диффа вызывающих мест.
    func setupWakeWordListenerIfEnabled() {
        let enabled = UserDefaults.standard.bool(forKey: "KrabEar_WakeWordEnabled")
        guard enabled else {
            logger.info("Wake word: выключен (UserDefaults KrabEar_WakeWordEnabled=false)")
            return
        }
        if wakeWordPoller == nil {
            wakeWordPoller = WakeWordPoller(
                ipcProvider: { [weak self] in self?.ipcClient },
                isToggleEnabled: { UserDefaults.standard.bool(forKey: "KrabEar_WakeWordEnabled") },
                onDetection: { [weak self] in
                    self?.historyPanel?.triggerConversationFromWakeWord()
                }
            )
        }
        setupWakeWordConversationObservers()
        wakeWordPoller?.activate()
    }

    /// Перезапустить wake word с новым значением enabled.
    /// Вызывается из HistoryPanelController+Settings при изменении тогглера.
    func applyWakeWordEnabled(_ enabled: Bool) {
        UserDefaults.standard.set(enabled, forKey: "KrabEar_WakeWordEnabled")
        if enabled {
            setupWakeWordListenerIfEnabled()
        } else {
            wakeWordPoller?.deactivate()
        }
    }

    /// Разговор занимает микрофон: пауза wake word на время разговора.
    /// Notification'ы шлёт ConversationViewController (start/stopConversation) —
    /// единственная воронка всех путей старта/останова разговора.
    private func setupWakeWordConversationObservers() {
        guard wakeWordConversationObservers.isEmpty else { return }
        let nc = NotificationCenter.default
        wakeWordConversationObservers.append(
            nc.addObserver(forName: .krabConversationStarted, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.wakeWordPoller?.pause(.conversation) }
            }
        )
        wakeWordConversationObservers.append(
            nc.addObserver(forName: .krabConversationStopped, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in self?.wakeWordPoller?.resume(.conversation) }
            }
        )
    }
```

- [ ] **Step 5.4: main+HealthMonitor.swift — privacy-хук** (в `setPrivacyMode(_:)`, ~стр.48)

После `self.applyHealthStateToStatusItem(self.lastHealthState)` добавить:
```swift
        // Wake word не должен держать микрофон в privacy mode.
        // Backend тоже откажет в wake_word_start (гейт живой после проводки
        // settings_get) — это двойная защита, агентская сторона первая.
        if on {
            wakeWordPoller?.pause(.privacyMode)
        } else {
            wakeWordPoller?.resume(.privacyMode)
        }
```

- [ ] **Step 5.5: Удалить мёртвый Porcupine-путь**

```bash
git rm native/KrabEarAgent/Sources/KrabEarAgent/WakeWordListener.swift
git rm native/KrabEarAgent/Tests/KrabEarAgentTests/WakeWordListenerTests.swift
grep -rn "WakeWordListener\|Porcupine" native/KrabEarAgent/Sources/ --include="*.swift"
```
Expected после grep: ноль вхождений `WakeWordListener`; `Porcupine` может остаться только в Settings-тексте (уберёт Task 7).

- [ ] **Step 5.6: Build + тесты трекера**

```bash
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -3 && swift test --filter WakeWordDetectionTrackerTests 2>&1 | tail -3
```
Expected: `Build complete!`; `0 failures`.

- [ ] **Step 5.7: Commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add -A native/KrabEarAgent
git commit -m "feat(wake-word): WakeWordPoller (IPC-поллинг) вместо мёртвого Porcupine

setupWakeWordListenerIfEnabled/applyWakeWordEnabled переведены на
wake_word_start/stop + поллинг wake_word_status (0.75s, off-main,
идиом refreshRealtimeOverlay). Self-heal при перезапуске backend
(rate-limit 10s). Privacy-хук в setPrivacyMode. WakeWordListener.swift
(Porcupine-заглушка, никогда не работала) и его тесты удалены."
```

---

### Task 6: Swift — координация микрофона (запись + разговор)

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+RealtimeOverlay.swift` (стр. ~17 и ~50)
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController.swift` (:119, :130)

- [ ] **Step 6.1: Пауза на запись**

В `startRealtimeOverlayPolling()` после строки `streamingPasteController?.recordingDidStart()` (~стр.17):
```swift
        // Запись владеет микрофоном; иначе wake word ловит собственную диктовку.
        wakeWordPoller?.pause(.recording)
```
В `stopRealtimeOverlayPolling()` после строки `streamingPasteController?.recordingDidStop()` (~стр.50):
```swift
        wakeWordPoller?.resume(.recording)
```
(Обе функции — универсальные хуки записи: streamingPaste-вызовы стоят ДО guard'а `realtimePreviewEnabled`, т.е. выполняются на каждый start/stop записи.)

- [ ] **Step 6.2: Notification'ы разговора**

В `ConversationViewController.swift` над классом (после import'ов):
```swift
// Разговор занимает микрофон: агент ставит wake word на паузу на .started
// и возобновляет на .stopped (см. setupWakeWordConversationObservers в main.swift).
extension Notification.Name {
    static let krabConversationStarted = Notification.Name("com.krabear.agent.conversationStarted")
    static let krabConversationStopped = Notification.Name("com.krabear.agent.conversationStopped")
}
```
Первой строкой тела `func startConversation()` (:119):
```swift
        NotificationCenter.default.post(name: .krabConversationStarted, object: nil)
```
Последней строкой тела `func stopConversation()` (:130, после `conversationState = .idle`):
```swift
        NotificationCenter.default.post(name: .krabConversationStopped, object: nil)
```
(`stopConversation` имеет `guard isSessionActive else { return }` — post не задвоится; пауза Set-идемпотентна в любом случае.)

- [ ] **Step 6.3: Build**

```bash
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -2
```
Expected: `Build complete!`

- [ ] **Step 6.4: Commit**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
git add native/KrabEarAgent/Sources/KrabEarAgent/main+RealtimeOverlay.swift native/KrabEarAgent/Sources/KrabEarAgent/ConversationViewController.swift
git commit -m "feat(wake-word): пауза на время записи и разговора

Запись: хуки в start/stopRealtimeOverlayPolling (универсальные точки
recordingDidStart/Stop). Разговор: notification'ы из единственной воронки
start/stopConversation — покрывает все пути завершения (hotkey, кнопка,
WS-close, ошибки)."
```

---

### Task 7: Swift — Settings UI (статус, модель, порог)

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift` (объявления рядом с `vaWakeWordToggle`)
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift` (`buildVoiceAssistantSection`, хендлеры, sync)

- [ ] **Step 7.1: Объявления контролов**

Найти объявление `vaWakeWordToggle` (`grep -n "vaWakeWordToggle" native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift`) и рядом добавить:
```swift
    let vaWakeWordStatusLabel = NSTextField(labelWithString: "openWakeWord: проверяю…")
    let vaWakeWordModelSelector = NSPopUpButton()
    let vaWakeWordThresholdSlider = NSSlider(value: 0.5, minValue: 0.05, maxValue: 1.0, target: nil, action: nil)
```

- [ ] **Step 7.2: Обновить wake-word ряд + добавить 3 ряда в `buildVoiceAssistantSection()`**

Заменить блок «2. Wake word toggle» (бейдж+ряд, ~стр.1261–1275) на:
```swift
        // 2. Wake word toggle (openWakeWord в backend, IPC-поллинг — spec 2026-07-05)
        vaWakeWordToggle.title = ""
        vaWakeWordToggle.setButtonType(.switch)
        vaWakeWordToggle.setAccessibilityLabel("Включить детектор слова-пробуждения (openWakeWord)")
        let wakePrivacyBadge = makeBadge(
            text: "приватность",
            color: KrabEarTheme.Colors.textSecondary,
            tooltip: "Микрофон слушает только слово-пробуждение локально (openWakeWord, Apache-2.0). Выключается в privacy mode. По умолчанию выключен.",
            symbol: "lock.fill"
        )
        let wakeWordRow = makeSwitchRow(
            label: "Детектор слова-пробуждения",
            description: "openWakeWord в backend — без ключей и регистраций. Скажи слово-пробуждение — откроется «Разговор с AI». По умолчанию выключен — приватность.",
            button: vaWakeWordToggle,
            statusBadge: wakePrivacyBadge
        )

        // 2a. Статус движка
        vaWakeWordStatusLabel.font = NSFont.systemFont(ofSize: 11)
        vaWakeWordStatusLabel.textColor = KrabEarTheme.Colors.textSecondary
        let wakeStatusRow = makeSettingRow(
            label: "Статус",
            description: "Установлен ли openWakeWord в Python-окружении backend.",
            control: vaWakeWordStatusLabel
        )

        // 2b. Модель
        vaWakeWordModelSelector.removeAllItems()
        vaWakeWordModelSelector.addItems(withTitles: ["hey_jarvis", "alexa", "hey_mycroft"])
        vaWakeWordModelSelector.setAccessibilityLabel("Модель слова-пробуждения")
        let savedModel = UserDefaults.standard.string(forKey: "KrabEar_WakeWordModel") ?? "hey_jarvis"
        vaWakeWordModelSelector.selectItem(withTitle: savedModel)
        let wakeModelRow = makeSettingRow(
            label: "Слово-пробуждение",
            description: "Встроенные модели openWakeWord (англ.). Кастомная «Краб» (.onnx в wake_word_models/) появится в списке автоматически.",
            control: vaWakeWordModelSelector
        )

        // 2c. Порог
        vaWakeWordThresholdSlider.numberOfTickMarks = 0
        vaWakeWordThresholdSlider.isContinuous = false
        vaWakeWordThresholdSlider.setAccessibilityLabel("Порог уверенности детектора")
        let savedThreshold = UserDefaults.standard.double(forKey: "KrabEar_WakeWordThreshold")
        vaWakeWordThresholdSlider.doubleValue = savedThreshold > 0 ? savedThreshold : 0.5
        let wakeThresholdRow = makeSettingRow(
            label: "Порог уверенности",
            description: "Ниже — чувствительнее (больше ложных срабатываний), выше — строже. По умолчанию 0.5.",
            control: vaWakeWordThresholdSlider
        )
```
И в сборку карточки после `card.contentStackView.addArrangedSubview(wakeWordRow)` добавить:
```swift
        card.contentStackView.addArrangedSubview(wakeStatusRow)
        card.contentStackView.addArrangedSubview(wakeModelRow)
        card.contentStackView.addArrangedSubview(wakeThresholdRow)
```

- [ ] **Step 7.3: Хендлеры + проводка target/action + обновление статуса**

Рядом с `onVAWakeWordToggleChanged()` добавить:
```swift
    @objc func onVAWakeWordModelChanged() {
        let model = vaWakeWordModelSelector.titleOfSelectedItem ?? "hey_jarvis"
        UserDefaults.standard.set(model, forKey: "KrabEar_WakeWordModel")
        restartWakeWordIfEnabled()
    }

    @objc func onVAWakeWordThresholdChanged() {
        UserDefaults.standard.set(vaWakeWordThresholdSlider.doubleValue, forKey: "KrabEar_WakeWordThreshold")
        restartWakeWordIfEnabled()
    }

    /// Смена модели/порога на лету: пере-старт сессии, если тумблер включён.
    private func restartWakeWordIfEnabled() {
        guard UserDefaults.standard.bool(forKey: "KrabEar_WakeWordEnabled"),
              let appDelegate = NSApp.delegate as? AgentAppDelegate else { return }
        appDelegate.wakeWordPoller?.deactivate()
        appDelegate.setupWakeWordListenerIfEnabled()
    }

    /// Off-main запрос wake_word_status для статус-строки и списка моделей.
    func refreshWakeWordStatusRow() {
        let ipc = self.ipcClient
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let status = try? ipc.call(method: "wake_word_status", params: [:])
            let models = try? ipc.call(method: "wake_word_list_models", params: [:])
            DispatchQueue.main.async {
                guard let self else { return }
                let result = status?["result"] as? [String: Any]
                let available = result?["engine_available"] as? Bool ?? false
                self.vaWakeWordStatusLabel.stringValue = available
                    ? "openWakeWord: установлен"
                    : "openWakeWord: не установлен — pip install -r KrabEar/requirements-wakeword.txt"
                if let list = (models?["result"] as? [String: Any])?["models"] as? [[String: Any]] {
                    let names = list.compactMap { $0["name"] as? String }
                    if !names.isEmpty {
                        let selected = self.vaWakeWordModelSelector.titleOfSelectedItem
                        self.vaWakeWordModelSelector.removeAllItems()
                        self.vaWakeWordModelSelector.addItems(withTitles: names)
                        if let selected, names.contains(selected) {
                            self.vaWakeWordModelSelector.selectItem(withTitle: selected)
                        }
                    }
                }
            }
        }
    }
```
Проводка (там же, где wired `vaWakeWordToggle` — найти `grep -n "vaWakeWordToggle.target\|vaWakeWordToggle.action" native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift` и рядом добавить):
```swift
        vaWakeWordModelSelector.target = self
        vaWakeWordModelSelector.action = #selector(onVAWakeWordModelChanged)
        vaWakeWordThresholdSlider.target = self
        vaWakeWordThresholdSlider.action = #selector(onVAWakeWordThresholdChanged)
```
И в `syncSettingsControls()` рядом с чтением `KrabEar_WakeWordEnabled` (~стр.1357) добавить вызов:
```swift
        refreshWakeWordStatusRow()
```

- [ ] **Step 7.4: Build + глиф-гейт**

```bash
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -2
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear"
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_swift_no_unicode_glyphs_wave621.py KrabEar/tests/test_swift_no_unicode_glyphs_wave658.py -q -p no:cacheprovider
```
Expected: `Build complete!`; глиф-тесты PASS.

- [ ] **Step 7.5: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+Settings.swift
git commit -m "feat(wake-word): Settings — статус движка, выбор модели, порог

Porcupine-тексты убраны. Статус/список моделей тянутся off-main через
wake_word_status/wake_word_list_models. Смена модели/порога на лету
пере-стартует сессию."
```

---

### Task 8: Зависимость + bootstrap

**Files:**
- Create: `KrabEar/requirements-wakeword.txt`
- Modify: `scripts/bootstrap_backend.command`

- [ ] **Step 8.1: `KrabEar/requirements-wakeword.txt`**

```text
# Опциональная зависимость wake word (детектор слова-пробуждения, openWakeWord).
# НАМЕРЕННО не в requirements.txt: ubuntu-CI ставит requirements.txt целиком,
# а wake word нужен только на macOS-машине пользователя (адаптер имеет stub-режим).
# Установка:
#   .venv_krab_ear/bin/pip install -r KrabEar/requirements-wakeword.txt
#   .venv_krab_ear/bin/python -c "import openwakeword.utils as u; u.download_models()"
openwakeword>=0.6.0
```

- [ ] **Step 8.2: bootstrap_backend.command**

После строки `log "Зависимости: ок"` добавить:
```bash
# Опционально: openWakeWord (детектор слова-пробуждения). Сбой НЕ фатален.
if "$VENV/bin/pip" install -r "$INSTALL_DIR/KrabEar/requirements-wakeword.txt"; then
  # Базовые модели скачиваются один раз с GitHub (dscripka/openWakeWord) —
  # HF_HUB_OFFLINE на них не влияет; без этого первый старт детектора упрётся
  # в отсутствующие файлы моделей.
  "$VENV/bin/python" -c "import openwakeword.utils as u; u.download_models()" \
    || warn "openWakeWord: модели не скачались — детектор попробует при первом запуске"
  log "openWakeWord: ок"
else
  warn "openWakeWord не установился — детектор слова-пробуждения будет недоступен (опционально)"
fi
```

- [ ] **Step 8.3: Синтакс-чек + dry-run**

```bash
bash -n scripts/bootstrap_backend.command && bash scripts/bootstrap_backend.command --dry-run
```
Expected: dry-run печатает план, exit 0 (новый блок в dry-run не выполняется — он после `exit 0` dry-run ветки; проверить glазами, что блок стоит ПОСЛЕ dry-run выхода — да, он в секции 4 после установки зависимостей).

- [ ] **Step 8.4: Commit**

```bash
git add KrabEar/requirements-wakeword.txt scripts/bootstrap_backend.command
git commit -m "feat(wake-word): опциональная зависимость openwakeword + bootstrap-доустановка"
```

---

### Task 9: Документация

**Files:**
- Modify: `docs/IPC_API_REFERENCE.md` — в описании `wake_word_status` добавить поле ответа:
```markdown
- `last_detection` (object|null): последняя детекция `{model: str, score: float, ts: float}`;
  `ts` — МОНОТОННЫЙ (time.monotonic) таймстамп процесса backend, агент дебаунсит по росту.
  Сбрасывается в null при wake_word_start/stop. Добавлено 2026-07-05 (spec wake-word-openwakeword).
```
- Modify: `CLAUDE.md` — две правки:
  1. Строку `- **WakeWordListener.swift** — openWakeWord adapter bridge (Swift↔Python); triggers recording on wake-word detection; hotkey remains primary fallback.` заменить на:
```markdown
- **`WakeWordPoller.swift`** — wake word через IPC-поллинг backend (openWakeWord): агент шлёт `wake_word_start/stop`, поллит `wake_word_status` (0.75s), рост `last_detection.ts` → «Разговор с AI». Пауза на запись/разговор/privacy (Set причин). Porcupine-путь (`WakeWordListener.swift`) УДАЛЁН 2026-07-05 — никогда не работал (заглушка без SDK). 🔴 SSE для этого НЕ подходит: прод = 2 процесса (service.py IPC + rest_server.py :5005) с РАЗДЕЛЬНЫМИ EventBus без моста.
```
  2. В строке про `backend/openwakeword_adapter.py` дописать в конец: `Настоящий движок с 2026-07-05 (Porcupine удалён); last_detection в wake_word_status для поллинга агента; settings_get проброшен из service.py (до этого privacy-гейт был декоративным).`
- Modify: `docs/USER_MANUAL.md` — `grep -n "Porcupine\|пробужден" docs/USER_MANUAL.md`; заменить упоминания Porcupine/AccessKey/.ppn на:
```markdown
Детектор слова-пробуждения работает на openWakeWord (локально, без ключей и
регистраций). Включается в Настройки → «Разговор с AI» → «Детектор
слова-пробуждения». Требуется однократная установка в окружение backend:
`.venv_krab_ear/bin/pip install -r KrabEar/requirements-wakeword.txt` (bootstrap-
инсталлятор делает это сам). Скажи «hey jarvis» (или выбранную модель) — откроется
«Разговор с AI». Детектор автоматически ставится на паузу во время диктовки,
разговора и в privacy mode.
```

- [ ] **Step 9.1: Внести правки** (по блокам выше)
- [ ] **Step 9.2: Дрифт-чекер CLAUDE.md**
```bash
python3 scripts/verify_claude_md.py | tail -3
```
Expected: OK/без ошибок.
- [ ] **Step 9.3: Commit**
```bash
git add docs/IPC_API_REFERENCE.md CLAUDE.md docs/USER_MANUAL.md
git commit -m "docs(wake-word): контракт last_detection + замена Porcupine-упоминаний"
```

---

### Task 10: PR + CI

- [ ] **Step 10.1: Финальный локальный гейт**

```bash
PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_wake_word_polling_contract.py KrabEar/tests/test_openwakeword_adapter.py KrabEar/tests/test_openwakeword_security_W1210.py KrabEar/tests/test_wave35_wakeword_misc.py -q -p no:cacheprovider
make audit-orphans && python3 scripts/audit_dead_extracted_modules.py --fail-on-found | tail -2
cd native/KrabEarAgent && swift build -c release 2>&1 | tail -2 && cd ../..
```
Expected: все PASS; аудиты чистые; build OK.

- [ ] **Step 10.2: Push + PR**

```bash
git push -u origin feature/wake-word-openwakeword
gh pr create --base codex/krab-ear-v2 --title "feat(wake-word): живой детектор слова-пробуждения (openWakeWord, IPC-поллинг)" --body "Spec: docs/superpowers/specs/2026-07-05-wake-word-openwakeword-design.md. Поркьюпайн-заглушка удалена; poller wake_word_status 0.75s; last_detection в адаптере; фикс декоративного settings_get; пауза на запись/разговор/privacy; Settings UI; опциональная зависимость + bootstrap. 🤖 Generated with [Claude Code](https://claude.com/claude-code)"
gh pr checks --watch --fail-fast   # (в фоне: run_in_background)
```
Expected: 20/20 SUCCESS (swift-build теперь собирает без WakeWordListener).

- [ ] **Step 10.3: Merge по зелёному**

```bash
gh pr merge --merge --delete-branch
```

---

### Task 11: Деплой + живой смок

- [ ] **Step 11.1: Зависимость в прод-venv + модели**

```bash
cd "/Users/pablito/Antigravity_AGENTS/Krab Ear" && git checkout codex/krab-ear-v2 && git pull --ff-only
.venv_krab_ear/bin/pip install -r KrabEar/requirements-wakeword.txt
.venv_krab_ear/bin/python -c "import openwakeword.utils as u; u.download_models(); print('models OK')"
```
Expected: установка OK; `models OK`.

- [ ] **Step 11.2: Рестарт backend (Python-изменения)**

```bash
launchctl kickstart -k gui/$(id -u)/ai.krab.ear.backend
```
Затем ping-проверка сокета (метод `ping`) — `ok:true`.

- [ ] **Step 11.3: Swift деплой** (меняется main.swift и т.д.)

```bash
./scripts/build_and_deploy.command --no-sentry
launchctl bootout gui/$(id -u)/ai.krab.ear.agent; sleep 2
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.krab.ear.agent.plist
pgrep -f "Krab Ear.app/Contents/MacOS/KrabEarAgent"
```
Expected: UUID match в деплое; агент поднялся (pid). (kickstart для агента НЕ использовать — spawn-failed рецепт.)

- [ ] **Step 11.4: Parity-коммит бинарей**

```bash
git add "Krab Ear.app/Contents/MacOS/KrabEarAgent"; git add -f native/runtime/KrabEarAgent
git commit -m "build(native): parity-коммит бинарей после мержа wake-word волны"
git push origin codex/krab-ear-v2
```

- [ ] **Step 11.5: Живой смок-чеклист**

1. Тумблер «Детектор слова-пробуждения» ON → статус «openWakeWord: установлен»; IPC `wake_word_status` → `running:true, active_model:"hey_jarvis"`.
2. Сказать «hey jarvis» → agent.log `[WakeWord] Детекция — запускаю разговор` → открылась вкладка «Разговор с AI».
3. Начать диктовку (Right Option) → `wake_word_status.running:false` (пауза); закончить → снова `true`.
4. Включить privacy mode → `running:false`, повторный `wake_word_start` руками → `{ok:false, reason:...}` (гейт живой). Выключить → восстановился.
5. `launchctl kickstart -k ...backend` при включённом тумблере → в течение ~10с `running:true` снова (self-heal), детекция НЕ выстрелила сама (nil re-arm).

---

## Self-Review (выполнен при написании)

- **Spec coverage:** все секции спеки → задачи: last_detection+status (T1), settings_get+privacy-loop (T2), tracker+poller+IPC (T4–5), удаление Porcupine (T5), координация микрофона: запись/разговор/privacy (T5–6), UI (T7), зависимость+bootstrap (T8), доки (T9), деплой+смок (T11). Отклонение от спеки одно и задокументировано: `Start Krab Ear.command` не патчится (проверено — нет pip-секции), покрытие bootstrap+deploy+docs.
- **Placeholder scan:** каждый код-шаг содержит полный код; «найди по grep»-якоря везде сопровождены точными командами и полным вставляемым кодом.
- **Type consistency:** `WakeWordPauseReason` единый (recording/conversation/privacyMode); `shouldTrigger(lastDetectionTs:)` единственная сигнатура; UD-ключи `KrabEar_WakeWordModel`/`KrabEar_WakeWordThreshold` согласованы между поллером (T5) и UI (T7); контракт `last_detection {model, score, ts}` согласован backend (T1) ↔ poller (T5) ↔ docs (T9); nil-re-arm трекера ↔ сброс start() ↔ смок п.5.
