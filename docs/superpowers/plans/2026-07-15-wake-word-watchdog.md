# Wake-word Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wake word самовосстанавливается после обоих вариантов audio-wedge (шторм нулей / зависшее чтение) без участия владельца.

**Architecture:** Heartbeat последнего ненулевого чанка в `_listen_loop` → `WakeWordWatchdog` (таймер-тред, 5с) замечает staleness → single-flight мягкий reinit через новый `AudioReinitCoordinator` (общий с `AudioSelfHealer`) → при зависшем треде или безуспешном reinit — `wedged:true` в `wake_word_status` + ErrorBus → Swift `WakeWordPoller` (rate-limit 30 мин) дёргает новый `BackendSupervisor.forceRestartBackend()` (passive: `launchctl kickstart -k`). Спека: `docs/superpowers/specs/2026-07-15-wake-word-watchdog-design.md` (одобрена владельцем 2026-07-15).

**Tech Stack:** Python 3.14 (`.venv_krab_ear`), unittest, threading; Swift 6 (SPM), XCTest.

**Исполнение:** строго последовательно в ОДНОМ worktree (база — tip `codex/krab-ear-v2`): Task 2 нужен Task 3 и Task 4; Task 6 wired всё из Task 1–5; Task 8 гоняет финальные гейты.

**Критические конвенции проекта (нарушение = красный CI):**
- Тесты гонять: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/<file>.py -v` из корня репо, venv `.venv_krab_ear` активирован.
- Тред-стабы в тестах — duck-type классы, НИКОГДА не наследники `threading.Thread` (иначе atexit-hang, exit 124 в CI).
- Каждый тест, конструирующий `BackendService`, ОБЯЗАН звать `self.service.close()` в `tearDown` (#1782).
- НЕ запускать прод-бинарь/`.app` (single-instance guard убьёт живой агент владельца). Swift — только `swift build -c release` и `swift test`.
- Новые модули `backend/*.py` обязаны иметь production-импортёров к моменту прогона `make audit-all` — поэтому Task 6 (проводка) идёт до финальных гейтов, а `make audit-all` гоняется в Task 8, не в Task 2/4.
- Коммиты — с трейлером `Co-Authored-By:` (указан в шагах).

---

## File Structure

| Файл | Роль |
|---|---|
| `KrabEar/backend/openwakeword_adapter.py` (modify) | heartbeat, generation-токен, `stop() -> bool`, хранение `wedged`, аддитивные поля status |
| `KrabEar/backend/audio_reinit.py` (create) | `AudioReinitCoordinator` + `ReinitOutcome` — единственный владелец танца reinit, single-flight |
| `KrabEar/backend/audio_selfheal.py` (modify) | делегация танца координатору; счётчиковая логика не меняется |
| `KrabEar/backend/wake_word_watchdog.py` (create) | `WakeWordWatchdog` — детекция staleness, heal, эскалация |
| `KrabEar/core/config.py` (modify) | `wake_word_watchdog_enabled`, `wake_word_stale_sec` |
| `KrabEar/backend/settings_validator.py` (modify) | `_BOOL_FIELDS` / `_RANGE_FIELDS` |
| `KrabEar/backend/error_codes.py` (modify) | `audio.wakeword_wedged` |
| `KrabEar/backend/service.py` (modify) | проводка координатор/хилер/watchdog + `close()` |
| `KrabEar/backend/health_check_service.py` (modify) | секция `wake_word_watchdog` в `get_diagnostics` |
| `native/.../WakeWordPoller.swift` (modify) | `WedgedEscalationTracker` + обработка `wedged` в `tick()` |
| `native/.../BackendSupervisor.swift` (modify) | `forceRestartBackend()` + `kickstartArguments()` |
| `native/.../main.swift` (modify) | проводка `onWedgedEscalation` |
| Тесты | `test_wake_word_heartbeat.py`, `test_audio_reinit_coordinator.py`, `test_wake_word_watchdog.py`, `test_wake_word_watchdog_settings.py` (create); `test_audio_selfheal.py`, `test_audio_selfheal_wiring.py` (modify); `WedgedEscalationTrackerTests.swift` (create); `BackendSupervisorTests.swift` (modify); `scripts/e2e_ipc_smoke.py` (modify) |

---

### Task 1: Адаптер — heartbeat, generation-токен, `stop() -> bool`, wedged-хранилище

**Files:**
- Modify: `KrabEar/backend/openwakeword_adapter.py`
- Test: `KrabEar/tests/test_wake_word_heartbeat.py` (create)

Контекст: `_listen_loop` (строки ~460-533) читает чанки из `sd.InputStream`; `sounddevice` импортируется ЛЕНИВО внутри `_listen_loop` — тесты подставляют фейк-модуль в `sys.modules` ОБРАТИМО (вставили в setUp → сняли в tearDown; необратимые стабы отравляют chunk-CI). `_listen_loop` можно вызывать СИНХРОННО в тесте — тред не нужен.

- [ ] **Step 1: Написать падающие тесты**

Создать `KrabEar/tests/test_wake_word_heartbeat.py`:

```python
"""Heartbeat/generation/stop()->bool в OpenWakeWordAdapter (спека 2026-07-15).

_listen_loop вызывается СИНХРОННО с фейковым sounddevice-модулем,
вставленным в sys.modules ОБРАТИМО (setUp/tearDown).
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
BACKEND_DIR = _PROJECT_ROOT / "KrabEar"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402


class _FakeStream:
    """Контекст-менеджер, эмулирующий sd.InputStream: отдаёт заготовленные
    чанки; когда чанки кончились — взводит stop_event и отдаёт нули."""

    def __init__(self, chunks, stop_event):
        self._chunks = list(chunks)
        self._stop_event = stop_event
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        self.reads += 1
        if not self._chunks:
            self._stop_event.set()
            return np.zeros((n, 1), dtype=np.int16), False
        return self._chunks.pop(0), False


class _FakeOWW:
    def predict(self, arr):
        return {}


def _nonzero_chunk(n=4):
    a = np.zeros((n, 1), dtype=np.int16)
    a[0][0] = 7
    return a


def _zero_chunk(n=4):
    return np.zeros((n, 1), dtype=np.int16)


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        self._sd_was_present = "sounddevice" in sys.modules
        self._sd_saved = sys.modules.get("sounddevice")
        self.fake_sd = types.ModuleType("sounddevice")
        sys.modules["sounddevice"] = self.fake_sd

    def tearDown(self):
        if self._sd_was_present:
            sys.modules["sounddevice"] = self._sd_saved
        else:
            sys.modules.pop("sounddevice", None)

    def _run_loop(self, chunks, generation=None):
        """Синхронный прогон _listen_loop с фейковым стримом."""
        self.adapter._stop_event.clear()
        stream = _FakeStream(chunks, self.adapter._stop_event)
        self.fake_sd.InputStream = lambda **kw: stream
        self.adapter._oww = _FakeOWW()
        gen = generation if generation is not None else self.adapter._generation
        self.adapter._listen_loop(
            threshold=0.5, chunk_size=4, sample_rate=16000, generation=gen,
        )
        return stream

    def test_nonzero_chunk_stamps_heartbeat(self):
        self._run_loop([_nonzero_chunk()])
        hb = self.adapter.heartbeat()
        self.assertIsNotNone(hb["listen_started_ts"])
        self.assertIsNotNone(hb["last_chunk_ts"])

    def test_zero_chunks_do_not_stamp_heartbeat(self):
        self._run_loop([_zero_chunk(), _zero_chunk()])
        hb = self.adapter.heartbeat()
        self.assertIsNotNone(hb["listen_started_ts"])
        self.assertIsNone(hb["last_chunk_ts"])

    def test_stale_generation_exits_loop_early(self):
        # Поколение адаптера ушло вперёд — «зомби»-тред обязан выйти,
        # не дочитав все чанки (проверяем по счётчику reads: 1, не 3).
        self.adapter._generation = 5
        stream = self._run_loop(
            [_nonzero_chunk(), _nonzero_chunk(), _nonzero_chunk()],
            generation=4,
        )
        self.assertEqual(stream.reads, 1)

    def test_start_resets_heartbeat_and_wedged(self):
        self.adapter._last_chunk_ts = 123.0
        self.adapter._listen_started_ts = 120.0
        self.adapter.set_wedged(True)
        # Прямой вызов внутренностей start() невозможен без библиотеки —
        # проверяем контракт через _reset_session_state(), который start()
        # обязан вызывать под локом.
        self.adapter._reset_session_state()
        hb = self.adapter.heartbeat()
        self.assertIsNone(hb["last_chunk_ts"])
        self.assertIsNone(hb["listen_started_ts"])
        self.assertFalse(self.adapter.is_wedged())


class _FakeThreadCleanExit:
    """Duck-type (НЕ наследник threading.Thread — atexit-hang правило).
    Жив до join, после join — вышел."""

    def __init__(self):
        self._alive = True
        self.join_timeout = None

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.join_timeout = timeout
        self._alive = False


class _FakeThreadHung(_FakeThreadCleanExit):
    def join(self, timeout=None):
        self.join_timeout = timeout  # остаётся _alive=True


class StopReturnsBoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)

    def test_stop_returns_true_when_not_running(self):
        self.assertTrue(self.adapter.stop())

    def test_stop_returns_true_on_clean_exit(self):
        self.adapter._thread = _FakeThreadCleanExit()
        self.assertTrue(self.adapter.stop())

    def test_stop_returns_false_when_thread_hung(self):
        fake = _FakeThreadHung()
        self.adapter._thread = fake
        self.assertFalse(self.adapter.stop())
        self.assertEqual(fake.join_timeout, 3.0)


class StatusFieldsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.adapter = OpenWakeWordAdapter(data_dir=self.tmp)

    def test_status_contains_additive_fields(self):
        result = self.adapter.handle_wake_word_status({})
        self.assertTrue(result["ok"])
        self.assertIn("last_chunk_ts", result)
        self.assertIn("listen_started_ts", result)
        self.assertIn("wedged", result)
        self.assertFalse(result["wedged"])

    def test_set_wedged_roundtrip(self):
        self.adapter.set_wedged(True)
        self.assertTrue(self.adapter.is_wedged())
        self.assertTrue(self.adapter.handle_wake_word_status({})["wedged"])
        self.adapter.set_wedged(False)
        self.assertFalse(self.adapter.is_wedged())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тесты падают по правильной причине**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_wake_word_heartbeat.py -v -p no:cacheprovider`
Expected: FAIL/ERROR — `AttributeError` на `heartbeat`/`set_wedged`/`_reset_session_state`, `TypeError: _listen_loop() got an unexpected keyword argument 'generation'`.

- [ ] **Step 3: Реализация в `openwakeword_adapter.py`**

В `__init__` после `self._last_detection…` (строка ~81) добавить:

```python
        # 2026-07-15 (спека wake-word-watchdog): heartbeat живого захвата.
        # last_chunk_ts штампуется ТОЛЬКО ненулевыми чанками (живой микрофон
        # никогда не отдаёт секунды идеальных int16-нулей; шторм нулей 12-07 и
        # зависшее чтение 13-07 оба оставляют его stale). Всё под self._lock.
        self._last_chunk_ts: float | None = None
        self._listen_started_ts: float | None = None
        # Поколение сессии: отвисший «зомби»-тред старой сессии видит чужое
        # поколение и выходит, не захватывая микрофон параллельно с новым.
        self._generation: int = 0
        # Выставляется watchdog'ом, когда мягкое лечение невозможно/не помогло.
        self._wedged: bool = False
```

Новые публичные методы (рядом с `active_threshold`, строка ~243):

```python
    def heartbeat(self) -> dict[str, float | None]:
        """Снапшот heartbeat'а для watchdog/status (спека 2026-07-15)."""
        with self._lock:
            return {
                "last_chunk_ts": self._last_chunk_ts,
                "listen_started_ts": self._listen_started_ts,
            }

    def set_wedged(self, value: bool) -> None:
        with self._lock:
            self._wedged = bool(value)

    def is_wedged(self) -> bool:
        with self._lock:
            return self._wedged

    def _reset_session_state(self) -> None:
        """Чистое состояние новой сессии. Вызывать ТОЛЬКО под self._lock
        (start()) или в тестах без конкуренции."""
        self._last_chunk_ts = None
        self._listen_started_ts = None
        self._wedged = False
```

В `start()` — после `self._last_detection = None` (строка ~193) добавить:

```python
            self._reset_session_state()
            self._generation += 1
```

и в `kwargs` треда добавить `"generation": self._generation,`.

`stop()` → сигнатура и возврат:

```python
    def stop(self, timeout: float = 3.0) -> bool:
        """Останавливает поток прослушивания.

        Returns:
            True — тред вышел (или не был запущен); False — тред НЕ вышел за
            timeout (застрял внутри PortAudio-вызова). Вызывающий обязан
            считать False сигналом «мягкий reinit небезопасен» (спека
            2026-07-15, вариант клина 13-07).
        """
        with self._lock:
            self._last_detection = None
            # Спека §4.1: heartbeat сбрасывается и в start(), и в stop().
            # wedged здесь НЕ трогаем — флаг обязан пережить pause/resume
            # циклы поллера (wake_word_stop при паузе), его снимает только
            # watchdog по свежему чанку или start() новой сессии.
            self._last_chunk_ts = None
            self._listen_started_ts = None
            if self._thread is None or not self._thread.is_alive():
                return True
            self._stop_event.set()
            thread = self._thread
            self._thread = None
            self._oww = None
            self._active_model = None
            self._active_threshold = None

        thread.join(timeout=timeout)
        exited = not thread.is_alive()
        if exited:
            logger.info("OpenWakeWordAdapter: остановлен")
        else:
            logger.error(
                "OpenWakeWordAdapter: тред слушателя не вышел за %.1fs — "
                "вероятно завис внутри PortAudio (класс инцидента 13-07)",
                timeout,
            )
        return exited
```

`_listen_loop` — сигнатура получает `generation: int`; первой строкой тела (ДО импорта sounddevice):

```python
        with self._lock:
            self._listen_started_ts = time.monotonic()
```

Внутри цикла заменить блок

```python
                    with self._lock:
                        oww = self._oww

                    if oww is None:
                        break
```

на

```python
                    with self._lock:
                        if self._generation != generation:
                            logger.info(
                                "OpenWakeWordAdapter: сессия устарела "
                                "(generation %d != %d) — зомби-тред выходит",
                                generation, self._generation,
                            )
                            break
                        oww = self._oww
                        if flat.any():
                            self._last_chunk_ts = time.monotonic()

                    if oww is None:
                        break
```

`handle_wake_word_status` — в возвращаемый dict добавить (после `last_detection`):

```python
            "last_chunk_ts": hb["last_chunk_ts"],
            "listen_started_ts": hb["listen_started_ts"],
            "wedged": self.is_wedged(),
```

где `hb = self.heartbeat()` вычисляется ВНЕ `with self._lock`-блока метода (как `is_running()`/`active_model()` — lock не реентерабельный).

- [ ] **Step 4: Тесты зелёные + регрессия соседей**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_wake_word_heartbeat.py KrabEar/tests/test_openwakeword_adapter.py KrabEar/tests/test_openwakeword_security_W1210.py KrabEar/tests/test_wake_word_polling_contract.py -v -p no:cacheprovider`
Expected: все PASS (существующие вызовы `stop()` игнорируют возврат — совместимо).

- [ ] **Step 5: Commit**

```bash
git add KrabEar/backend/openwakeword_adapter.py KrabEar/tests/test_wake_word_heartbeat.py
git commit -m "feat(wake-word): heartbeat + generation-токен + stop()->bool + wedged-хранилище

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `AudioReinitCoordinator` — single-flight владелец танца reinit

**Files:**
- Create: `KrabEar/backend/audio_reinit.py`
- Test: `KrabEar/tests/test_audio_reinit_coordinator.py` (create)

- [ ] **Step 1: Написать падающие тесты**

Создать `KrabEar/tests/test_audio_reinit_coordinator.py`:

```python
"""AudioReinitCoordinator — single-flight танец reinit (спека 2026-07-15)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_reinit import AudioReinitCoordinator, ReinitOutcome  # noqa: E402


class _FakeAdapter:
    """Duck-type OpenWakeWordAdapter (см. test_audio_selfheal.py)."""

    def __init__(self, running=True, model="hey_jarvis", threshold=0.63,
                 stop_result=True):
        self._running = running
        self._model = model if running else None
        self._threshold = threshold if running else None
        self._stop_result = stop_result
        self.calls: list[str] = []
        self.start_args: list[tuple] = []

    def is_running(self):
        return self._running

    def active_model(self):
        return self._model

    def active_threshold(self):
        return self._threshold

    def stop(self):
        self.calls.append("stop")
        if self._stop_result:
            self._running = False
        return self._stop_result

    def start(self, model_name, on_detected, threshold=0.5, **kw):
        self.calls.append("start")
        self.start_args.append((model_name, threshold))
        self._running = True
        on_detected("smoke", 0.99)


def _make(adapter=None, recording=False, reinit_exc=None):
    calls: list[str] = []

    def _reinit():
        calls.append("reinit")
        if reinit_exc:
            raise reinit_exc

    coord = AudioReinitCoordinator(
        reinit_audio_backend=_reinit,
        is_recording=lambda: recording,
        wake_word_adapter=adapter,
    )
    return coord, calls


class DanceTests(unittest.TestCase):
    def test_ok_full_dance_order_and_restore(self):
        adapter = _FakeAdapter(running=True, model="krab_ru", threshold=0.42)
        coord, calls = _make(adapter=adapter)
        outcome = coord.reinit_with_wake_word_restore()
        self.assertEqual(outcome, ReinitOutcome.OK)
        self.assertEqual(adapter.calls, ["stop", "start"])
        self.assertEqual(calls, ["reinit"])
        self.assertEqual(adapter.start_args, [("krab_ru", 0.42)])

    def test_ok_without_adapter(self):
        coord, calls = _make(adapter=None)
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        self.assertEqual(calls, ["reinit"])

    def test_listener_not_running_reinit_only(self):
        adapter = _FakeAdapter(running=False)
        coord, calls = _make(adapter=adapter)
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(calls, ["reinit"])

    def test_threshold_none_falls_back_to_default(self):
        adapter = _FakeAdapter(running=True, threshold=None)
        adapter._threshold = None
        coord, _ = _make(adapter=adapter)
        coord.reinit_with_wake_word_restore()
        self.assertEqual(adapter.start_args, [("hey_jarvis", 0.5)])


class GuardTests(unittest.TestCase):
    def test_recording_defers(self):
        adapter = _FakeAdapter()
        coord, calls = _make(adapter=adapter, recording=True)
        self.assertEqual(
            coord.reinit_with_wake_word_restore(),
            ReinitOutcome.DEFERRED_RECORDING,
        )
        self.assertEqual(adapter.calls, [])
        self.assertEqual(calls, [])

    def test_hung_thread_skips_reinit(self):
        adapter = _FakeAdapter(stop_result=False)
        coord, calls = _make(adapter=adapter)
        self.assertEqual(
            coord.reinit_with_wake_word_restore(), ReinitOutcome.THREAD_HUNG,
        )
        self.assertEqual(adapter.calls, ["stop"])
        self.assertEqual(calls, [])  # sd._terminate НЕ вызывался

    def test_legacy_stop_returning_none_is_success(self):
        adapter = _FakeAdapter()
        adapter.stop = lambda: adapter.calls.append("stop")  # -> None
        coord, calls = _make(adapter=adapter)
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        self.assertEqual(calls, ["reinit"])

    def test_busy_when_flight_lock_held(self):
        coord, calls = _make(adapter=None)
        self.assertTrue(coord._flight_lock.acquire(blocking=False))
        try:
            self.assertEqual(
                coord.reinit_with_wake_word_restore(), ReinitOutcome.BUSY,
            )
        finally:
            coord._flight_lock.release()
        self.assertEqual(calls, [])

    def test_reinit_exception_returns_failed_but_restores_listener(self):
        adapter = _FakeAdapter(running=True)
        coord, calls = _make(adapter=adapter, reinit_exc=RuntimeError("boom"))
        self.assertEqual(
            coord.reinit_with_wake_word_restore(), ReinitOutcome.FAILED,
        )
        self.assertEqual(adapter.calls, ["stop", "start"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_audio_reinit_coordinator.py -v -p no:cacheprovider`
Expected: FAIL на импорте — `ModuleNotFoundError: No module named 'backend.audio_reinit'`.

- [ ] **Step 3: Создать `KrabEar/backend/audio_reinit.py`**

```python
"""AudioReinitCoordinator — единственный владелец танца переинициализации
аудио-стека (спека docs/superpowers/specs/2026-07-15-wake-word-watchdog-design.md §4.2).

Танец переехал из AudioSelfHealer._perform_reinit, чтобы AudioSelfHealer
(пассивный триггер — пустые диктовки) и WakeWordWatchdog (активный триггер —
stale heartbeat) делили ОДИН путь лечения с single-flight локом, а не
дрейфующие копии (класс «double-write одного side effect из двух tap'ов»).

Ключевой инвариант: если adapter.stop() вернул False (тред слушателя завис
внутри PortAudio-вызова — сигнатура живого инцидента 2026-07-13), звать
sd._terminate() НЕЛЬЗЯ (Pa_Terminate при заблокированном в библиотеке треде —
риск сегфолта) — возвращаем THREAD_HUNG, лечение уходит на уровень процесса.
"""

from __future__ import annotations

import logging
import threading
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("KrabEar.Backend.AudioReinit")

_WAKE_WORD_THRESHOLD_DEFAULT = 0.5


class ReinitOutcome(str, Enum):
    OK = "ok"
    DEFERRED_RECORDING = "deferred_recording"
    THREAD_HUNG = "thread_hung"
    BUSY = "busy"
    FAILED = "failed"


class AudioReinitCoordinator:
    """Single-flight танец: сохранить wake-word сессию → stop() →
    reinit PortAudio → восстановить сессию.

    Parameters
    ----------
    reinit_audio_backend:
        Zero-arg callable (production: ``sd._terminate(); sd._initialize()``).
    is_recording:
        Zero-arg callable; True — идёт диктовка, reinit откладывается.
    wake_word_adapter:
        Duck-typed OpenWakeWordAdapter (``is_running``/``active_model``/
        ``active_threshold``/``stop``/``start``) или None.
    """

    def __init__(
        self,
        *,
        reinit_audio_backend: Callable[[], None],
        is_recording: Callable[[], bool],
        wake_word_adapter: Any = None,
    ) -> None:
        self._reinit_audio_backend = reinit_audio_backend
        self._is_recording = is_recording
        self._wake_word_adapter = wake_word_adapter
        # Non-blocking single-flight: конкурент получает BUSY и приходит со
        # своим следующим триггером, а не ждёт в блокировке.
        self._flight_lock = threading.Lock()

    def reinit_with_wake_word_restore(self) -> ReinitOutcome:
        if not self._flight_lock.acquire(blocking=False):
            logger.info("AudioReinitCoordinator: reinit уже идёт — BUSY")
            return ReinitOutcome.BUSY
        try:
            return self._dance()
        finally:
            self._flight_lock.release()

    # ------------------------------------------------------------------

    def _dance(self) -> ReinitOutcome:
        try:
            if self._is_recording():
                logger.info(
                    "AudioReinitCoordinator: идёт активная запись — reinit отложен"
                )
                return ReinitOutcome.DEFERRED_RECORDING
        except Exception:
            logger.exception("AudioReinitCoordinator: is_recording() упал")

        saved_model: str | None = None
        saved_threshold: float | None = None
        was_running = False
        adapter = self._wake_word_adapter

        if adapter is not None:
            try:
                was_running = bool(adapter.is_running())
            except Exception:
                logger.exception("AudioReinitCoordinator: is_running() упал")
                was_running = False
            if was_running:
                try:
                    saved_model = adapter.active_model()
                    get_thr = getattr(adapter, "active_threshold", None)
                    saved_threshold = get_thr() if callable(get_thr) else None
                except Exception:
                    logger.exception(
                        "AudioReinitCoordinator: не удалось прочитать "
                        "состояние wake word перед reinit"
                    )
                try:
                    stopped = adapter.stop()
                except Exception:
                    logger.exception("AudioReinitCoordinator: adapter.stop() упал")
                    stopped = False
                # None — легаси duck-type без возврата: трактуем как успех.
                if stopped is False:
                    logger.error(
                        "AudioReinitCoordinator: тред слушателя не вышел — "
                        "Pa_Terminate небезопасен, THREAD_HUNG"
                    )
                    return ReinitOutcome.THREAD_HUNG

        reinit_failed = False
        logger.warning(
            "AudioReinitCoordinator: переинициализация аудио-стека (PortAudio)"
        )
        try:
            self._reinit_audio_backend()
        except Exception:
            logger.exception(
                "AudioReinitCoordinator: reinit_audio_backend завершился с исключением"
            )
            reinit_failed = True

        if adapter is not None and was_running and saved_model:
            try:
                adapter.start(
                    saved_model,
                    self._on_wake_word_detected_after_reinit,
                    threshold=(
                        saved_threshold
                        if saved_threshold is not None
                        else _WAKE_WORD_THRESHOLD_DEFAULT
                    ),
                )
            except Exception:
                logger.exception(
                    "AudioReinitCoordinator: не удалось перезапустить wake word "
                    "после reinit"
                )
                reinit_failed = True

        return ReinitOutcome.FAILED if reinit_failed else ReinitOutcome.OK

    @staticmethod
    def _on_wake_word_detected_after_reinit(model_name: str, score: float) -> None:
        """Доставка детекций агенту идёт через _record_detection() безусловно
        внутри цикла слушателя — этому callback'у достаточно лога."""
        logger.info(
            "AudioReinitCoordinator: wake word обнаружен после reinit "
            "(model=%r, score=%.3f)",
            model_name, score,
        )
```

- [ ] **Step 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_audio_reinit_coordinator.py -v -p no:cacheprovider`
Expected: все PASS.

- [ ] **Step 5: Commit**

```bash
git add KrabEar/backend/audio_reinit.py KrabEar/tests/test_audio_reinit_coordinator.py
git commit -m "feat(audio): AudioReinitCoordinator — single-flight танец reinit c THREAD_HUNG-дискриминатором

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `AudioSelfHealer` — делегация танца координатору

**Files:**
- Modify: `KrabEar/backend/audio_selfheal.py`
- Modify: `KrabEar/tests/test_audio_selfheal.py`

Поведенческий контракт (`record_success`/`record_empty_result`, streak/threshold/escalate) НЕ меняется. Меняется: конструктор (`reinit_audio_backend` + `is_recording` + `wake_word_adapter` → один `reinit_coordinator`), `_perform_reinit`/`_on_wake_word_detected_after_reinit` удаляются, is_recording-defer выражается через `DEFERRED_RECORDING`/`BUSY` исходы (attempted-флаг при них НЕ ставится — попытка отложена, не потрачена).

- [ ] **Step 1: Обновить тесты (падающие)**

В `KrabEar/tests/test_audio_selfheal.py`:

1. Заменить import-блок:

```python
from backend.audio_reinit import ReinitOutcome  # noqa: E402
from backend.audio_selfheal import AudioSelfHealer  # noqa: E402
```

2. Удалить класс `_FakeWakeWordAdapter` (уехал в test_audio_reinit_coordinator.py). Добавить фейк-координатор:

```python
class _FakeCoordinator:
    """Duck-type AudioReinitCoordinator со скриптованными исходами."""

    def __init__(self, outcomes=None):
        self._outcomes = list(outcomes or [])
        self.calls = 0

    def reinit_with_wake_word_restore(self):
        self.calls += 1
        if self._outcomes:
            return self._outcomes.pop(0)
        return ReinitOutcome.OK
```

3. Переписать `_make_healer`:

```python
def _make_healer(*, settings=None, coordinator=None, error_bus=None):
    settings = dict(settings or {})
    coordinator = coordinator or _FakeCoordinator()
    healer = AudioSelfHealer(
        reinit_coordinator=coordinator,
        error_bus=error_bus,
        settings_get=lambda k, d: settings.get(k, d),
    )
    return healer, coordinator
```

4. По всему файлу: ассерты вида `self.assertEqual(calls, ["reinit"])` → `self.assertEqual(coordinator.calls, 1)` (и `[]` → `0`). Тесты про `is_recording=lambda: True` (defer) переписать на исход:

```python
    def test_deferred_outcome_keeps_streak_and_attempt_budget(self):
        coord = _FakeCoordinator(outcomes=[
            ReinitOutcome.DEFERRED_RECORDING, ReinitOutcome.OK,
        ])
        healer, _ = _make_healer(
            settings={"audio_selfheal_empty_threshold": 3}, coordinator=coord,
        )
        for _ in range(3):
            healer.record_empty_result()
        self.assertEqual(coord.calls, 1)          # DEFERRED — попытка не потрачена
        healer.record_empty_result()               # streak всё ещё >= threshold
        self.assertEqual(coord.calls, 2)          # повторная попытка, теперь OK

    def test_busy_outcome_treated_as_deferred(self):
        coord = _FakeCoordinator(outcomes=[ReinitOutcome.BUSY, ReinitOutcome.OK])
        healer, _ = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2}, coordinator=coord,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(coord.calls, 2)

    def test_thread_hung_counts_as_attempt_then_escalates(self):
        coord = _FakeCoordinator(outcomes=[ReinitOutcome.THREAD_HUNG])
        bus = _FakeErrorBus()
        healer, _ = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2},
            coordinator=coord, error_bus=bus,
        )
        healer.record_empty_result()
        healer.record_empty_result()   # attempt (THREAD_HUNG)
        healer.record_empty_result()   # streak снова >= threshold → эскалация
        self.assertEqual(coord.calls, 1)
        self.assertEqual(len(bus.pushed), 1)
        self.assertEqual(bus.pushed[0].code, "audio.stack_wedged")
```

Тесты про wake-word save/restore/callback из этого файла УДАЛИТЬ — они переехали в test_audio_reinit_coordinator.py (Task 2).

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_audio_selfheal.py -v -p no:cacheprovider`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'reinit_coordinator'`.

- [ ] **Step 3: Реализация в `audio_selfheal.py`**

Конструктор:

```python
    def __init__(
        self,
        *,
        reinit_coordinator: Any,
        error_bus: Any = None,
        settings_get: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self._reinit_coordinator = reinit_coordinator
        self._error_bus = error_bus
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda _k, d: d)

        self._lock = threading.Lock()
        self._empty_streak = 0
        self._reinit_attempted_since_last_success = False
```

Обновить докстринги класса/модуля: убрать упоминания `reinit_audio_backend`/`is_recording`/`wake_word_adapter`-параметров, добавить «танец — в `backend.audio_reinit.AudioReinitCoordinator`, is_recording-guard и single-flight живут там». `record_empty_result` (ветку is_recording заменяет outcome-обработка):

```python
        action: str | None = None
        with self._lock:
            self._empty_streak += 1
            threshold = self._threshold()
            if self._empty_streak < threshold:
                return
            if self._reinit_attempted_since_last_success:
                action = "escalate"
            else:
                action = "reinit"

        if action == "reinit":
            from backend.audio_reinit import ReinitOutcome

            outcome = self._reinit_coordinator.reinit_with_wake_word_restore()
            if outcome in (ReinitOutcome.DEFERRED_RECORDING, ReinitOutcome.BUSY):
                # Попытка отложена, не потрачена — следующий пустой результат
                # переоценит streak (семантика прежнего is_recording-defer).
                logger.info(
                    "AudioSelfHealer: reinit отложен координатором (%s)",
                    getattr(outcome, "value", outcome),
                )
                return
            with self._lock:
                self._reinit_attempted_since_last_success = True
        elif action == "escalate":
            self._escalate()
            with self._lock:
                self._empty_streak = 0
                self._reinit_attempted_since_last_success = False
```

Удалить `_perform_reinit`, `_on_wake_word_detected_after_reinit`, константу `_WAKE_WORD_THRESHOLD_DEFAULT` (теперь в audio_reinit.py). `_escalate` не трогать.

- [ ] **Step 4: Тесты зелёные (оба файла)**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_audio_selfheal.py KrabEar/tests/test_audio_reinit_coordinator.py -v -p no:cacheprovider`
Expected: все PASS. (test_audio_selfheal_wiring.py пока КРАСНЫЙ — чинится в Task 6 вместе с проводкой; НЕ гонять его здесь.)

- [ ] **Step 5: Commit**

```bash
git add KrabEar/backend/audio_selfheal.py KrabEar/tests/test_audio_selfheal.py
git commit -m "refactor(audio): AudioSelfHealer делегирует танец reinit координатору

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `WakeWordWatchdog` — детекция staleness + heal + эскалация

**Files:**
- Create: `KrabEar/backend/wake_word_watchdog.py`
- Test: `KrabEar/tests/test_wake_word_watchdog.py` (create)

- [ ] **Step 1: Написать падающие тесты**

Создать `KrabEar/tests/test_wake_word_watchdog.py`:

```python
"""WakeWordWatchdog — матрица check_once + жизненный цикл (спека 2026-07-15).

Всё на фейках с инжектированным clock; реальный тред — только в
LifecycleTests (короткий interval, join по Event).
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_reinit import ReinitOutcome  # noqa: E402
from backend.wake_word_watchdog import WakeWordWatchdog  # noqa: E402


class _FakeAdapter:
    def __init__(self):
        self.running = True
        self.model = "hey_jarvis"
        self.hb = {"last_chunk_ts": None, "listen_started_ts": None}
        self.wedged = False

    def is_running(self):
        return self.running

    def active_model(self):
        return self.model

    def heartbeat(self):
        return dict(self.hb)

    def set_wedged(self, v):
        self.wedged = bool(v)

    def is_wedged(self):
        return self.wedged


class _FakeCoordinator:
    def __init__(self, outcomes=None):
        self._outcomes = list(outcomes or [])
        self.calls = 0

    def reinit_with_wake_word_restore(self):
        self.calls += 1
        return self._outcomes.pop(0) if self._outcomes else ReinitOutcome.OK


class _FakeErrorBus:
    def __init__(self):
        self.pushed = []

    def push(self, err):
        self.pushed.append(err)
        return True


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _make(adapter=None, coordinator=None, bus=None, settings=None, clock=None):
    settings = dict(settings or {})
    adapter = adapter or _FakeAdapter()
    coordinator = coordinator or _FakeCoordinator()
    clock = clock or _Clock()
    wd = WakeWordWatchdog(
        adapter=adapter,
        reinit_coordinator=coordinator,
        error_bus=bus,
        settings_get=lambda k, d: settings.get(k, d),
        clock=clock,
    )
    return wd, adapter, coordinator, clock


class CheckOnceGuardTests(unittest.TestCase):
    def test_disabled_noop(self):
        wd, adapter, coord, clock = _make(
            settings={"wake_word_watchdog_enabled": False},
        )
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": 0.0}
        clock.t = 1000.0
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)

    def test_inactive_session_noop_and_resets_episode(self):
        wd, adapter, coord, clock = _make()
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": 0.0}
        clock.t = 1000.0
        self.assertEqual(wd.check_once(), "healed")   # эпизод открыт
        adapter.running = False
        self.assertIsNone(wd.check_once())            # сессии нет → сброс
        adapter.running = True
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": clock.t}
        clock.t += 40.0
        self.assertEqual(wd.check_once(), "healed")   # новый эпизод: heal снова доступен

    def test_started_none_is_fresh(self):
        wd, adapter, coord, _ = _make()
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": None}
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)

    def test_warmup_grace_within_threshold(self):
        wd, adapter, coord, clock = _make()
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": 990.0}
        clock.t = 1000.0   # staleness 10 < 30
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)

    def test_fresh_chunk_noop(self):
        wd, adapter, coord, clock = _make()
        adapter.hb = {"last_chunk_ts": 995.0, "listen_started_ts": 900.0}
        clock.t = 1000.0
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)


class HealAndEscalateTests(unittest.TestCase):
    def _stale(self, adapter, clock):
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": clock.t - 60.0}

    def test_stale_triggers_single_heal(self):
        wd, adapter, coord, clock = _make()
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "healed")
        self.assertEqual(coord.calls, 1)

    def test_heal_does_not_close_episode_until_real_chunk(self):
        # Ловушка: после heal новая сессия даёт свежий listen_started_ts —
        # grace-окно НЕ должно сбрасывать эпизод, иначе watchdog зациклится
        # heal'ом каждые ~35с и никогда не эскалирует.
        wd, adapter, coord, clock = _make()
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "healed")
        # heal «перезапустил» сессию: started свежий, чанков всё ещё нет
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": clock.t}
        clock.t += 10.0
        self.assertIsNone(wd.check_once())            # grace — но эпизод жив
        clock.t += 35.0                                # снова stale
        self.assertEqual(wd.check_once(), "escalated")
        self.assertTrue(adapter.wedged)
        self.assertEqual(coord.calls, 1)              # второго heal НЕ было

    def test_real_chunk_closes_episode_and_clears_wedged(self):
        wd, adapter, coord, clock = _make()
        self._stale(adapter, clock)
        wd.check_once()                                # healed
        adapter.wedged = True                          # как будто эскалировали раньше
        adapter.hb = {"last_chunk_ts": clock.t - 1.0, "listen_started_ts": clock.t - 90.0}
        self.assertIsNone(wd.check_once())
        self.assertFalse(adapter.wedged)
        # эпизод закрыт: новый stale → heal доступен снова
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "healed")
        self.assertEqual(coord.calls, 2)

    def test_thread_hung_escalates_immediately(self):
        bus = _FakeErrorBus()
        coord = _FakeCoordinator(outcomes=[ReinitOutcome.THREAD_HUNG])
        wd, adapter, _, clock = _make(coordinator=coord, bus=bus)
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "escalated")
        self.assertTrue(adapter.wedged)
        self.assertEqual(len(bus.pushed), 1)
        self.assertEqual(bus.pushed[0].code, "audio.wakeword_wedged")

    def test_deferred_keeps_retrying_without_burning_attempt(self):
        coord = _FakeCoordinator(outcomes=[
            ReinitOutcome.DEFERRED_RECORDING, ReinitOutcome.BUSY, ReinitOutcome.OK,
        ])
        wd, adapter, _, clock = _make(coordinator=coord)
        self._stale(adapter, clock)
        self.assertIsNone(wd.check_once())     # deferred
        self.assertIsNone(wd.check_once())     # busy
        self.assertEqual(wd.check_once(), "healed")
        self.assertEqual(coord.calls, 3)

    def test_escalation_fires_once_per_episode(self):
        bus = _FakeErrorBus()
        coord = _FakeCoordinator(outcomes=[ReinitOutcome.THREAD_HUNG])
        wd, adapter, _, clock = _make(coordinator=coord, bus=bus)
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "escalated")
        clock.t += 10.0
        self.assertIsNone(wd.check_once())     # молчим до конца эпизода
        self.assertEqual(len(bus.pushed), 1)

    def test_failed_outcome_counts_as_attempt(self):
        coord = _FakeCoordinator(outcomes=[ReinitOutcome.FAILED])
        wd, adapter, _, clock = _make(coordinator=coord)
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "healed")
        clock.t += 40.0
        self.assertEqual(wd.check_once(), "escalated")

    def test_stale_sec_clamped_from_settings(self):
        wd, adapter, coord, clock = _make(settings={"wake_word_stale_sec": 1})
        # clamp к 10: staleness 5 НЕ алармит
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": clock.t - 5.0}
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)


class StateTests(unittest.TestCase):
    def test_state_dict_shape(self):
        wd, adapter, _, clock = _make()
        adapter.hb = {"last_chunk_ts": clock.t - 2.0, "listen_started_ts": clock.t - 50.0}
        state = wd.state()
        self.assertTrue(state["enabled"])
        self.assertTrue(state["session_active"])
        self.assertAlmostEqual(state["staleness_sec"], 2.0, places=3)
        self.assertFalse(state["heal_attempted_this_episode"])
        self.assertFalse(state["wedged"])


class LifecycleTests(unittest.TestCase):
    def test_start_stop_real_thread(self):
        wd, adapter, _, _ = _make()
        adapter.running = False
        wd._check_interval_sec = 0.05
        wd.start()
        self.assertTrue(wd._thread.is_alive())
        wd.start()   # идемпотентен
        wd.stop()
        self.assertFalse(wd._thread is not None and wd._thread.is_alive())
        wd.stop()    # идемпотентен

    def test_tick_exception_does_not_kill_thread(self):
        wd, adapter, _, _ = _make()
        adapter.is_running = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        wd._check_interval_sec = 0.02
        wd.start()
        done = threading.Event()
        done.wait(0.1)
        self.assertTrue(wd._thread.is_alive())
        wd.stop()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_wake_word_watchdog.py -v -p no:cacheprovider`
Expected: FAIL на импорте `backend.wake_word_watchdog`.

- [ ] **Step 3: Создать `KrabEar/backend/wake_word_watchdog.py`**

```python
"""WakeWordWatchdog — активный сторож независимого wake-word аудио-потока
(спека docs/superpowers/specs/2026-07-15-wake-word-watchdog-design.md §4.3).

Закрывает подтверждённый пробел покрытия: AudioSelfHealer триггерится только
пустыми ДИКТОВКАМИ, а заклинивший wake-word поток (тред жив, CoreAudio не
отдаёт кадры — живой инцидент 2026-07-13, Sentry KRAB-EAR-BACKEND-1J) для него
невидим, wake_word_status.running при этом врёт true (голый thread.is_alive()).

Семантика эпизода: «эпизод» — непрерывный интервал staleness внутри одной
сессии слушателя. Закрывается ТОЛЬКО реальным свежим чанком (не свежим
listen_started_ts! — иначе после heal новая сессия закрывала бы эпизод своим
grace-окном и watchdog зациклился бы heal'ом, никогда не эскалируя),
неактивной сессией или рестартом процесса.

Направление отказа — fail-safe: ложный staleness стоит один лишний цикл
stop/reinit/start (~1-2с тишины микрофона) один раз на эпизод; исключение в
тике ловится и логируется, тред живёт.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger("KrabEar.Backend.WakeWordWatchdog")

_STALE_SEC_MIN = 10.0
_STALE_SEC_MAX = 120.0
_STALE_SEC_DEFAULT = 30.0
_CHECK_INTERVAL_SEC_DEFAULT = 5.0


class WakeWordWatchdog:
    """Таймер-тред: проверяет heartbeat слушателя, лечит через координатор,
    эскалирует wedged-флагом + ErrorBus.

    Все коллабораторы инжектятся (duck-typed) — тестируется фейками без
    sounddevice/реального адаптера:
      adapter: is_running(), active_model(), heartbeat(), set_wedged(), is_wedged()
      reinit_coordinator: reinit_with_wake_word_restore() -> ReinitOutcome
      error_bus: push(KrabError) | None
      settings_get: (key, default) -> Any
      clock: () -> float (monotonic)
    """

    def __init__(
        self,
        *,
        adapter: Any,
        reinit_coordinator: Any,
        error_bus: Any = None,
        settings_get: Callable[[str, Any], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        check_interval_sec: float = _CHECK_INTERVAL_SEC_DEFAULT,
    ) -> None:
        self._adapter = adapter
        self._coordinator = reinit_coordinator
        self._error_bus = error_bus
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda _k, d: d)
        self._clock = clock
        self._check_interval_sec = check_interval_sec

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._heal_attempted_this_episode = False
        self._escalated_this_episode = False

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _enabled(self) -> bool:
        try:
            return bool(self._settings_get("wake_word_watchdog_enabled", True))
        except Exception:
            return True

    def _stale_sec(self) -> float:
        try:
            value = float(self._settings_get("wake_word_stale_sec", _STALE_SEC_DEFAULT))
        except (TypeError, ValueError):
            value = _STALE_SEC_DEFAULT
        return max(_STALE_SEC_MIN, min(_STALE_SEC_MAX, value))

    # ------------------------------------------------------------------
    # Lifecycle (start()/stop() — stop() обязателен в BackendService.close())
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="WakeWordWatchdog",
            )
            self._thread.start()
        logger.info(
            "WakeWordWatchdog: запущен (interval=%.1fs)", self._check_interval_sec,
        )

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.wait(self._check_interval_sec):
            try:
                self.check_once()
            except Exception:
                logger.exception("WakeWordWatchdog: тик упал")

    # ------------------------------------------------------------------
    # Один тик (чистая логика — юниты зовут напрямую)
    # ------------------------------------------------------------------

    def check_once(self) -> str | None:
        """Возвращает выполненное действие: "healed" | "escalated" | None."""
        if not self._enabled():
            return None

        try:
            session_active = bool(self._adapter.is_running()) and (
                self._adapter.active_model() is not None
            )
        except Exception:
            logger.exception("WakeWordWatchdog: опрос адаптера упал")
            return None

        if not session_active:
            # Легитимные паузы (recording/conversation/TTS/privacy) выглядят
            # именно так — Swift шлёт wake_word_stop. Эпизод сбрасывается.
            self._reset_episode()
            return None

        hb = self._adapter.heartbeat()
        started = hb.get("listen_started_ts")
        last = hb.get("last_chunk_ts")
        if started is None:
            return None  # тред спавнут, но не вошёл в цикл — свежий

        now = self._clock()
        stale_sec = self._stale_sec()

        # Эпизод закрывает ТОЛЬКО реальный свежий чанк (см. докстринг модуля).
        if last is not None and (now - last) < stale_sec:
            self._close_episode_fresh()
            return None

        staleness = now - max(started, last or 0.0)
        if staleness < stale_sec:
            return None  # grace-окно прогрева: не алармим и не закрываем эпизод

        with self._lock:
            heal_tried = self._heal_attempted_this_episode
            escalated = self._escalated_this_episode
        if escalated:
            return None

        if not heal_tried:
            from backend.audio_reinit import ReinitOutcome

            logger.warning(
                "WakeWordWatchdog: heartbeat stale %.1fs (порог %.1fs) — "
                "мягкое лечение через координатор",
                staleness, stale_sec,
            )
            outcome = self._coordinator.reinit_with_wake_word_restore()
            if outcome in (ReinitOutcome.DEFERRED_RECORDING, ReinitOutcome.BUSY):
                return None  # попытка отложена, не потрачена
            if outcome == ReinitOutcome.THREAD_HUNG:
                self._escalate(staleness, str(getattr(outcome, "value", outcome)))
                return "escalated"
            with self._lock:
                self._heal_attempted_this_episode = True
            return "healed"

        self._escalate(staleness, "stale_after_reinit")
        return "escalated"

    # ------------------------------------------------------------------

    def _reset_episode(self) -> None:
        with self._lock:
            self._heal_attempted_this_episode = False
            self._escalated_this_episode = False

    def _close_episode_fresh(self) -> None:
        self._reset_episode()
        try:
            if self._adapter.is_wedged():
                logger.info("WakeWordWatchdog: heartbeat ожил — снимаю wedged")
                self._adapter.set_wedged(False)
        except Exception:
            logger.exception("WakeWordWatchdog: сброс wedged упал")

    def _escalate(self, staleness: float, reason: str) -> None:
        with self._lock:
            self._heal_attempted_this_episode = True
            self._escalated_this_episode = True
        logger.error(
            "WakeWordWatchdog: мягкое лечение невозможно/не помогло (%s, "
            "staleness=%.1fs) — wedged:true, лечение на стороне агента",
            reason, staleness,
        )
        try:
            self._adapter.set_wedged(True)
        except Exception:
            logger.exception("WakeWordWatchdog: set_wedged упал")
        if self._error_bus is None:
            return
        try:
            from datetime import datetime, timezone

            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY

            entry = ERROR_REGISTRY.get("audio.wakeword_wedged", {})
            self._error_bus.push(KrabError(
                severity=entry.get("severity", "error"),
                component="audio",
                code="audio.wakeword_wedged",
                message_user=entry.get(
                    "user_msg_ru", "Wake word завис — перезапускаю Krab Ear…",
                ),
                message_debug=(
                    f"wake-word heartbeat stale {staleness:.1f}s, reason={reason}"
                ),
                timestamp=datetime.now(timezone.utc),
                context={"staleness_sec": round(staleness, 1), "reason": reason},
                actionable=False,
                action_id=None,
            ))
        except Exception:
            logger.exception("WakeWordWatchdog: ErrorBus.push упал при эскалации")

    # ------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Снапшот для get_diagnostics."""
        try:
            session_active = bool(self._adapter.is_running()) and (
                self._adapter.active_model() is not None
            )
        except Exception:
            session_active = False
        staleness: float | None = None
        try:
            hb = self._adapter.heartbeat()
            started = hb.get("listen_started_ts")
            last = hb.get("last_chunk_ts")
            if started is not None:
                staleness = self._clock() - max(started, last or 0.0)
        except Exception:
            pass
        wedged = False
        try:
            wedged = bool(self._adapter.is_wedged())
        except Exception:
            pass
        with self._lock:
            heal_attempted = self._heal_attempted_this_episode
        return {
            "enabled": self._enabled(),
            "session_active": session_active,
            "staleness_sec": round(staleness, 3) if staleness is not None else None,
            "heal_attempted_this_episode": heal_attempted,
            "wedged": wedged,
        }
```

Примечание к тесту `test_inactive_session_noop_and_resets_episode`: первый `check_once()` с `listen_started_ts=0.0` и `clock=1000.0` даёт staleness 1000 → heal. Убедись, что фейк-координатор по умолчанию отвечает OK.

- [ ] **Step 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_wake_word_watchdog.py -v -p no:cacheprovider`
Expected: все PASS. (Тест `bus.pushed[0].code == "audio.wakeword_wedged"` пройдёт и ДО Task 5 — код берётся из fallback-ветки `entry.get(...)`; KrabError импортируется из error_bus, реестр может ещё не знать код — это ок.)

- [ ] **Step 5: Commit**

```bash
git add KrabEar/backend/wake_word_watchdog.py KrabEar/tests/test_wake_word_watchdog.py
git commit -m "feat(wake-word): WakeWordWatchdog — staleness-детекция, heal, эскалация wedged

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Настройки + validator + error-код

**Files:**
- Modify: `KrabEar/core/config.py` (DEFAULT_SETTINGS, рядом со строкой ~814 `audio_selfheal_*`)
- Modify: `KrabEar/backend/settings_validator.py`
- Modify: `KrabEar/backend/error_codes.py` (в конец, после `audio.stack_wedged`)
- Test: `KrabEar/tests/test_wake_word_watchdog_settings.py` (create)

- [ ] **Step 1: Написать падающие тесты**

```python
"""Настройки/validator/error-код wake-word watchdog (спека 2026-07-15)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.error_codes import ERROR_REGISTRY  # noqa: E402
from backend.settings_validator import _BOOL_FIELDS, _RANGE_FIELDS  # noqa: E402
from core.config import DEFAULT_SETTINGS  # noqa: E402


class DefaultsTests(unittest.TestCase):
    def test_defaults_present(self):
        self.assertIs(DEFAULT_SETTINGS["wake_word_watchdog_enabled"], True)
        self.assertEqual(DEFAULT_SETTINGS["wake_word_stale_sec"], 30.0)

    def test_validator_fields(self):
        self.assertIn("wake_word_watchdog_enabled", _BOOL_FIELDS)
        self.assertEqual(
            _RANGE_FIELDS["wake_word_stale_sec"], (10.0, 120.0, 30.0, float),
        )


class ErrorCodeTests(unittest.TestCase):
    def test_registry_entry(self):
        entry = ERROR_REGISTRY["audio.wakeword_wedged"]
        self.assertEqual(entry["severity"], "error")
        self.assertFalse(entry["actionable"])
        self.assertIn("Wake word", entry["user_msg_ru"])
        self.assertEqual(entry["dedupe_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что падают** (KeyError на всех трёх местах)

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_wake_word_watchdog_settings.py -v -p no:cacheprovider`

- [ ] **Step 3: Реализация**

`core/config.py`, сразу после `"audio_selfheal_empty_threshold": 3,` (~строка 815):

```python
    # Wake-word watchdog (спека 2026-07-15): активный сторож независимого
    # wake-word потока — heartbeat staleness → мягкий reinit → wedged-эскалация.
    "wake_word_watchdog_enabled": True,
    "wake_word_stale_sec": 30.0,
```

`settings_validator.py`: в `_BOOL_FIELDS` добавить `"wake_word_watchdog_enabled": True,`; в `_RANGE_FIELDS` добавить `"wake_word_stale_sec": (10.0, 120.0, 30.0, float),`.

`error_codes.py`, после блока `audio.stack_wedged` (внутри dict, перед закрывающей `}`):

```python
    # audio.wakeword_wedged — WakeWordWatchdog (backend/wake_word_watchdog.py).
    # Root cause: живой инцидент 2026-07-13 — wake-word _listen_loop тихо завис
    # до активации CoreAudio (тред жив, кадров нет, исключений нет; Sentry
    # KRAB-EAR-BACKEND-1J, PaErrorCode -9986). Мягкий reinit невозможен
    # (тред застрял внутри PortAudio) или не помог — Swift-агент по этому коду
    # и по wedged:true в wake_word_status выполняет принудительный рестарт
    # backend (launchctl kickstart -k, rate-limit 30 мин на стороне агента).
    "audio.wakeword_wedged": {
        "user_msg_ru": (
            "Wake word завис — перезапускаю Krab Ear…"
        ),
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "error",
        "dedupe_seconds": 300,
    },
```

- [ ] **Step 4: Тесты зелёные + существующие тесты реестра/валидатора**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_wake_word_watchdog_settings.py KrabEar/tests/test_error_codes.py KrabEar/tests/test_settings_validator.py -v -p no:cacheprovider`
Expected: PASS (если test_settings_validator.py не существует — прогнать только первые два; если test_error_codes.py ассертит точное число кодов — обновить это число на +1 в том же коммите).

- [ ] **Step 5: Commit**

```bash
git add KrabEar/core/config.py KrabEar/backend/settings_validator.py KrabEar/backend/error_codes.py KrabEar/tests/test_wake_word_watchdog_settings.py
git commit -m "feat(settings): wake_word_watchdog_enabled/wake_word_stale_sec + audio.wakeword_wedged

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

(Плюс `git add KrabEar/tests/test_error_codes.py`, если правил число кодов.)

---

### Task 6: Проводка в `service.py` + `close()` + `get_diagnostics`

**Files:**
- Modify: `KrabEar/backend/service.py` (блок ~1021-1042 + `close()` ~1419 + конструкция HealthCheckService)
- Modify: `KrabEar/backend/health_check_service.py`
- Modify: `KrabEar/tests/test_audio_selfheal_wiring.py`

- [ ] **Step 1: Обновить wiring-тест (падающий)**

Прочитай `KrabEar/tests/test_audio_selfheal_wiring.py` целиком и адаптируй его ассерты к новой проводке, сохранив его подход к конструированию `BackendService` (и обязательный `self.service.close()` в `tearDown` — правило #1782). Новые обязательные ассерты (добавить классом или влить в существующий):

```python
    def test_reinit_coordinator_wired(self):
        coord = self.service._audio_reinit_coordinator
        self.assertIsNotNone(coord)
        self.assertIs(coord._wake_word_adapter, self.service._oww_adapter)
        self.assertIs(self.service._audio_selfheal._reinit_coordinator, coord)

    def test_watchdog_wired_and_running(self):
        wd = self.service._wake_word_watchdog
        self.assertIsNotNone(wd)
        self.assertIs(wd._adapter, self.service._oww_adapter)
        self.assertIs(wd._coordinator, self.service._audio_reinit_coordinator)
        self.assertTrue(wd._thread is not None and wd._thread.is_alive())

    def test_close_stops_watchdog_thread(self):
        wd = self.service._wake_word_watchdog
        self.service.close()
        self.assertFalse(wd._thread is not None and wd._thread.is_alive())

    def test_diagnostics_contains_watchdog_section(self):
        diag = self.service.handle_request(
            {"id": "t", "method": "get_diagnostics", "params": {}},
        )
        section = diag["result"]["wake_word_watchdog"]
        self.assertIn("enabled", section)
        self.assertIn("wedged", section)
```

(Если существующий файл ассертит старые аргументы хилера — `reinit_audio_backend`/`wake_word_adapter` — эти ассерты заменить на координаторные выше.)

- [ ] **Step 2: Убедиться, что падают**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_audio_selfheal_wiring.py -v -p no:cacheprovider`
Expected: FAIL — у сервиса нет `_audio_reinit_coordinator`/`_wake_word_watchdog`; конструктор хилера упадёт на старых kwargs.

- [ ] **Step 3: Проводка в `service.py`**

Импорты (рядом с `from backend.audio_selfheal import AudioSelfHealer`, строка ~103):

```python
from backend.audio_reinit import AudioReinitCoordinator
from backend.wake_word_watchdog import WakeWordWatchdog
```

Блок ~1021-1042 — заменить конструкцию хилера на:

```python
        def _reinit_audio_backend() -> None:
            try:
                import sounddevice as _sd  # type: ignore
            except Exception:
                logger.warning("AudioReinit: sounddevice недоступен, reinit пропущен")
                return
            _sd._terminate()
            _sd._initialize()

        # 2026-07-15 (спека wake-word-watchdog): единый single-flight владелец
        # танца reinit — им пользуются пассивный AudioSelfHealer (пустые
        # диктовки) и активный WakeWordWatchdog (stale heartbeat).
        self._audio_reinit_coordinator = AudioReinitCoordinator(
            reinit_audio_backend=_reinit_audio_backend,
            is_recording=lambda: bool(getattr(self.recorder, "is_recording", False)),
            wake_word_adapter=self._oww_adapter,
        )
        self._audio_selfheal = AudioSelfHealer(
            reinit_coordinator=self._audio_reinit_coordinator,
            error_bus=self._error_bus,
            settings_get=self._get_runtime_setting,
        )
        self._recording_core_svc._audio_selfheal = self._audio_selfheal
        # Активный сторож wake-word потока (живой инцидент 2026-07-13):
        # heartbeat staleness → мягкий reinit → wedged:true (эскалация на
        # Swift-agent, который выполняет kickstart -k). Останавливается в
        # close() — правило #1782 про daemon-треды в chunked CI.
        self._wake_word_watchdog = WakeWordWatchdog(
            adapter=self._oww_adapter,
            reinit_coordinator=self._audio_reinit_coordinator,
            error_bus=self._error_bus,
            settings_get=self._get_runtime_setting,
        )
        self._wake_word_watchdog.start()
```

ВНИМАНИЕ: `self._error_bus` на строке ~1039 уже существует к этому месту (хилер его получал) — порядок конструирования не менять.

В `close()` (после блока DiskSpaceMonitor, ~строка 1449) добавить:

```python
        # Stop WakeWordWatchdog daemon thread — same CI daemon-thread teardown
        # rule (feedback_backendservice_teardown_ci.md).
        watchdog = getattr(self, "_wake_word_watchdog", None)
        if watchdog is not None:
            try:
                watchdog.stop()
            except Exception:
                logger.exception("WakeWordWatchdog.stop() raised during close()")
```

- [ ] **Step 4: Секция diagnostics в `health_check_service.py`**

В конструкторе `HealthCheckService` (рядом с «Optional collaborators for get_diagnostics», строка ~47) добавить kwarg `wake_word_watchdog: Any = None` и `self._wake_word_watchdog = wake_word_watchdog`. В возвращаемом dict `handle_get_diagnostics` добавить ключ (на верхнем уровне, рядом с `"llm"`):

```python
            "wake_word_watchdog": (
                self._wake_word_watchdog.state()
                if self._wake_word_watchdog is not None
                else {"enabled": False, "wired": False}
            ),
```

В `service.py` найти конструкцию `HealthCheckService(` (grep) и передать `wake_word_watchdog=self._wake_word_watchdog,`. ВАЖНО: если HealthCheckService конструируется РАНЬШЕ watchdog'а в `__init__` — передать late-inject присваиванием сразу после создания watchdog'а (паттерн `_recording_core_svc._audio_selfheal` выше):

```python
        self._health_check_svc._wake_word_watchdog = self._wake_word_watchdog
```

- [ ] **Step 5: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_audio_selfheal_wiring.py KrabEar/tests/test_audio_selfheal.py KrabEar/tests/test_wake_word_watchdog.py -v -p no:cacheprovider`
Expected: все PASS.

- [ ] **Step 6: Commit**

```bash
git add KrabEar/backend/service.py KrabEar/backend/health_check_service.py KrabEar/tests/test_audio_selfheal_wiring.py
git commit -m "feat(wiring): координатор+watchdog в BackendService, stop в close(), diagnostics-секция

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Swift — `WedgedEscalationTracker`, обработка wedged, `forceRestartBackend()`

**Files:**
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/BackendSupervisor.swift`
- Modify: `native/KrabEarAgent/Sources/KrabEarAgent/main.swift` (setupWakeWordListenerIfEnabled, ~строка 520)
- Test: `native/KrabEarAgent/Tests/KrabEarAgentTests/WedgedEscalationTrackerTests.swift` (create)
- Modify: `native/KrabEarAgent/Tests/KrabEarAgentTests/BackendSupervisorTests.swift`

- [ ] **Step 1: Написать падающие Swift-тесты**

Создать `WedgedEscalationTrackerTests.swift`:

```swift
import XCTest
@testable import KrabEarAgent

final class WedgedEscalationTrackerTests: XCTestCase {

    func test_notWedged_neverEscalates() {
        var t = WedgedEscalationTracker()
        XCTAssertFalse(t.shouldEscalate(wedged: false, now: 100))
        XCTAssertFalse(t.shouldEscalate(wedged: false, now: 100_000))
    }

    func test_firstWedged_escalates() {
        var t = WedgedEscalationTracker()
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: 100))
    }

    func test_withinGap_suppressed() {
        var t = WedgedEscalationTracker()
        _ = t.shouldEscalate(wedged: true, now: 100)
        XCTAssertFalse(t.shouldEscalate(wedged: true, now: 100 + WedgedEscalationTracker.minGapSec - 1))
    }

    func test_afterGap_escalatesAgain() {
        var t = WedgedEscalationTracker()
        _ = t.shouldEscalate(wedged: true, now: 100)
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: 100 + WedgedEscalationTracker.minGapSec))
    }

    func test_reset_rearms() {
        var t = WedgedEscalationTracker()
        _ = t.shouldEscalate(wedged: true, now: 100)
        t.reset()
        XCTAssertTrue(t.shouldEscalate(wedged: true, now: 101))
    }
}
```

В `BackendSupervisorTests.swift` добавить:

```swift
    func test_kickstartArguments_shape() {
        XCTAssertEqual(
            BackendSupervisor.kickstartArguments(uid: 501),
            ["kickstart", "-k", "gui/501/ai.krab.ear.backend"]
        )
    }
```

- [ ] **Step 2: Убедиться, что не компилируется**

Run: `cd native/KrabEarAgent && swift test --filter WedgedEscalationTrackerTests 2>&1 | tail -5`
Expected: compile error — `WedgedEscalationTracker` не существует.

- [ ] **Step 3: Реализация**

`WakeWordPoller.swift` — рядом с `WakeWordDetectionTracker` добавить:

```swift
// MARK: - Решение об эскалации wedged (чистая логика, без таймеров/IPC)

/// Backend сообщил wedged:true (wake-word поток заклинил, мягкое лечение
/// невозможно/не помогло — спека 2026-07-15). Разрешаем принудительный
/// рестарт backend не чаще раза в minGapSec.
struct WedgedEscalationTracker {
    static let minGapSec: TimeInterval = 1800  // 30 минут

    private var lastEscalationAt: TimeInterval?

    mutating func shouldEscalate(wedged: Bool, now: TimeInterval) -> Bool {
        guard wedged else { return false }
        if let last = lastEscalationAt, now - last < Self.minGapSec { return false }
        lastEscalationAt = now
        return true
    }

    mutating func reset() { lastEscalationAt = nil }
}
```

В `WakeWordPoller`:
- поле `private var wedgedTracker = WedgedEscalationTracker()` (рядом с `tracker`);
- свойство `private let onWedgedEscalation: (() -> Void)?` + параметр init `onWedgedEscalation: (() -> Void)? = nil` (после `onDetection`, дефолт nil сохраняет совместимость всех существующих call-site'ов, включая тесты);
- в `activate()` после `tracker.reset()` добавить `wedgedTracker.reset()`;
- в `tick()` ПОСЛЕ блока детекции (`if self.tracker.shouldTrigger…return }`) и ПЕРЕД `if !running` вставить:

```swift
                // Эскалация wedged: backend сам не смог вылечить wake-word
                // поток (спека 2026-07-15) — просим принудительный рестарт.
                let wedged = result["wedged"] as? Bool ?? false
                if self.wedgedTracker.shouldEscalate(
                    wedged: wedged, now: ProcessInfo.processInfo.systemUptime
                ) {
                    AgentLogger.shared.warn(
                        "[WakeWord] backend сообщил wedged — эскалация: принудительный рестарт backend")
                    self.onWedgedEscalation?()
                    return
                }
```

`BackendSupervisor.swift` — рядом с `restartIfDead()` добавить:

```swift
    /// launchd-label backend-сервиса (scripts/install_backend_launchagent.command).
    static let backendLaunchdLabel = "ai.krab.ear.backend"

    /// Аргументы launchctl для принудительного рестарта launchd-owned backend.
    /// Выделено в чистую функцию для юнит-тестов.
    static func kickstartArguments(uid: uid_t) -> [String] {
        ["kickstart", "-k", "gui/\(uid)/\(backendLaunchdLabel)"]
    }

    /// Принудительный рестарт ЗАВЕДОМО ЖИВОГО backend-процесса (audio-wedge:
    /// жив по IPC, мёртв по аудио — спека 2026-07-15). НЕ смешивать с
    /// restartIfDead(): тот short-circuit'ится на живом процессе, а
    /// stopBackend() в passive-режиме — no-op. В passive это ровно ручной
    /// рецепт живого инцидента 13-07: launchctl kickstart -k.
    func forceRestartBackend() -> Bool {
        switch supervisionMode {
        case .passive:
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            p.arguments = Self.kickstartArguments(uid: getuid())
            do {
                try p.run()
            } catch {
                return false
            }
            p.waitUntilExit()
            return p.terminationStatus == 0
        case .active:
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

Перед реализацией провериь label: `grep -n "ai.krab.ear.backend" scripts/install_backend_launchagent.command` — должен совпасть с константой.

`main.swift` — в `setupWakeWordListenerIfEnabled()` конструкции `WakeWordPoller(` добавить параметр после `onDetection`:

```swift
                onWedgedEscalation: { [weak self] in
                    guard let self else { return }
                    self.logger.warn("Wake word: backend wedged — принудительный рестарт backend")
                    BackendToast.shared.show("Wake word завис — перезапускаю backend…", duration: 5.0)
                    DispatchQueue.global(qos: .utility).async { [weak self] in
                        guard let self else { return }
                        let ok = self.backendSupervisor.forceRestartBackend()
                        DispatchQueue.main.async {
                            BackendToast.shared.show(
                                ok ? "Backend перезапущен (wake word)"
                                   : "⚠ Рестарт backend не удался — перезапустите Krab Ear вручную",
                                duration: ok ? 3.0 : 10.0
                            )
                        }
                    }
                }
```

(Проверь сигнатуру `AgentLogger`/`logger` в контексте `setupWakeWordListenerIfEnabled` — там используется `logger.info`, значит `self.logger.warn(...)` доступен; если метод называется иначе (`warning`), используй его.)

- [ ] **Step 4: Сборка и Swift-тесты зелёные**

Run: `cd native/KrabEarAgent && swift build -c release 2>&1 | tail -3 && swift test --filter "WedgedEscalationTrackerTests|BackendSupervisorTests" 2>&1 | tail -5`
Expected: Build complete; тесты PASS.

- [ ] **Step 5: Commit**

```bash
git add native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift native/KrabEarAgent/Sources/KrabEarAgent/BackendSupervisor.swift native/KrabEarAgent/Sources/KrabEarAgent/main.swift native/KrabEarAgent/Tests/KrabEarAgentTests/WedgedEscalationTrackerTests.swift native/KrabEarAgent/Tests/KrabEarAgentTests/BackendSupervisorTests.swift
git commit -m "feat(agent): wedged-эскалация — WedgedEscalationTracker + forceRestartBackend (kickstart -k)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: e2e-смок + полные гейты волны

**Files:**
- Modify: `scripts/e2e_ipc_smoke.py`

- [ ] **Step 1: Дополнить смок**

Прочитай `scripts/e2e_ipc_smoke.py`, найди его идиому «вызвал метод → проверил санити результата» и добавь по ней две проверки:

1. `wake_word_status` → `ok is True`, ключи `wedged` (bool), `last_chunk_ts`, `listen_started_ts` присутствуют (значения могут быть None — движок на throwaway-инстансе не запущен).
2. `get_diagnostics` → в результате есть `wake_word_watchdog` c ключами `enabled` (bool) и `wedged`.

- [ ] **Step 2: Живой смок на throwaway-инстансе**

Run: `bash scripts/run_e2e_smokes.command 2>&1 | tail -15`
Expected: оба смока PASS (скрипт сам поднимает временный backend на temp data-dir и гасит его; прод не трогается).

- [ ] **Step 3: Полный Python-гейт**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_wake_word_heartbeat.py \
  KrabEar/tests/test_audio_reinit_coordinator.py \
  KrabEar/tests/test_audio_selfheal.py \
  KrabEar/tests/test_audio_selfheal_wiring.py \
  KrabEar/tests/test_wake_word_watchdog.py \
  KrabEar/tests/test_wake_word_watchdog_settings.py \
  KrabEar/tests/test_openwakeword_adapter.py \
  KrabEar/tests/test_openwakeword_security_W1210.py \
  KrabEar/tests/test_wake_word_polling_contract.py \
  KrabEar/tests/test_error_codes.py \
  -v -p no:cacheprovider
make lint
make audit-all
```
Expected: всё зелёное; audit-all подтверждает, что `audio_reinit.py`/`wake_word_watchdog.py` имеют production-импортёров.

- [ ] **Step 4: ubuntu-parity на изменённых/новых тест-файлах**

```bash
bash scripts/pre_merge_py312_check.sh \
  KrabEar/tests/test_wake_word_heartbeat.py \
  KrabEar/tests/test_audio_reinit_coordinator.py \
  KrabEar/tests/test_audio_selfheal.py \
  KrabEar/tests/test_audio_selfheal_wiring.py \
  KrabEar/tests/test_wake_word_watchdog.py \
  KrabEar/tests/test_wake_word_watchdog_settings.py
```
Expected: PASS (py3.12, mlx purged).

- [ ] **Step 5: Commit**

```bash
git add scripts/e2e_ipc_smoke.py
git commit -m "test(e2e): wake_word_status wedged/heartbeat поля + diagnostics.wake_word_watchdog в смоке

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## После плана (оркестратор, вне задач воркеров)

1. **Ручная проверка эскалационной цепочки** (спека §7, «шаг плана, не прод-код»):
   Python-половина — на THROWAWAY dev-инстансе в worktree: временно закомментировать
   строку штампа `self._last_chunk_ts = time.monotonic()` в `_listen_loop`
   (правка НЕ коммитится), поднять dev-backend (`python KrabEar/main.py
   --data-dir <tmp>`), socket-запросами `set_settings {wake_word_stale_sec: 10}`
   → `wake_word_start` → через ~20с `wake_word_status`: ожидать `wedged: true`
   (при THREAD_HUNG-имитации — или faster-путь: реальный heal сработает и
   wedged придёт после второго stale-окна). Откатить правку.
   Swift-половина — юниты трекера/kickstartArguments (Task 7) + примитив
   `launchctl kickstart -k gui/$(id -u)/ai.krab.ear.backend` уже доказан живым
   инцидентом 2026-07-13; полный Swift-конец цепочки на dev не гоняется
   (второй агент запрещён single-instance guard'ом).
2. PR в `codex/krab-ear-v2`, CI-гейт по ТОЧНОМУ headSha (без пушей в ветку,
   пока гейт взведён; `cancelled` = fail-closed).
3. Финальный adversarial-гейт всего диффа (Fable, reasoning max — дифф трогает
   захват аудио + право системы на само-рестарт процесса = security-sensitive
   класс по глобальному кодексу).
4. Мерж, deploy-ритуал (`build_and_deploy.command` + `launchctl kickstart -k`
   backend-сервиса + рестарт агента + верификация нового PID/UUID), parity-коммит
   бинарей, ROADMAP/CLAUDE.md запись.
