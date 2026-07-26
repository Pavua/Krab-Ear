# R2 «Владение записью» — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать владение общей записью явным и проверяемым, чтобы чужой потребитель не мог остановить не свою запись, а прерванная транспортом остановка не теряла результат.

**Architecture:** Поколение записи (`token` + `owner` + `state`) заводится при старте под уже существующим `_recording_lifecycle_lock`; гейт остановки — первая операция в `phase_a_locked`, решает по токену (не по кэшу); явная матрица переходов владения; кэш терминальных ответов для replay; Swift-сторона перестаёт трогать чужую запись и получает единый ретрай-helper. Спека: `docs/superpowers/specs/2026-07-25-r2-recording-ownership-design.md`.

**Tech Stack:** Python 3.14 (`.venv_krab_ear`), unittest/pytest, Swift 6 (swift-tools 6.0, macOS 13+), Unix-socket JSON-RPC.

## Global Constraints

- Тесты Python: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/<file> -v -p no:cacheprovider` из корня репо; venv `source .venv_krab_ear/bin/activate`.
- Каждый тест, создающий `BackendService(...)`, ОБЯЗАН звать `service.close()` в tearDown (правило #1782). Тесты, создающие его на temp-директории, используют `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)` (установленная конвенция, см. `test_backend_service.py`).
- Swift: `cd native/KrabEarAgent && swift build -c release`; тесты `swift test`.
- Новые тест-файлы Python — через `scripts/pre_merge_py312_check.sh <файлы>` (ubuntu-parity, ТОЛЬКО тест-файлы).
- flake8 по CI-команде (W293 в тестах НЕ расслаблен), `make audit-all` перед финишем.
- НИКАКОГО I/O под `AudioRecorder._lock` (класс W1652/F3) и никаких локов/I/O/логирования в signal-callback.
- Коммиты явными путями (`git add <files>`), НИКОГДА `git add -A`.
- Swift-глифы: любой новый non-ASCII символ грепать по `native/` — если 0 вхождений, заменить установленным (CoreText-hang AGENT-J/M).
- **Никакие изменения не должны отвергать запросы без owner/token** — старый бинарь агента против нового backend обязан продолжать работать (two-binary drift).
- `recording_owner_enforce` по умолчанию `False`; токенные инварианты действуют в ОБОИХ режимах безусловно.

---

## Чанки исполнения

- **Чанк A** (Task 1) — проверенный кодовый checkpoint безопасности, но **не
  самостоятельный production deploy**. Разбор выявил остаточное TOCTOU-окно между
  клиентскими `get_recording_state` и `stop_recording`; выкладка разрешена только
  после серверного token/generation gate из Tasks 2–3.
- **Чанк B** (Task 2-4) — ядро: поколение, гейт, матрица переходов.
- **Чанк C** (Task 5-6) — кэш ответов и телеметрия владения.
- **Чанк D** (Task 7-8) — Swift-ретрай и закрытие волны.

> **Гейт частичной выкладки:** Tasks 2–6 можно коммитить и тестировать
> изолированно, но нельзя отдельно merge/deploy. До Task 7 (Swift сохраняет
> token и делает bounded retry stop) и Task 8 (cross-binary e2e)
> `recorder_timeout` намеренно удерживает G1; legacy UI после него может увидеть
> только `recorder_stopping` и не умеет завершить recovery-цикл. Допустима лишь
> атомарная выкладка полного R2 после всех гейтов.

---

## Чанк A — живой фикс безопасности

### Task 1: Хоткей не трогает чужую запись (F1)

**Files:**
- Modify: `KrabEar/backend/recording_core_service.py`, `recorder.py`,
  `meeting_session_service.py`, `service.py`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+HotkeyRecording.swift`,
  `main+QuickCapture.swift`, `main.swift`
- Test: `KrabEar/tests/test_recording_owner_state.py` (новый)
- Test: `KrabEar/tests/test_recording_core_service.py`,
  `test_recording_spill_wiring.py`, `test_recorder_spill_integration.py`,
  `test_meeting_session_service_W_C2a.py`,
  `test_meeting_dispatch_privacy_W_C2a.py`
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/HotkeyOwnerGuardTests.swift` (новый)
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/MainHotkeyRecordingTests.swift`

**Interfaces:**
- Produces (для Task 2): поля `self._active_owner: str | None` и
  `self._active_owner_revision: int` на `RecordingCoreService` — минимальное
  отслеживание владельца и CAS-revision для безопасной компенсации promote.
  **Task 2 поглощает оба поля**, заменяя их полноценным
  `self._active_generation`; Task 2 обязан сохранить revision-bound семантику.
- Produces (для Task 7): `get_recording_state` возвращает дополнительное поле `owner: str | None`.

**Контекст задачи (зачем):** сегодня при идущей встрече одиночный тап Right Option попадает в ветку «лечения рассинхрона» (`main+HotkeyRecording.swift:61-71`) и останавливает запись встречи: отчёт теряется (`item_id: None`), а транскрипт часа уходит в `pasteToFrontmostApp`. Плюс `already_recording` в хоткее (`:118-126`) считается успехом.

> **Checkpoint 2026-07-26 — Task 1 code-complete, deploy paused.**
> Adversarial-разбор расширил исходный F1 до полного lifecycle-контракта:
> owner публикуется атомарно со start/stop; promote откатывается revision-CAS;
> fresh-start и shutdown сохраняют retry-handles; `AudioRecorder.start()` стал
> failure-atomic; normal stop атомарно забирает spill; зависшие recorder,
> preview, partial/RSF и meeting-worker не маскируются флагом `is_recording`.
> Swift fail-closed различает отсутствующее поле owner (старый backend) и
> явный `null` (unmanaged recording), а все stop-ветки проходят один owner-guard.
>
> Проверено: Python 3.14 — 135/135 доменных + 5/5 BackendService/dispatch;
> Ubuntu-parity Python 3.12 без MLX — 141/141; Swift release build — OK;
> Swift suite — 1348 executed, 12 skipped, 0 failed; flake8 и
> `git diff --check` — чисто; независимый adversarial gate — GREEN.
> Живой production smoke намеренно не выполнялся: token-gate ещё отсутствует,
> а production-рекордер удерживает rescue `.part` после PortAudio `-9986`.

- [x] **Step 1: Написать падающий Python-тест**

Создать `KrabEar/tests/test_recording_owner_state.py`. Фейк-коллабораторы скопировать из `KrabEar/tests/test_recording_spill_wiring.py` (прочитать его `_FakeRecorder`, `_make_service` и переиспользовать структуру — НЕ изобретать свою).

```python
"""owner в get_recording_state (R2 Task 1) — живой фикс F1."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# _FakeRecorder / _make_service — скопировать из test_recording_spill_wiring.py


class RecordingOwnerStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        self._tmp = Path(self._tmp_ctx.name)
        self.rescue_dir = self._tmp / "rescue"

    def test_owner_is_none_when_idle(self):
        svc = _make_service(self._tmp, self.rescue_dir, recorder=_FakeRecorder())
        state = svc.handle_get_recording_state({})
        self.assertIsNone(state["owner"])

    def test_owner_defaults_to_dictation(self):
        svc = _make_service(self._tmp, self.rescue_dir, recorder=_FakeRecorder())
        svc.handle_start_recording({})
        self.assertEqual(svc.handle_get_recording_state({})["owner"], "dictation")

    def test_owner_reflects_explicit_source(self):
        svc = _make_service(self._tmp, self.rescue_dir, recorder=_FakeRecorder())
        svc.handle_start_recording({"source": "meeting"})
        self.assertEqual(svc.handle_get_recording_state({})["owner"], "meeting")

    def test_owner_cleared_after_stop(self):
        svc = _make_service(self._tmp, self.rescue_dir, recorder=_FakeRecorder())
        svc.handle_start_recording({})
        svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertIsNone(svc.handle_get_recording_state({})["owner"])


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Убедиться, что падает правильно**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_recording_owner_state.py -v -p no:cacheprovider`
Expected: FAIL с `KeyError: 'owner'`

- [x] **Step 3: Реализовать backend-часть**

В `RecordingCoreService.__init__` рядом с `self._active_spill: Any = None` добавить:

```python
        # R2 Task 1: минимальное отслеживание владельца записи для живого фикса F1
        # (хоткей не должен останавливать чужую запись). Task 2 заменяет это поле
        # полноценным self._active_generation — здесь оно введено, чтобы фикс
        # безопасности шипился первым, не дожидаясь всей архитектуры владения.
        self._active_owner: str | None = None
```

В `_handle_start_recording_locked` сразу после `self._active_spill = spill` (строка ~331 после R1-правок) добавить:

```python
        self._active_owner = str(params.get("source", "dictation"))
```

В ветке `if not started:` НИЧЕГО про `_active_owner` не делать (по прецеденту R1 HIGH-2: ветка not-started не трогает состояние живой чужой записи).

В `handle_stop_recording` рядом с существующим `spill = getattr(self, "_active_spill", None); self._active_spill = None` добавить сброс:

```python
        self._active_owner = None
```

В `handle_get_recording_state` в возвращаемый dict добавить:

```python
            "owner": getattr(self, "_active_owner", None),
```

- [x] **Step 4: Зелёные тесты + регрессия**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_recording_owner_state.py KrabEar/tests/test_recording_core_service.py KrabEar/tests/test_recording_spill_wiring.py -v -p no:cacheprovider`
Expected: все PASS

- [x] **Step 5: Написать падающий Swift-тест**

Создать `native/KrabEarAgent/Tests/KrabEarAgentTests/HotkeyOwnerGuardTests.swift`. Прочитать существующий `QuickCaptureWiringTests.swift` для установленного в проекте паттерна source-contract тестов и переиспользовать его.

```swift
import XCTest
@testable import KrabEarAgent

/// R2 Task 1 (F1): хоткей не должен останавливать чужую запись.
/// Source-contract: проверяем, что ветка auto-heal гейтится владельцем.
final class HotkeyOwnerGuardTests: XCTestCase {

    private func hotkeySource() throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // KrabEarAgentTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // KrabEarAgent
            .appendingPathComponent("Sources/KrabEarAgent/main+HotkeyRecording.swift")
        return try String(contentsOf: url, encoding: .utf8)
    }

    func test_autoHeal_is_gated_by_owner() throws {
        let src = try hotkeySource()
        XCTAssertTrue(
            src.contains("backendOwner"),
            "Ветка лечения рассинхрона обязана учитывать владельца записи (F1)"
        )
    }

    func test_already_recording_is_not_treated_as_success() throws {
        let src = try hotkeySource()
        XCTAssertFalse(
            src.contains("isRecording = true\n                startRealtimeOverlayPolling()"),
            "already_recording больше не должен считаться успешным стартом (зеркало C3a-фикса)"
        )
    }
}
```

- [x] **Step 6: Убедиться, что Swift-тесты падают**

Run: `cd native/KrabEarAgent && swift test --filter HotkeyOwnerGuardTests`
Expected: FAIL обоих кейсов

- [x] **Step 7: Реализовать Swift-часть**

7a. `syncRecordingStateWithBackend()` (строка 80) — вернуть не только флаг, но и владельца. Заменить сигнатуру и тело:

```swift
    /// Возвращает (пишет ли backend, владелец записи).
    /// owner == nil означает: либо записи нет, либо backend СТАРЫЙ и поля не отдаёт
    /// (агент пересобран, backend не кикстартнут). Отличать эти случаи по is_recording.
    func syncRecordingStateWithBackend() -> (recording: Bool, owner: String?) {
        guard
            let stateResponse = try? callWithRecovery(method: "get_recording_state", params: [:]),
            let state = stateResponse["result"] as? [String: Any]
        else {
            return (isRecording, nil)
        }

        let backendRecording = (state["is_recording"] as? Bool) ?? false
        let backendOwner = state["owner"] as? String
        if backendRecording != isRecording {
            isRecording = backendRecording
            refreshStatusItemTitle()
            rebuildStatusMenu()
        }
        return (backendRecording, backendOwner)
    }
```

7b. В `performRecordToggle` заменить первую строку и ветку auto-heal:

```swift
        let (backendRecording, backendOwner) = syncRecordingStateWithBackend()
        if backendRecording != wasRecordingLocally {
            logger.warn("Десинхрон состояния записи: local=\(wasRecordingLocally), backend=\(backendRecording), owner=\(backendOwner ?? "nil")")
        }
```

и ветку `if !wasRecordingLocally && backendRecording` целиком заменить на:

```swift
        // Если backend пишет, а локально флаг был сбит — раньше мы безусловно
        // «лечили рассинхрон», останавливая запись. При активной встрече это
        // убивало её отчёт (item_id: None) и вставляло час транскрипта в активное
        // окно (F1). Теперь heal разрешён только для СВОЕЙ записи.
        // Отсутствие owner = старый backend → heal разрешён (сегодняшнее поведение),
        // иначе зависшую диктовку станет нечем добить (two-binary drift).
        if !wasRecordingLocally && backendRecording {
            if let owner = backendOwner, owner != "dictation" {
                let human = owner == "meeting" ? "встреча" : "быстрая заметка"
                await MainActor.run {
                    self.notify(
                        title: "Krab Ear",
                        body: "Идёт \(human) — запись не тронута."
                    )
                }
                return
            }
            await MainActor.run {
                self.notify(
                    title: "Krab Ear",
                    body: "Найден рассинхрон записи. Сначала завершаю зависшую сессию."
                )
            }
            stopRecording()
            return
        }
```

7c. В `startRecording()` заменить блок `if status == "already_recording"` (строка ~118) на отказ с откатом ducking. Прочитать строки 100-135 целиком: ducking включается ДО старта (строка ~111) и восстанавливается только в defer стопа — под чужой записью его надо восстановить здесь же, иначе системный звук зальёт микрофон встречи. Точное имя метода восстановления взять из существующего defer в `stopRecording()` (`grep -n "ducking" native/KrabEarAgent/Sources/KrabEarAgent/main+HotkeyRecording.swift`).

```swift
            if status == "already_recording" {
                // Зеркало C3a-фикса для быстрой заметки: already_recording — НЕ успех.
                // Раньше агент выставлял isRecording = true и следующий тап слал стоп,
                // останавливая чужую (например, встречи) запись.
                logger.warn("start_recording: запись уже идёт — не перехватываем")
                restoreSystemAudioAfterRecording()   // точное имя — из defer в stopRecording()
                notify(title: "Krab Ear", body: "Запись уже идёт — новая не начата.")
                return
            }
```

- [x] **Step 8: Зелёные Swift-тесты + полная сборка**

Run: `cd native/KrabEarAgent && swift build -c release && swift test`
Expected: сборка OK, вся сьюта зелёная (существующие тесты, зависящие от `syncRecordingStateWithBackend`, могли сломаться из-за смены сигнатуры — починить их под новый кортеж, это ожидаемая часть задачи)

- [x] **Step 9: Гейты и коммит**

```bash
scripts/pre_merge_py312_check.sh \
  KrabEar/tests/test_recording_owner_state.py \
  KrabEar/tests/test_recording_spill_wiring.py \
  KrabEar/tests/test_meeting_session_service_W_C2a.py \
  KrabEar/tests/test_recorder_spill_integration.py \
  KrabEar/tests/test_recording_core_service.py \
  KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py
make audit-all
cd native/KrabEarAgent && swift build -c release && swift test
# Стадировать только явный список файлов checkpoint; git add -A запрещён.
git commit -m "fix(r2): хоткей не останавливает чужую запись + already_recording не успех (Task 1, F1)"
```

---

## Чанк B — ядро владения

### Task 2: Поколение записи (токен + владелец + spill-интеграция)

**Files:**
- Modify: `KrabEar/backend/recording_spill.py` (`RecordingSpillWriter.__init__`)
- Modify: `KrabEar/backend/recording_core_service.py` (`__init__`,
  `_handle_start_recording_locked`, `handle_get_recording_state`, phase A,
  owner-bound shutdown)
- Test: `KrabEar/tests/test_recording_generation.py` (новый)
- Test: `KrabEar/tests/test_recording_owner_state.py` (CAS/atomic-state/retry)
- Test: `KrabEar/tests/test_recording_core_service.py` (degraded start +
  восстановление восьми ранее не собиравшихся helper-тестов)

**Interfaces:**
- Consumes: `self._active_owner` и `self._active_owner_revision` из Task 1
  (ПОГЛОЩАЮТСЯ — удаляются, заменяются на `_active_generation`; CAS-revision
  должна стать частью generation/token-перехода, а не исчезнуть).
- Produces (для Task 3-7):
  - `self._active_generation: dict | None` со схемой `{"token": str, "owner": str, "state": "capturing"|"finalizing", "started_at": float, "promoted_from": str | None, "revision": int}`; revision и монотонный `_generation_revision` сохраняют CAS-контракт Task 1.
  - `RecordingSpillWriter(rescue_dir, sample_rate, channels, source="unknown", session_id=None)` — новый последний параметр; при `None` генерирует свой uuid как сейчас.
  - `handle_start_recording` возвращает в ответе `generation_token: str` и `owner: str`.
  - `handle_get_recording_state` возвращает `owner` и `generation_token`.

- [x] **Step 1: Написать падающий тест**

Создать `KrabEar/tests/test_recording_generation.py` (фейки — из `test_recording_spill_wiring.py`):

```python
"""Поколение записи: токен, владелец, единство с spill (R2 Task 2)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_spill import RecordingSpillWriter  # noqa: E402
# _FakeRecorder / _make_service — скопировать из test_recording_spill_wiring.py


class SpillWriterSessionIdTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        self.rescue_dir = Path(self._tmp_ctx.name) / "rescue"

    def test_accepts_external_session_id(self):
        w = RecordingSpillWriter(rescue_dir=self.rescue_dir, sample_rate=16000,
                                 channels=1, source="dictation", session_id="tok-123")
        self.assertEqual(w.session_id, "tok-123")
        self.assertTrue(w.part_path.name.startswith("tok-123"))

    def test_generates_own_id_when_omitted(self):
        w = RecordingSpillWriter(rescue_dir=self.rescue_dir, sample_rate=16000,
                                 channels=1, source="dictation")
        self.assertTrue(w.session_id)


class RecordingGenerationTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        self._tmp = Path(self._tmp_ctx.name)
        self.rescue_dir = self._tmp / "rescue"

    def test_start_creates_generation_and_returns_token(self):
        svc = _make_service(self._tmp, self.rescue_dir, recorder=_FakeRecorder(),
                            settings_overrides={"recording_spill_enabled": True})
        resp = svc.handle_start_recording({})
        self.assertEqual(resp["status"], "recording")
        self.assertTrue(resp["generation_token"])
        self.assertEqual(resp["owner"], "dictation")
        gen = svc._active_generation
        self.assertEqual(gen["state"], "capturing")
        self.assertEqual(gen["token"], resp["generation_token"])
        self.assertIsNone(gen["promoted_from"])

    def test_generation_token_is_spill_session_id(self):
        """F7: токен поколения и имя rescue-файла — одна сущность."""
        recorder = _FakeRecorder()
        svc = _make_service(self._tmp, self.rescue_dir, recorder=recorder,
                            settings_overrides={"recording_spill_enabled": True})
        resp = svc.handle_start_recording({})
        self.assertEqual(recorder.received_spill.session_id, resp["generation_token"])

    def test_state_exposes_token_and_owner(self):
        svc = _make_service(self._tmp, self.rescue_dir, recorder=_FakeRecorder())
        resp = svc.handle_start_recording({"source": "meeting"})
        state = svc.handle_get_recording_state({})
        self.assertEqual(state["owner"], "meeting")
        self.assertEqual(state["generation_token"], resp["generation_token"])


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: Verify FAIL** (`TypeError: __init__() got an unexpected keyword argument 'session_id'`)

- [x] **Step 3: Реализация**

3a. `recording_spill.py`, `RecordingSpillWriter.__init__` — заменить генерацию id:

```python
    def __init__(self, rescue_dir: Path, sample_rate: int, channels: int,
                 source: str = "unknown", session_id: "str | None" = None) -> None:
        # R2 F7: токен поколения записи и имя rescue-файла — ОДНА сущность, чтобы
        # сообщение клиента «восстановится при следующем запуске» указывало на
        # конкретный файл. При session_id=None поведение прежнее (свой uuid).
        self.session_id = session_id or uuid.uuid4().hex
```

3b. `recording_core_service.py.__init__` — УДАЛИТЬ `self._active_owner` (Task 1) и добавить:

```python
        # R2: поколение записи — идентичность цикла «старт → терминальный ответ».
        # Схема: {"token", "owner", "state": capturing|finalizing, "started_at",
        # "promoted_from", "revision"}. Живёт под _recording_lifecycle_lock.
        self._active_generation: "dict[str, Any] | None" = None
        self._generation_revision = 0
```

3c. `_handle_start_recording_locked` — токен генерится ПЕРВЫМ, до создания spill, и передаётся в него. Заменить блок создания spill (после R1-правок он начинается с `spill = None`):

```python
        import uuid as _uuid
        _generation_token = _uuid.uuid4().hex
        _owner = str(params.get("source", "dictation"))

        spill = None
        _rescue_dir = getattr(self, "_rescue_dir", None)
        if _rescue_dir is not None and bool(
            _settings_pre.get("recording_spill_enabled", True)
        ):
            try:
                from backend.recording_spill import RecordingSpillWriter
                spill = RecordingSpillWriter(
                    rescue_dir=_rescue_dir,
                    sample_rate=int(getattr(self.recorder, "sample_rate", 16000)),
                    channels=int(getattr(self.recorder, "channels", 1)),
                    source=_owner,
                    session_id=_generation_token,
                )
                if not spill.open():
                    spill = None
            except Exception:
                logger.warning("RecordingSpill: не удалось создать writer — "
                               "запись продолжается без spill", exc_info=True)
                spill = None
```

После `self._active_spill = spill` публиковать generation через единый helper,
который выдаёт монотонную CAS-revision:

```python
        generation = self._publish_active_generation_locked(
            token=_generation_token,
            owner=_owner,
        )
```

Фактический helper также записывает `revision` через
`_next_generation_revision_locked`; прямое присваивание dict здесь запрещено,
иначе CAS Task 1 тихо исчезнет. В успешный ответ старта добавить
`"generation_token": generation["token"]`, `"owner": generation["owner"]` и
`"owner_revision": generation["revision"]`.

3d. `handle_get_recording_state` — заменить строку с `owner` из Task 1 на:

```python
        _gen = getattr(self, "_active_generation", None)
```

и в возвращаемый dict:

```python
            "owner": (_gen or {}).get("owner"),
            "generation_token": (_gen or {}).get("token"),
```

3e. **Уточнение после Task 1:** простой сброс в outer
`handle_stop_recording` запрещён. Он позволил бы хвосту stop G1 стереть G2,
стартовавшую после физической остановки G1. В Task 2 generation снимается
под lifecycle-lock в phase A вместе с подтверждённым physical stop; при
`recorder_timeout` сохраняется как retry-handle. Shutdown снимает её только
после подтверждённой остановки recorder и всех retained worker-ов.

```python
        self._clear_active_generation_locked()
```

- [x] **Step 4: Зелёные + регрессия**

Run: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/KrabEar" python -m pytest
KrabEar/tests/test_recording_generation.py
KrabEar/tests/test_recording_owner_state.py KrabEar/tests/test_recording_spill.py
KrabEar/tests/test_recording_spill_wiring.py
KrabEar/tests/test_recording_core_service.py
KrabEar/tests/test_recording_rescue.py
KrabEar/tests/test_recorder_spill_integration.py
KrabEar/tests/test_meeting_session_service_W_C2a.py --timeout=60
-p no:cacheprovider -p no:xdist -q`

Evidence 2026-07-26: **182/182 PASS** на macOS. Отдельно проверены
детерминированные межпоточные окна G1/G2, stale CAS, `recorder_timeout`,
retained shutdown, privacy+spill-off, collision/partial-unlink и post-capture
`TypeError`. Старый `owner_revision`-consumer MeetingSession остался зелёным.

- [x] **Step 5: Гейты и коммит**

```bash
scripts/pre_merge_py312_check.sh \
  KrabEar/tests/test_recording_generation.py \
  KrabEar/tests/test_recording_owner_state.py \
  KrabEar/tests/test_recording_spill.py \
  KrabEar/tests/test_recording_spill_wiring.py \
  KrabEar/tests/test_recorder_spill_integration.py \
  KrabEar/tests/test_recording_rescue.py \
  KrabEar/tests/test_meeting_session_service_W_C2a.py
flake8 KrabEar/backend/recording_spill.py \
  KrabEar/backend/recording_core_service.py \
  KrabEar/tests/test_recording_generation.py
# Стадировать только явный список Task 2; git add -A запрещён.
git commit -m "feat(r2): поколение записи — токен, владелец, единство с spill (Task 2)"
```

Evidence 2026-07-26: полный parity-набор Python 3.12 без MLX GREEN; после
финального cleanup-hardening изменённые generation+spill **31/31 PASS**.
`flake8`, `git diff --check` и два независимых read-only adversarial-аудита
GREEN. Swift/MLX/прод не запускались и не перезапускались: Task 2 не меняет
Swift, а на машине параллельно идёт длительная локальная транскрибация.

### Task 3: Гейт остановки и токенные инварианты

**Files:**
- Modify: `KrabEar/backend/recording_core_service.py` (`handle_stop_recording`, `_stop_recording_phase_a_locked`)
- Test: `KrabEar/tests/test_recording_stop_gate.py` (новый)
- Modify: `docs/superpowers/specs/2026-07-25-r2-recording-ownership-design.md`
- Modify: этот план (фактический контракт и evidence)

**Interfaces:**
- Consumes: `self._active_generation` (Task 2).
- Produces (для Task 5, 7): статусы `owner_mismatch`, `unknown_generation`,
  `stop_in_progress`, `finalization_failed`; bounded-реестр
  `self._finalizing_generations: dict[str, dict[str, Any]]`; хук
  `self._terminalize_generation(generation, response) -> None`, который Task 5
  расширяет записью в кэш.

**Инварианты (из спеки §4.2, нарушение = провал задачи):**
1. Гейт — ПЕРВАЯ операция в `_stop_recording_phase_a_locked`, ДО `self._stop_preview_worker()`. Иначе отвергнутый стоп уже убил партиалы/превью живой чужой записи.
2. Решение по ТОКЕНУ, не по кэшу.
3. Только отсутствие **ключа** `generation_token` → legacy-путь, НИКОГДА не
   отказ. Присутствующее невалидное значение (`None`, `""`, `0`, list/dict)
   → `unknown_generation`, recorder не трогаем.
4. `recorder_timeout` НЕ терминализирует поколение (повторный стоп — штатный путь спасения аудио, `recorder.py:158-161`).
5. `_active_generation` представляет только текущий physical capture. После
   успешной phase A прежняя G1 переходит в `_finalizing_generations`, поэтому
   G2 может стартовать, а повторный stop G1 всё ещё получает
   `stop_in_progress`.
6. Проверка и удаление generation выполняются под тем же lifecycle-lock, что
   start/phase A. Identity-check без lock — TOCTOU и не является CAS.
7. После `recorder_timeout` сохранённая active G1 блокирует fresh start до
   успешного token-retry, даже если physical recorder уже idle. Guard стоит до
   UUID/spill/device/start и не имеет побочных эффектов.

- [x] **Step 1: Написать падающий тест**

`KrabEar/tests/test_recording_stop_gate.py` (фейки — из `test_recording_spill_wiring.py`):

```python
"""Гейт остановки: токенные инварианты (R2 Task 3)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# _FakeRecorder / _FakeTranscriber / _make_service — из test_recording_spill_wiring.py


class StopGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        self._tmp = Path(self._tmp_ctx.name)
        self.rescue_dir = self._tmp / "rescue"

    def _svc(self, **kw):
        return _make_service(self._tmp, self.rescue_dir, recorder=_FakeRecorder(), **kw)

    def test_matching_token_stops_normally(self):
        svc = self._svc()
        start = svc.handle_start_recording({})
        resp = svc.handle_stop_recording({
            "quality_profile": "balanced",
            "generation_token": start["generation_token"],
        })
        self.assertNotIn(resp.get("status"), ("unknown_generation", "owner_mismatch"))

    def test_foreign_token_never_stops_active_recording(self):
        """F2: главный инвариант — чужой токен не трогает живую запись."""
        svc = self._svc()
        svc.handle_start_recording({})
        resp = svc.handle_stop_recording({
            "quality_profile": "balanced",
            "generation_token": "totally-unknown-token",
        })
        self.assertEqual(resp["status"], "unknown_generation")
        self.assertTrue(svc.recorder.is_recording, "запись обязана продолжаться")
        self.assertIsNotNone(svc._active_generation)

    def test_missing_token_uses_legacy_path(self):
        """Старый бинарь агента обязан продолжать работать (two-binary drift)."""
        svc = self._svc()
        svc.handle_start_recording({})
        resp = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertNotIn(resp.get("status"), ("unknown_generation", "owner_mismatch"))

    def test_gate_runs_before_preview_teardown(self):
        """F6: отвергнутый стоп НЕ должен сносить коллабораторов живой записи.

        Проверяем ПОРЯДОК напрямую — через мок `_stop_preview_worker`, а не через
        `_preview_text`: `_stop_preview_worker` только останавливает тред и текст
        превью НЕ трогает (сброс живёт в `_reset_preview_state` и происходит на
        старте следующей записи), поэтому ассерт на текст был бы зелёным и БЕЗ
        гейта — тест-пустышка, на которой воркер застрял бы в RED-фазе.
        """
        from unittest.mock import patch
        svc = self._svc()
        svc.handle_start_recording({})
        with patch.object(svc, "_stop_preview_worker") as stop_preview:
            resp = svc.handle_stop_recording({
                "quality_profile": "balanced",
                "generation_token": "foreign",
            })
        self.assertEqual(resp["status"], "unknown_generation")
        stop_preview.assert_not_called()
        self.assertTrue(svc.recorder.is_recording)
        self.assertIsNotNone(svc._active_generation)

    def test_terminalization_does_not_wipe_a_newer_generation(self):
        """🔴 P1: стоп A завершается, пока уже идёт запись B — слот B не трогать.

        lifecycle-лок отпускается сразу после phase_a, фазы b-e идут минутами
        без него: пользователь успевает начать запись B. Безусловное обнуление
        слота стёрло бы поколение B, и остановить её собственным токеном стало
        бы нечем (класс R1 HIGH-2).
        """
        svc = self._svc()
        gen_a = {"token": "A", "owner": "dictation", "state": "finalizing",
                 "started_at": 0.0, "promoted_from": None, "revision": 1}
        gen_b = {"token": "B", "owner": "dictation", "state": "capturing",
                 "started_at": 1.0, "promoted_from": None, "revision": 2}
        svc._finalizing_generations["A"] = gen_a
        svc._active_generation = gen_b
        svc._terminalize_generation(gen_a, {"status": "ok"})
        self.assertIs(svc._active_generation, gen_b, "поколение B не должно быть стёрто")
        self.assertNotIn("A", svc._finalizing_generations)

if __name__ == "__main__":
    unittest.main()
```

Кроме кода выше, Step 1 обязан содержать два **непустых детерминированных**
interleaving-теста:

1. Заблокировать phase B stop G1, запустить G2 и повторить stop с token G1:
   ответ `stop_in_progress`, recorder и active-generation G2 не изменены.
2. Удержать lifecycle-lock, запустить terminalizer G1 и доказать, что он ждёт;
   опубликовать G2 под тем же lock и отпустить. После завершения из
   finalizing-map удалена только G1, active G2 сохранена. Тест обязан
   синхронизироваться через `threading.Event`, без `sleep()` и вероятностного
   stress-loop.

- [x] **Step 2: Verify FAIL** — исходная реализация дала 9 FAIL / 10 PASS:
  токенный гейт отсутствовал, lifecycle G1/G2 не был разделён.

- [x] **Step 3: Реализация**

3a. В `__init__` добавить bounded-реестр:

```python
        self._finalizing_generations: dict[str, dict[str, Any]] = {}
```

Одновременно разрешено не более 8 незавершённых тяжёлых хвостов. Если реестр
достиг лимита, новый start возвращает уже поддерживаемый клиентом
`recorder_stopping` и не захватывает микрофон; живые записи из реестра не
вытесняются.

Добавить метод разбора гейта на `RecordingCoreService` (рядом с
`handle_stop_recording`).

**Гейт двухосевой, и оси нельзя схлопывать** (P3 ревью плана): ось 1 — токенные
инварианты (защита данных, безусловна), ось 2 — владение (политика shadow/enforce,
её тело добавляет Task 6). Ранний `return None` при отсутствии токена сделал бы
ось 2 недостижимой для tokenless-вызовов (стоп встречи после Task 6 идёт с `source`,
но БЕЗ токена), а `return None` при совпадении токена пропускал бы promote-кейс:
диктовка после повышения во встречу держит ВАЛИДНЫЙ токен, и её стоп остановил бы
встречу даже в enforce.

```python
    def _stop_gate_decision(self, params: "dict[str, Any]") -> "dict[str, Any] | None":
        """Решение гейта остановки. None = пропустить в штатный стоп (R2 §4.2).

        Ось 1 (токен) — защита данных, работает безусловно в обоих режимах.
        Ось 2 (владелец) — политика; в shadow только сообщает, в enforce отказывает.
        Отсутствие токена НИКОГДА не отказ сам по себе (старый бинарь агента против
        нового backend — задокументированный two-binary drift).
        """
        token_present = "generation_token" in params
        token = params.get("generation_token")
        requested_owner = params.get("source")
        gen = getattr(self, "_active_generation", None)

        # ── Ось 1: токенные инварианты ───────────────────────────────────────
        if token_present:
            if not isinstance(token, str) or not token:
                return {
                    "status": "unknown_generation",
                    "generation_token": token,
                }
            if gen is not None and gen.get("token") == token:
                if gen.get("state") == "finalizing":
                    return {
                        "status": "stop_in_progress",
                        "generation_token": token,
                    }
                # Токен наш и запись идёт — НО не выходим: promote-кейс обязан
                # дойти до оси 2 (владелец мог смениться на meeting).
            elif token in self._finalizing_generations_locked():
                return {
                    "status": "stop_in_progress",
                    "generation_token": token,
                }
            else:
                replayed = self._replay_terminal_response(token)
                if replayed is not None:
                    return replayed
                # Токен предъявлен, но не найден ни среди активных, ни в кэше:
                # рекордер НЕ трогаем ни при каких обстоятельствах — иначе
                # протухший ретрай остановил бы уже начатую СЛЕДУЮЩУЮ запись (F2).
                return {"status": "unknown_generation", "generation_token": token}

        # ── Ось 2: владение (тело _report_owner_mismatch и enforce — Task 6) ──
        if gen is not None and requested_owner and gen.get("owner") != requested_owner:
            self._report_owner_mismatch(gen.get("owner"), requested_owner)
            if bool(self._get_runtime_setting("recording_owner_enforce", False)):
                return {
                    "status": "owner_mismatch",
                    "owner": gen.get("owner"),
                    "requested": requested_owner,
                }

        return None
```

Заглушка `_report_owner_mismatch` вводится здесь же (Task 6 наполняет тело):

```python
    def _report_owner_mismatch(self, owner: "str | None", requested: str) -> None:
        """Сообщить о попытке остановить чужую запись. Task 6 добавляет
        WARNING + ErrorBus; здесь — заглушка, чтобы гейт был цельным."""
        return None
```

3b. Добавить заглушку replay (Task 5 наполнит её кэшем):

```python
    def _replay_terminal_response(self, token: str) -> "dict[str, Any] | None":
        """Реплей терминального ответа по токену. Task 5 добавляет кэш."""
        return None
```

3c. Добавить терминализацию поколения — **по ССЫЛКЕ на своё поколение, с CAS
под lifecycle-lock**:

```python
    def _terminalize_generation(
        self, gen: "dict[str, Any] | None", response: "dict[str, Any]"
    ) -> None:
        """Завершить КОНКРЕТНОЕ поколение: запись необратимо окончена (R2 §4.1).

        Зовётся ПЕРЕД каждым терминальным return handle_stop_recording. Task 5
        добавляет сюда запись ответа в кэш под ключом gen["token"].

        🔴 Принимает ССЫЛКУ на своё поколение и удаляет только его через CAS
        под lifecycle-lock. Безусловное `self._active_generation = None` было бы багом класса
        R1 HIGH-2: lifecycle-лок отпускается сразу после phase_a
        (`_stop_recording_phase_a`: `with lifecycle_lock: return ...`), а фазы
        b-e идут минутами БЕЗ лока. За это время пользователь успевает начать
        запись B (рекордер уже idle → старт проходит) — и завершение стопа A
        стёрло бы поколение живой записи B, после чего её собственный токен
        давал бы `unknown_generation` и остановить её стало бы нечем.
        """
        if gen is None:
            return
        # Task 5 вставит сюда запись в кэш под gen["token"].
        lifecycle_lock, _ = self._ensure_recording_lifecycle_state()
        with lifecycle_lock:
            token = str(gen.get("token") or "")
            finalizing = self._finalizing_generations_locked()
            if finalizing.get(token) is gen:
                del finalizing[token]
            # Defensive путь для already_stopped/empty до помещения в реестр.
            if self._active_generation is gen:
                self._clear_active_generation_locked()
```

Чтобы ссылка была доступна, `_stop_recording_phase_a_locked` кладёт своё поколение в
результат (тем же приёмом, каким `handle_stop_recording` уже забирает `spill`):
сначала сохраняет локальную G1, переводит именно её в `finalizing`, очищает
active-slot, затем добавляет в возвращаемый dict ключ `"generation": generation`.
Возвращать здесь `self._active_generation` запрещено: после атомарного move там
уже `None`, а позднее может находиться G2. В `handle_stop_recording` сразу после
`phase_a` захватить локальную ссылку рядом с существующим захватом spill:

```python
        _gen = phase_a.get("generation")
```

и передавать `_gen` первым аргументом во все вызовы `_terminalize_generation`.

3d. В `_stop_recording_phase_a_locked` — гейт ПЕРВОЙ строкой, до `self._stop_preview_worker()`:

```python
        # R2 §4.2 (F6): гейт — ПЕРВАЯ операция под lifecycle-локом. phase_a ниже
        # сносит preview worker, _rt_partial и RSF ДО recorder.stop(); гейт,
        # стоящий позже, уже убил бы партиалы живой ЧУЖОЙ записи, и обещание
        # «отказ не трогает запись» было бы ложью.
        _gate = self._stop_gate_decision(params)
        if _gate is not None:
            return {"early_return": _gate}
```

3e. В `handle_stop_recording` тяжёлые B–E вынести в
`_run_stop_recording_tail()`, который возвращает один terminal response. Outer
handler вызывает `_terminalize_generation(generation, response)` ровно в одной
точке. Неожиданное исключение B–E превращается в `finalization_failed`, spill
закрывается и остаётся для rescue, после чего та же единая точка терминализирует
G1. Для early-return phase A правило структурное:

- gate-ответ и `recorder_timeout` не содержат `generation` и возвращаются без
  терминализации;
- `already_stopped`, `empty_audio` и post-stop `finalization_failed` содержат
  локальную G1 и проходят через тот же terminalizer.

Так код не зависит от вручную поддерживаемого списка статусов и не оставляет
утечку map при добавлении новой терминальной ветки.

3f. В `_handle_start_recording_locked` при успешном старте взводить `capturing`
(уже сделано Task 2). До захвата микрофона проверить лимит
`_finalizing_generations`; при 8 live-хвостах вернуть `recorder_stopping`.

До лимита отдельно поставить guard: если recorder уже idle, но active G1
сохранена после `recorder_timeout`, вернуть `recorder_stopping` **до**
device/UUID/spill/start. Это не даёт G2 перезаписать retry/rescue G1.

В `phase_a_locked` сразу после успешного забора аудио (после того как `stopped`
получено и не `None`) атомарно переместить generation из active-слота в
finalizing-реестр через единый helper:

```python
        generation = self._move_active_generation_to_finalizing_locked()
```

Move выполняется до fallible breadcrumb/LM/settings hooks. Если любой из них
падает после physical stop, ответ становится `finalization_failed`, G1
терминализируется, spill остаётся для rescue. При `recorder_timeout` перемещения
нет: G1 остаётся активным retry-handle. Stop-gate читает active и finalizing
структуры только под lifecycle-lock (его вызывают первой операцией внутри phase A).

- [x] **Step 4: Зелёные + регрессия**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_recording_stop_gate.py KrabEar/tests/test_recording_generation.py KrabEar/tests/test_recording_core_service.py KrabEar/tests/test_recording_spill_wiring.py KrabEar/tests/test_meeting_session_service_W_C2a.py -v -p no:cacheprovider`
Expected: все PASS

Evidence 2026-07-26: новый stop-gate **20/20 PASS**; объединённая матрица
Tasks 1–3 **179/179 PASS** с `--timeout=60`, без xdist/cacheprovider;
ubuntu-parity Python 3.12.11 без MLX — все 6 файлов **179/179 PASS**, повтор
финального stop-gate после cleanup-hardening **20/20 PASS**. `flake8` и
`git diff --check` GREEN. Два независимых read-only аудита: GO для
изолированного Task 3 checkpoint; release NO-GO до исправленного Task 7 и Task 8
(явный гейт частичной выкладки выше).

- [x] **Step 5: Гейты и коммит**

```bash
scripts/pre_merge_py312_check.sh KrabEar/tests/test_recording_stop_gate.py
flake8 KrabEar/backend/recording_core_service.py KrabEar/tests/test_recording_stop_gate.py
git add KrabEar/backend/recording_core_service.py \
  KrabEar/tests/test_recording_stop_gate.py \
  docs/superpowers/plans/2026-07-25-r2-recording-ownership.md \
  docs/superpowers/specs/2026-07-25-r2-recording-ownership-design.md
git commit -m "feat(r2): гейт остановки — токенные инварианты, терминализация поколения (Task 3)"
```

### Task 4: Матрица переходов владения + promote

**Files:**
- Modify: `KrabEar/backend/recording_core_service.py` (preflight матрица +
  общий owner-transition/rollback)
- Modify: `KrabEar/backend/recording_spill.py` (новый метод `rewrite_source`)
- Test: `KrabEar/tests/test_recording_owner_matrix.py` (новый)
- Test: `KrabEar/tests/test_recording_generation.py`,
  `test_recording_spill_wiring.py`,
  `test_meeting_session_service_W_C2a.py` (усиленный контракт)
- Modify: дизайн-спека и этот план

**Interfaces:**
- Consumes: `self._active_generation` (Task 2), статусы Task 3.
- Produces: статусы старта `owner_conflict`, `unmanaged_recording`;
  promote-ответ `already_recording` + тот же `generation_token` + новая
  `owner_revision` + `owner_promoted: true` + `promoted: true`.

**Матрица (спека §4.3) — реализовать ДОСЛОВНО:**

| Текущий владелец | Стартует | Решение |
|---|---|---|
| нет записи | любой | обычный старт |
| `dictation` | `meeting` | promote: владелец → `meeting`, токен тот же, `promoted_from="dictation"` |
| тот же владелец | тот же | идемпотентный `already_recording` (как сегодня) |
| `quick_capture` | `meeting` | `owner_conflict` |
| любой | `quick_capture` | `owner_conflict` |
| любой | `dictation` | `owner_conflict` |
| `meeting` | любой (кроме meeting) | `owner_conflict` |
| запись идёт, поколения нет (call assist) | любой | `unmanaged_recording` |
| статус `recorder_stopping` | любой | отказ как сегодня |

- [x] **Step 1: Написать падающий тест**

`KrabEar/tests/test_recording_owner_matrix.py` — по кейсу на каждую строку матрицы. Ключевые:

```python
    def test_meeting_promotes_dictation(self):
        """Живой прод-сценарий C2: встреча повышает идущую диктовку."""
        svc = self._svc()
        start = svc.handle_start_recording({})
        svc.recorder.is_recording = True
        resp = svc.handle_start_recording({"source": "meeting"})
        self.assertEqual(resp["status"], "already_recording")
        self.assertTrue(resp["promoted"])
        self.assertEqual(resp["generation_token"], start["generation_token"])
        gen = svc._active_generation
        self.assertEqual(gen["owner"], "meeting")
        self.assertEqual(gen["promoted_from"], "dictation")

    def test_quick_capture_does_not_promote_to_meeting(self):
        svc = self._svc()
        svc.handle_start_recording({"source": "quick_capture"})
        svc.recorder.is_recording = True
        resp = svc.handle_start_recording({"source": "meeting"})
        self.assertEqual(resp["status"], "owner_conflict")
        self.assertEqual(svc._active_generation["owner"], "quick_capture")

    def test_same_owner_repeat_is_idempotent(self):
        svc = self._svc()
        svc.handle_start_recording({})
        svc.recorder.is_recording = True
        resp = svc.handle_start_recording({})
        self.assertEqual(resp["status"], "already_recording")
        self.assertFalse(resp.get("promoted", False))

    def test_recording_without_generation_is_unmanaged(self):
        """Call assist стартует рекордер напрямую, минуя сервис."""
        svc = self._svc()
        svc.recorder.is_recording = True   # рекордер занят, поколения нет
        resp = svc.handle_start_recording({"source": "meeting"})
        self.assertEqual(resp["status"], "unmanaged_recording")
```

- [x] **Step 2: Verify FAIL** — исходный RED: **10 FAIL / 2 PASS**. Зелёными
  были только fresh start и уже существующий `recorder_stopping`; отсутствовали
  conflicts/unmanaged/promoted/meta rewrite.

- [x] **Step 3: Реализация**

Матрица существующей generation решается **до** settings/device/UUID/spill и
`recorder.start()`. Это сильнее первоначального варианта в `if not started`:
repeat/promote/conflict не трогают микрофон и не создают placeholder B.

```python
requested_owner = _requested_recording_owner(params)  # null/blank → dictation
if active_generation is not None:
    if (
        not recorder_was_recording
        or active_generation.get("state") != "capturing"
    ):
        return {"status": "recorder_stopping"}
    return _active_generation_start_response_locked(
        active_generation,
        requested_owner,
    )
if recorder_was_recording:
    return {"status": "unmanaged_recording"}
```

`_active_generation_start_response_locked`:

- same owner возвращает тот же token/revision и оба false-маркера promote;
- dictation → meeting вызывает только
  `_transition_generation_owner_locked("meeting", promoted_from="dictation")`
  и возвращает `owner_promoted`, `owner_revision`, `promoted`;
- остальные пары возвращают `owner_conflict` без token, revision/meta/recorder
  не меняются.

Fallback `if not started` сохраняется лишь для гонки с внешним direct-recorder:
занятый recorder без generation → `unmanaged_recording`, idle →
`recorder_stopping`; placeholder собственной неудавшейся попытки discard-ится.

`_transition_generation_owner_locked` после in-memory CAS best-effort вызывает
`_active_spill.rewrite_source(...)`. Поэтому тот же путь работает при
`rollback_owner_transition`: source возвращается в `dictation`,
`promoted_from` удаляется. Исключение duck-typed writer логируется и не
откатывает уже совершённый owner-переход.

`RecordingSpillWriter.rewrite_source(...) -> bool`:

- сначала проверяет `_owns_paths`; collision/failed-open/discard возвращают
  `False` и не трогают чужие/удалённые пути;
- сохраняет sample_rate/channels/started_at и будущие неизвестные поля;
- пишет через `core.atomic_io.atomic_write_text` (unique temp, fsync,
  `os.replace`, cleanup);
- меняет `self.source` только после успешного replace;
- работает и при открытом writer, и после `close`; ошибка fail-open возвращает
  `False`, старый валидный meta остаётся на месте.

- [x] **Step 4: Зелёные + КРИТИЧЕСКАЯ регрессия promote**

Run (канонический проектный venv): `PYTHONDONTWRITEBYTECODE=1
PYTHONPATH="$PWD/KrabEar"
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python"
-m pytest
KrabEar/tests/test_recording_owner_matrix.py
KrabEar/tests/test_recording_generation.py
KrabEar/tests/test_recording_spill.py
KrabEar/tests/test_recording_spill_wiring.py
KrabEar/tests/test_recording_owner_state.py
KrabEar/tests/test_recording_stop_gate.py
KrabEar/tests/test_recording_core_service.py
KrabEar/tests/test_meeting_session_service_W_C2a.py
KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py
--timeout=60 -p no:cacheprovider -p no:xdist -q`.

Evidence 2026-07-26: исходный RED — **10 FAIL / 2 PASS**; после реализации
полная Task 1–4 матрица — **212/212 PASS** на macOS. Отдельный Python 3.12
без MLX прогнал owner/generation/spill/meeting-набор — **93/93 PASS**.
Два независимых read-only аудита повторили расширенный набор — **202 PASS**
каждый; atomic helper + новая owner-матрица дополнительно — **19/19 PASS**.
Падение трёх meeting-dispatch тестов в системном Anaconda Python
локализовано как shadowing локального namespace сторонним пакетом `tests`;
канонический `.venv_krab_ear` дал **6/6 PASS**, поэтому код/импорты проекта
не менялись.

- [x] **Step 5: Гейты и коммит**

```bash
scripts/pre_merge_py312_check.sh \
  KrabEar/tests/test_recording_owner_matrix.py \
  KrabEar/tests/test_recording_generation.py \
  KrabEar/tests/test_recording_spill.py \
  KrabEar/tests/test_recording_spill_wiring.py \
  KrabEar/tests/test_meeting_session_service_W_C2a.py
flake8 KrabEar/backend/recording_core_service.py \
  KrabEar/backend/recording_spill.py \
  KrabEar/tests/test_recording_owner_matrix.py \
  KrabEar/tests/test_recording_generation.py \
  KrabEar/tests/test_recording_spill_wiring.py \
  KrabEar/tests/test_meeting_session_service_W_C2a.py
git add \
  KrabEar/backend/recording_core_service.py \
  KrabEar/backend/recording_spill.py \
  KrabEar/tests/test_recording_owner_matrix.py \
  KrabEar/tests/test_recording_generation.py \
  KrabEar/tests/test_recording_spill_wiring.py \
  KrabEar/tests/test_meeting_session_service_W_C2a.py \
  docs/superpowers/plans/2026-07-25-r2-recording-ownership.md \
  docs/superpowers/specs/2026-07-25-r2-recording-ownership-design.md
git commit -m "feat(r2): матрица переходов владения + promote с атомарной spill-meta (Task 4)"
```

Evidence 2026-07-26: финальный focused-набор на каноническом venv —
**143/143 PASS**; расширенные macOS и Python 3.12 прогоны перечислены в
Step 4. `flake8`, `git diff --check` и два независимых read-only
adversarial-аудита — GREEN/GO. Swift/MLX/production runtime не запускались:
Task 4 не меняет Swift, а на машине параллельно идёт длительная локальная
транскрибация. Checkpoint не даёт разрешения на merge/deploy до Tasks 5–8.

---

## Чанк C — кэш ответов и телеметрия

### Task 5: Кэш терминальных ответов + инвалидация

**Files:**
- Modify: `KrabEar/backend/recording_core_service.py` (`__init__`, `_terminalize_generation`, `_replay_terminal_response`, новый `clear_terminal_cache`)
- Modify: `KrabEar/backend/history_service.py` (`__init__` — новый параметр, `handle_purge_all_data` — шаг очистки)
- Modify: `KrabEar/backend/service.py` (проводка: передать recording-core в `HistoryService`)
- Modify: `scripts/audit_inmemory_purge_coverage.py` (курируемый реестр — новая строка)
- Test: `KrabEar/tests/test_recording_terminal_cache.py` (новый)

**🔴 Проводки НЕ существует — её надо создать.** Проверено грепом: в
`history_service.py` ноль упоминаний `recording_core`. Формулировка «сверить имя
атрибута грепом» была бы тупиком. Конкретно:

1. `HistoryService.__init__` получает новый keyword-параметр `recording_core=None`
   (по образцу уже существующих опциональных коллабораторов этого класса —
   `_transcript_versions`, `_recording_chain_mgr`, `_semantic_searcher`: сохраняются
   в `self._<имя>` и везде проверяются на `None`).
2. `service.py` создаёт `HistoryService` раньше `RecordingCoreService`: он уже
   нужен `PurgeScheduler`, а перестановка расширила бы риск за Task 5. Поэтому
   после создания Core используется прямой late-inject
   `self._history._recording_core = self._recording_core_svc` рядом с уже живой
   проводкой `_job_tracker`. Callback отклонён: AST-гард ниже распознаёт literal
   clear-call на атрибуте, а late-inject соответствует текущему паттерну проекта.
3. Шаг в `handle_purge_all_data` рядом с остальными in-memory шагами:

```python
        # R2: RAM-кэш терминальных ответов несёт transcript целиком —
        # audit_purge_coverage видит только file-backed хранилища и его пропустит.
        try:
            if self._recording_core is not None:
                self._recording_core.clear_terminal_cache()
        except Exception:
            logger.warning("purge_all_data: clear_terminal_cache failed", exc_info=True)
            secondary_errors.append("terminal_cache")
```

4. `scripts/audit_inmemory_purge_coverage.py` — курируемый AST-реестр: строка вызова
   в шаге purge обязана **дословно** совпасть со строкой реестра. К прежним пяти
   записям добавляется шестая:
   `self._recording_core.clear_terminal_cache()` с описанием
   «terminal-ответы стопа (text/original_text/translated_text) в RAM, TTL 5 мин».

**Interfaces:**
- Consumes: `_terminalize_generation` / `_replay_terminal_response` (Task 3, заглушки).
- Produces: `clear_terminal_cache()` — публичный метод для purge.

- [x] **Step 1: Падающий тест** — `test_recording_terminal_cache.py`
  проверяет: exact replay без второго stop/persist; старый token не трогает G2;
  FIFO cap=3; monotonic TTL (`299.999` жив, `300.000` истёк); prune всех expired
  на put/replay; privacy read-gate; idempotent clear; deep-copy на store и каждом
  replay; stale identity-CAS; fail-open copy/read/write; History constructor,
  purge success/failure/no-confirm/compat и production late-inject.

- [x] **Step 2: Verify FAIL** — исходный RED **12 FAIL / 3 PASS**. После базовой
  реализации два независимых adversarial-аудита добавили детерминированные RED:
  cache write после успешного CAS пробрасывал `RuntimeError`, затем cache read
  из TTL-prune ломал stop-gate. Оба дефекта воспроизведены до исправления.

- [x] **Step 3: Реализация.**

- `OrderedDict[token → (stored_at_monotonic, deepcopy(response))]`, отдельный
  lock, FIFO cap=3 и TTL 300 секунд; каждый put/replay удаляет **все** expired.
- generation при publish сохраняет `terminal_cache_epoch`. Purge под cache-lock
  делает `clear()` + `epoch += 1`; terminalizer после успешного identity-CAS
  публикует snapshot только при совпавшем epoch. Так blocked G1, начатая до
  purge, завершает history, но не репопулирует PII; G2 после purge replayable.
- Snapshot ответа строится до lifecycle-lock; CAS-delete + cache publish остаются
  одной lifecycle-линеаризацией без окна `не finalizing, но ещё не cached`.
  Lock-order только `lifecycle → cache`; purge lifecycle-lock не берёт.
- Store и replay используют независимые `deepcopy`. Ошибки snapshot, publish и
  всего read-пути (`ensure/prune/get/deepcopy/pop`) fail-open как cache miss:
  terminal lifecycle не меняется, payload/token в warning не логируются.
- Replay дважды проверяет live `privacy_mode_enabled`; privacy возвращает
  `unknown_generation`, а не сохранённый текст.
- `HistoryService(recording_core=None)` + production late-inject, прямой purge
  clear-call и шестая запись curated AST registry реализованы дословно.

- [x] **Step 4: Зелёные + privacy audit**

Evidence 2026-07-26:

- новый cache/epoch/purge-набор — **20/20 PASS**;
- расширенная регрессия Tasks 1–5 + существующие RAM-purge кластеры —
  **197/197 PASS** на каноническом macOS venv;
- Ubuntu-parity Python 3.12.11 без MLX — **20/20 PASS**, новых worker-процессов
  после файла нет;
- `audit_inmemory_purge_coverage.py --fail-on-found`: **6/6 covered, 0 gaps**;
- `flake8`, `git diff --check` и независимый read-only adversarial-аудит —
  GREEN/GO.

- [x] **Step 5: Гейты и коммит**

```bash
scripts/pre_merge_py312_check.sh \
  KrabEar/tests/test_recording_terminal_cache.py
"/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear/bin/python" \
  scripts/audit_inmemory_purge_coverage.py --fail-on-found
git add \
  KrabEar/backend/recording_core_service.py \
  KrabEar/backend/history_service.py \
  KrabEar/backend/service.py \
  KrabEar/tests/test_recording_terminal_cache.py \
  scripts/audit_inmemory_purge_coverage.py \
  docs/superpowers/plans/2026-07-25-r2-recording-ownership.md \
  docs/superpowers/specs/2026-07-25-r2-recording-ownership-design.md
git commit -m "feat(r2): TTL-replay терминальных ответов + purge-epoch (Task 5)"
```

Swift/MLX/production runtime не запускались и не перезапускались: Task 5
backend-only, а на машине параллельно идёт длительная локальная транскрибация.
Этот checkpoint не даёт разрешения на merge/deploy до Tasks 6–8.

### Task 6: Прошивка `source` + shadow-телеметрия владения

**Files:**
- Modify: `KrabEar/backend/meeting_session_service.py:493-494` (внутренний стоп)
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+QuickCapture.swift:91, 134`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main+HotkeyRecording.swift` (старт и стоп)
- Modify: `KrabEar/backend/recording_core_service.py` (`_stop_gate_decision` — mismatch)
- Modify: `KrabEar/backend/error_codes.py` (новый код)
- Modify: `KrabEar/backend/error_bus.py` (типизированный component `recording`)
- Modify: `KrabEar/backend/service.py` (живой late-inject ErrorBus)
- Modify: `KrabEar/backend/settings_validator.py` (bool-нормализация)
- Modify: `KrabEar/core/config.py` (`recording_owner_enforce`)
- Modify: `KrabEar/tests/test_error_codes.py` (канонический registry = 64)
- Modify: `KrabEar/tests/test_meeting_session_service_W_C2a.py` (живой payload)
- Test: `KrabEar/tests/test_recording_owner_telemetry.py` (новый)

**Ключевое правило (F5):** mismatch фиксируется ТОЛЬКО позитивный — владелец передан И не совпал. Отсутствие владельца — `logger.debug`, никогда не WARNING и не ErrorBus. Иначе штатная финализация встречи спамила бы телеметрию и «неделя чистых логов» не наступила бы.

- [x] **Step 1: Падающий тест** — 22 теста покрывают shadow/enforce,
  matching/tokenless stop, foreign/malformed/finalizing/replay precedence,
  promote, legacy/whitespace/non-string source, fail-open logger/ErrorBus,
  PII-redaction, config/validator/registry и живую source-проводку.

- [x] **Step 2: Verify FAIL** — исходный RED: **19 failure/subfailure /
  10 pass** (21 test item). Падения точно совпали с отсутствующей Task 6:
  stub-телеметрия, ложный `bool("false")`, ненормализованный source, нет
  ErrorBus wiring/registry/default и три живые stop/start-проводки.

- [x] **Step 3: Реализация.**

Фактическая реализация расширена после независимого pre-audit:

- `_requested_stop_owner` принимает только непустую строку после `strip`;
  missing/null/empty/whitespace/non-string остаются legacy и дают только
  PII-безопасный DEBUG при живом generation;
- token invariants и terminal replay остаются раньше owner-политики;
- positive mismatch даёт один WARNING + ErrorBus. Произвольные source не
  выходят из lifecycle-слоя: allowlist `dictation/meeting/quick_capture`,
  остальное сворачивается в `other`; logger/ErrorBus/debug fail-open;
- `recording.owner_mismatch` получил нейтральный user-message, severity warn,
  dedupe 30 и типизированный ErrorBus component `recording`;
- Core получает живой `self._error_bus`; DEFAULT_SETTINGS и SettingsValidator
  держат `recording_owner_enforce=False`, Core дополнительно использует
  `_coerce_bool`, поэтому строка `"false"` не включает enforce;
- meeting internal stop передаёт `source=meeting`; dictation start/stop —
  `source=dictation`; Quick Capture start уже был прошит до Task 6, поэтому
  добавлены только normal/orphan stop без duplicate Swift key.

- [x] **Step 4: Зелёные регрессии + ресурсно-безопасный Swift-гейт**

Evidence 2026-07-26: новый файл — **22/22 PASS**; объединённый узкий набор
Tasks 1–6 + meeting/settings/ErrorBus registry — **280/280 PASS** на macOS.
Ubuntu-parity Python 3.12 без MLX — **22 + 38 + 12 PASS** для Task 6,
meeting и error registry. `flake8`, `py_compile`, `git diff --check` и
`swiftc -frontend -parse` двух изменённых Swift-файлов зелёные.
Независимый Terra Ultra post-audit: **GO, P0–P2 нет**.

Полный `swift test` намеренно отложен до release-гейта Task 8: на M4 Max
параллельно сутки идёт локальная транскрибация, а Swift build создаст лишнюю
CPU/disk contention. Parse-only здесь честно отмечен и не считается release
acceptance.

- [x] **Step 5: Гейты и checkpoint-коммит**

Task 6 отдельно не merge/deploy: enforce остаётся `False`, а Task 7 обязан
добавить token/retry, сохранить meeting retry-handle при `recorder_timeout`
и убрать условный stop по `recorder.is_recording`.

---

## Чанк D — Swift-ретрай и закрытие

### Task 7: Единый helper остановки с ретраем

**Files:**
- Create: `native/KrabEarAgent/Sources/KrabEarAgent/RecordingStopCoordinator.swift`
- Modify: `main+HotkeyRecording.swift`, `main+QuickCapture.swift` (перевести стопы на helper)
- Modify: `MeetingLivePanelController.swift` (тот же bounded retry без sync IPC)
- Modify: `KrabEar/backend/meeting_session_service.py` (сохранить token и
  session/retry-handle при `recorder_timeout`)
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/RecordingStopCoordinatorTests.swift`
- Test: `KrabEar/tests/test_meeting_session_service_W_C2a.py`

**Контракт helper'а** (спека §4.6):
- предъявляет `generation_token`, полученный на старте;
- ретраит ТОЛЬКО транспортную неоднозначность (сокет закрыт, обрыв, реконнект), максимум 2 доп. попытки;
- `recorder_timeout` — единственный non-terminal типизированный ответ: повторить
  stop с тем же token максимум 2 дополнительных раза с задержками 2 и 4 с;
  после исчерпания бюджета сохранить token/recovery-state, и следующее действие
  снова направить в stop, а не в fresh start;
- прочие типизированные `ok:false` НЕ ретраит;
- `stop_in_progress` → опрос раз в 2 с, суммарный бюджет 5 минут, затем сообщение «финализация затянулась — результат появится в истории» (НЕ «потеряно»);
- `unknown_generation` и исчерпание попыток → показать превью + «запись восстановится при следующем запуске»;
- `owner_mismatch` → человеческое сообщение + сброс локального состояния.

Референс на чтение (НЕ cherry-pick): `.worktrees/user3-recording-rescue-20260722/native/KrabEarAgent/Sources/KrabEarAgent/RecordingStopRecovery.swift` — там та же задача решена в 679 строк; наша цель ~200-250, без owner/token-двухосевости.

- [ ] **Step 1: Написать падающий тест**

Создать `native/KrabEarAgent/Tests/KrabEarAgentTests/RecordingStopCoordinatorTests.swift`.
Тестируется чистая логика решения (что делать с ответом/ошибкой), поэтому координатор
проектируется так, чтобы решение было отделимо от IPC: чистая функция
`RecordingStopCoordinator.decide(afterStatus:attempt:)` и
`RecordingStopCoordinator.decide(afterTransportError:attempt:)`, возвращающие
`StopDecision`. IPC-цикл — тонкая обёртка над ними.

```swift
import XCTest
@testable import KrabEarAgent

final class RecordingStopCoordinatorTests: XCTestCase {

    func test_transport_error_retries_up_to_two_extra_attempts() {
        XCTAssertEqual(RecordingStopCoordinator.decide(afterTransportError: true, attempt: 1), .retry)
        XCTAssertEqual(RecordingStopCoordinator.decide(afterTransportError: true, attempt: 2), .retry)
        XCTAssertEqual(RecordingStopCoordinator.decide(afterTransportError: true, attempt: 3), .giveUpRescuePending)
    }

    func test_typed_backend_error_is_never_retried() {
        // Типизированный ok:false — настоящая ошибка бэкенда, крутить её нельзя.
        XCTAssertEqual(RecordingStopCoordinator.decide(afterStatus: "stt_failed", attempt: 1), .surfaceAsIs)
    }

    func test_recorder_timeout_retries_same_generation_then_retains_recovery() {
        XCTAssertEqual(
            RecordingStopCoordinator.decide(afterStatus: "recorder_timeout", attempt: 1),
            .retryRecorderStop(delaySec: 2)
        )
        XCTAssertEqual(
            RecordingStopCoordinator.decide(afterStatus: "recorder_timeout", attempt: 2),
            .retryRecorderStop(delaySec: 4)
        )
        XCTAssertEqual(
            RecordingStopCoordinator.decide(afterStatus: "recorder_timeout", attempt: 3),
            .recoveryPending
        )
    }

    func test_stop_in_progress_polls_within_budget_then_reports_slow_finalization() {
        XCTAssertEqual(RecordingStopCoordinator.decide(afterStatus: "stop_in_progress", attempt: 1), .pollAgain)
        // 5 минут / 2 с = 150 опросов; 151-й выходит из цикла БЕЗ «потеряно».
        XCTAssertEqual(RecordingStopCoordinator.decide(afterStatus: "stop_in_progress", attempt: 151), .finalizationSlow)
    }

    func test_unknown_generation_points_at_rescue_not_loss() {
        XCTAssertEqual(RecordingStopCoordinator.decide(afterStatus: "unknown_generation", attempt: 1), .giveUpRescuePending)
    }

    func test_owner_mismatch_has_its_own_branch() {
        XCTAssertEqual(RecordingStopCoordinator.decide(afterStatus: "owner_mismatch", attempt: 1), .foreignOwner)
    }
}
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `cd native/KrabEarAgent && swift test --filter RecordingStopCoordinatorTests`
Expected: FAIL — `cannot find 'RecordingStopCoordinator' in scope`

- [ ] **Step 3: Реализовать координатор**

Создать `native/KrabEarAgent/Sources/KrabEarAgent/RecordingStopCoordinator.swift`:

```swift
import Foundation

/// Решение о следующем шаге остановки записи (R2 §4.6).
enum StopDecision: Equatable {
    /// Транспорт неоднозначен — повторить запрос с тем же токеном.
    case retry
    /// Physical worker не отдал аудио — повторить stop той же G1.
    case retryRecorderStop(delaySec: TimeInterval)
    /// Бюджет timeout-retry исчерпан; token нельзя очищать или заменять G2.
    case recoveryPending
    /// Финализация ещё идёт — подождать и переспросить.
    case pollAgain
    /// Финализация затянулась дольше бюджета: результат появится в истории.
    case finalizationSlow
    /// Запись не найдена / попытки исчерпаны: аудио восстановится при старте.
    case giveUpRescuePending
    /// Запись принадлежит другому потребителю.
    case foreignOwner
    /// Обычный ответ бэкенда — обработать как есть, НЕ ретраить.
    case surfaceAsIs
}

enum RecordingStopCoordinator {
    /// Максимум ДОПОЛНИТЕЛЬНЫХ попыток при транспортной неоднозначности.
    static let maxTransportRetries = 2
    /// Максимум ДОПОЛНИТЕЛЬНЫХ physical-stop попыток после recorder_timeout.
    static let maxRecorderTimeoutRetries = 2
    /// Опрос при stop_in_progress: раз в 2 с, суммарный бюджет 5 минут.
    static let pollIntervalSec: TimeInterval = 2
    static let maxPolls = 150

    static func decide(afterTransportError isTransport: Bool, attempt: Int) -> StopDecision {
        guard isTransport else { return .surfaceAsIs }
        return attempt <= maxTransportRetries ? .retry : .giveUpRescuePending
    }

    static func decide(afterStatus status: String, attempt: Int) -> StopDecision {
        switch status {
        case "recorder_timeout":
            guard attempt <= maxRecorderTimeoutRetries else {
                return .recoveryPending
            }
            return .retryRecorderStop(
                delaySec: TimeInterval(1 << attempt)
            )
        case "stop_in_progress":
            return attempt <= maxPolls ? .pollAgain : .finalizationSlow
        case "unknown_generation":
            return .giveUpRescuePending
        case "owner_mismatch":
            return .foreignOwner
        default:
            // Любой ОСТАЛЬНОЙ типизированный ответ бэкенда — не ретраим:
            // recorder_timeout разобран выше как единственное non-terminal
            // исключение, а настоящую ошибку крутить бесконечно нельзя.
            return .surfaceAsIs
        }
    }
}
```

- [ ] **Step 4: Зелёные тесты**

Run: `cd native/KrabEarAgent && swift test --filter RecordingStopCoordinatorTests`
Expected: все PASS

- [ ] **Step 5: Подключить координатор к живым путям остановки**

В `main+HotkeyRecording.swift` и `main+QuickCapture.swift`: хранить `generation_token`,
полученный из ответа `start_recording`, передавать его в params стопа и прогонять
ответ/ошибку через `RecordingStopCoordinator.decide(...)`, реализовав ветки:
`.retry` — повторить вызов после транспортной неоднозначности;
`.retryRecorderStop(delaySec)` — после async `Task.sleep` на 2/4 с повторить
stop **с тем же token**;
`.recoveryPending` — сохранить token и локальный recovery-state, показать
«Аудио ещё восстанавливается — нажмите остановку ещё раз; при необходимости
перезапустите backend»; следующий toggle обязан снова идти в stop, не start;
`.pollAgain` — `Thread.sleep`-эквивалент через
`Task.sleep(nanoseconds:)` на `pollIntervalSec` и повтор; `.finalizationSlow` —
`notify` «Финализация затянулась — результат появится в истории»; `.giveUpRescuePending`
— показать текст превью + «запись восстановится при следующем запуске»;
`.foreignOwner` — «Идёт другая запись» + сброс локального `isRecording`;
`.surfaceAsIs` — существующая обработка статусов (не трогать).

Точное место хранения токена: `recordingTargetApp` — обычное stored property на
`AgentAppDelegate` (`main.swift:184`, НЕ associated-object; не городить
`objc_setAssociatedObject`). Завести рядом `var activeGenerationToken: String?`
тем же способом. Обнулять только после terminal response; при
`recorder_timeout`/`.recoveryPending` токен сохраняется.

Meeting-путь закрыть тем же контрактом, а не надеяться на публичный флаг
`recorder.is_recording`:

1. `_MeetingSession` хранит `generation_token`, полученный из ответа
   `handle_start_recording`; `meeting_start` и `get_meeting_live_state`
   аддитивно возвращают его Swift как opaque-string.
2. `meeting_stop` принимает необязательный token. Если он передан и не совпал с
   живой session, вернуть `unknown_generation` без вызова Core; tokenless старый
   Swift получает server-side token из session.
3. `handle_meeting_stop` зовёт Core с
   `{"source": "meeting", "generation_token": token}` даже если recorder уже
   сообщает `False`.
4. При `recorder_timeout` сессия и token сохраняются, `_teardown_session()` и
   `meeting.finished` не вызываются; ответ остаётся non-terminal.
   Флаг `stop_retry_pending` не даёт повторно эмитить `meeting.finalizing`.
5. `MeetingLivePanelController` повторяет именно `meeting_stop`, а не raw
   `stop_recording`, по решению `.retryRecorderStop`; при `.recoveryPending`
   вновь разрешает явное действие восстановления, но не изображает
   idle/fresh-start. Ручной повтор получает новый отдельный трёхпопытный бюджет.
6. Потерянный terminal IPC-ответ: повтор с token после teardown вызывает Core
   replay Task 5 и снова возвращает тот же `item_id`; persist и
   `meeting.finished` не дублируются. Конкурентный wrapper-stop нормализуется в
   `stop_in_progress`.

Backend-тесты обязаны покрыть: первый stop меняет recorder flag на `False` и
возвращает timeout, повтор всё равно предъявляет тот же token и завершается;
исчерпание retry сохраняет `_MeetingSession`, token и не эмиттит
`meeting.finished`; неверный token не вызывает Core; конкурентный stop зовёт
Core один раз; потерянный успешный ответ реплеит тот же `item_id` без второго
persist/finished; tokenless legacy-вызов использует token session.

🔴 Poll-цикл `stop_in_progress` (до 5 минут) обязан жить **вне главного потока** —
это прямой AGENT-3 класс (синхронный IPC на main thread даёт AppHang).
`AgentAppDelegate` помечен `@MainActor`, поэтому одного внешнего `Task.detached`
недостаточно: обращение к actor-isolated helper может снова выполнить синхронный
IPC на main. Каждый IPC-вызов цикла обязан идти через существующий
`ipcClient.callAsync(...)`, который переносит `call(...)` на background queue.
Ожидание между опросами — `try await Task.sleep(nanoseconds:)`, НЕ
`Thread.sleep`; чистая decision-логика координатора не должна требовать MainActor.

- [ ] **Step 6: Полная сборка и сьюта**

Run: `cd native/KrabEarAgent && swift build -c release && swift test`
Expected: сборка OK, вся сьюта зелёная

- [ ] **Step 7: Коммит**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/RecordingStopCoordinator.swift \
  native/KrabEarAgent/Tests/KrabEarAgentTests/RecordingStopCoordinatorTests.swift \
  native/KrabEarAgent/Sources/KrabEarAgent/main+HotkeyRecording.swift \
  native/KrabEarAgent/Sources/KrabEarAgent/main+QuickCapture.swift \
  native/KrabEarAgent/Sources/KrabEarAgent/MeetingLivePanelController.swift \
  KrabEar/backend/meeting_session_service.py \
  KrabEar/tests/test_meeting_session_service_W_C2a.py
git commit -m "feat(r2): единый координатор остановки с ретраем и бюджетом (Task 7)"
```

### Task 8: Живой e2e, финальные гейты, документация

**Files:**
- Create: `scripts/e2e_owner_gate_smoke.py`
- Modify: `docs/ROADMAP-2026H2.md` (журнал + хвост F8), `CLAUDE.md` (карта модулей)

- [ ] **Step 1: Живой смок.** По образцу `scripts/e2e_rescue_smoke.py`
  (throwaway data_dir, teardown в finally). Сценарии: (а) диктовка → promote во
  встречу → `meeting_stop`: один терминальный ответ, ноль ложных mismatch в
  логе; (б) стоп с протухшим токеном при активной записи →
  `unknown_generation`, запись жива; (в) двойной стоп с одним токеном → один и
  тот же ответ; (г) `recorder_timeout` → recorder flag `False` → повтор тем же
  token успешно забирает G1, fresh start между попытками не вызывается;
  (д) meeting-timeout сохраняет session/token, не эмиттит `meeting.finished`,
  повторный `meeting_stop` завершается ровно один раз; (е) terminal
  `meeting_stop` с потерянным IPC-ответом реплеит тот же `item_id` по token без
  второго persist/finished.

- [ ] **Step 1b: Проверка DoD 1 — тап хоткея при чужой записи.** Сценарии (а)-(в)
  все IPC-уровня и НЕ проверяют то, что обещает DoD 1 спеки («тап Right Option при
  активной встрече не останавливает её — доказано живым смоком»). Закрыть одним из
  двух способов, выбор — за исполнителем по обстановке:
  - **предпочтительно**: AX-смок по паттерну C2c (клик по меню-бару через
    Accessibility + `say` в колонки как источник звука; синтетические keystroke НЕ
    долетают до `NSEvent.addGlobalMonitorForEvents` — задокументированный урок C3b,
    поэтому именно AX-клик, а не эмуляция клавиши);
  - **если AX-среда недоступна** (Stage Manager, чужие окна на экране — риск из C3a):
    зафиксировать это в отчёте задачи и **ослабить формулировку DoD 1 в спеке** до
    «Swift source-contract тест + IPC-смок», а не оставлять расхождение молча.
    Расхождение обещания и проверки любой следующий ревьюер поднимет как находку.

- [ ] **Step 2: Полные гейты**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_recording_owner_state.py KrabEar/tests/test_recording_generation.py \
  KrabEar/tests/test_recording_stop_gate.py KrabEar/tests/test_recording_owner_matrix.py \
  KrabEar/tests/test_recording_terminal_cache.py KrabEar/tests/test_recording_owner_telemetry.py \
  KrabEar/tests/test_recording_core_service.py KrabEar/tests/test_recording_spill_wiring.py \
  KrabEar/tests/test_recording_rescue.py KrabEar/tests/test_meeting_session_service_W_C2a.py \
  KrabEar/tests/test_backend_service.py -v -p no:cacheprovider
make audit-all
cd native/KrabEarAgent && swift build -c release && swift test
python scripts/e2e_owner_gate_smoke.py
python scripts/e2e_rescue_smoke.py     # регрессия R1
bash scripts/run_e2e_smokes.command    # регрессия 37 методов + privacy
```

- [ ] **Step 3: Документация.** Журнал ROADMAP (объём, находки, отклонённая рекомендация по `recorder_timeout` с обоснованием); **обязательно строка про хвост F8** (таймаут `meeting_stop` 60с короче финализации часовой встречи); строки в CLAUDE.md про поколение записи и матрицу владения.

- [ ] **Step 4: Adversarial-ревью всего диффа** (Fable) перед мержем — конвейер §1 ROADMAP.

---

## Порядок и параллелизм

Задачи строго последовательны: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8. Каждая следующая опирается на интерфейсы предыдущей (`_active_owner` → `_active_generation` → гейт → матрица → кэш → телеметрия → клиент). Параллелить нечего — это одна цепочка по одному файлу-ядру `recording_core_service.py`.

Рекомендуемое исполнение: subagent-driven, свежий Sonnet-воркер на задачу, личный построчный гейт координатора после каждой, финальный Fable-гейт всего диффа перед мержем.
