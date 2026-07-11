# C2a — Live Meeting Backend Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Backend-ядро живой панели встречи: MeetingSessionService с единым GPU-слотом, растущий транскрипт по курсору, live action items, события `meeting.*`, 3 IPC-метода.

**Architecture:** Пассивный сервис (18-я экстракция по паттерну проекта) с одним воркер-тредом, сериализующим тяжёлые операции (CHUNK_STT / ITEMS_LLM; слот сразу знает тип DIAR_WINDOW для C2b). Транскрипт копится непересекающимися чанками по курсору (`AudioRecorder.snapshot_range`), items замещаются целиком каждым LLM-вызовом, на время LLM партиалы ставятся на паузу (Metal-констрейнт). Спека: `docs/superpowers/specs/2026-07-10-c2-live-meeting-overlay-design.md`.

**Tech Stack:** Python 3.14 (`.venv_krab_ear`), threading, numpy, существующие `ActionItemsExtractor`/`Transcriber.transcribe_preview`/`brain_lease`/`EventBus` (модульный синглтон `bus`), unittest.

---

## Контекст для исполнителя (прочитай перед Task 1)

- Репозиторий: `/Users/pablito/Antigravity_AGENTS/Krab Ear`, ветка от `codex/krab-ear-v2`.
- Тесты: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/<file> -q -p no:cacheprovider`.
- flake8 (CI-команда): `.venv_krab_ear/bin/python -m flake8 KrabEar/backend/<files> --max-line-length=150`.
- 🔴 Каждый тест, создающий `BackendService(...)`, ОБЯЗАН звать `self.service.close()` в tearDown (иначе daemon-треды валят весь chunk-файл в CI).
- 🔴 ubuntu-CI не имеет mlx/torch: новые тест-файлы гонять через `bash scripts/pre_merge_py312_check.sh <files>`.
- EventBus: `from backend.event_bus import bus as event_bus`; `emit(str, dict)` НЕ требует регистрации типа (прецеденты: `realtime.partial_transcript`, `krab_error`).
- `handle_stop_recording` возвращает поле **`history_id`** (не item_id).
- `ActionItemsExtractor.extract(transcript, language="ru") -> ActionItemsResult` НИКОГДА не raises; поля item'а: `text/assignee/due/priority`; result: `.ok/.action_items/.decisions/.questions/.fallback_reason/.latency_ms`.
- `acquire_brain_lease(owner, ttl_sec)` / `release_brain_lease(owner)` NEVER raise; повторный acquire тем же owner = продление TTL.

### Карта файлов

| Файл | Роль |
|---|---|
| `KrabEar/backend/recorder.py` (modify) | + `snapshot_range(from_sec, to_sec)` |
| `KrabEar/backend/realtime_partial.py` (modify) | + `pause()/resume()` + проверка в `_worker` |
| `KrabEar/backend/recording_core_service.py` (modify) | + `pause_realtime_partials()/resume_realtime_partials()` (доступ под `_rt_lock`) |
| `KrabEar/backend/meeting_session_service.py` (create) | сервис: сессия, аккумулятор, GPU-слот, 3 handle_* |
| `KrabEar/backend/service.py` (modify) | конструирование + 3 записи dispatch + close() |
| `KrabEar/core/config.py` (modify) | 3 настройки в DEFAULT_SETTINGS |
| `KrabEar/backend/settings_validator.py` (modify) | 3 записи _RANGE_FIELDS |
| `KrabEar/tests/test_meeting_recorder_range_W_C2a.py` (create) | юниты snapshot_range |
| `KrabEar/tests/test_meeting_partial_pause_W_C2a.py` (create) | юниты pause/resume |
| `KrabEar/tests/test_meeting_session_service_W_C2a.py` (create) | юниты сервиса/слота/items |
| `KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py` (create) | dispatch-invariant + privacy + BackendService-интеграция |
| `scripts/e2e_meeting_smoke.py` (create) | живой e2e против throwaway-backend |
| `docs/IPC_API_REFERENCE.md` (modify) | 3 новых метода |

---

### Task 1: `AudioRecorder.snapshot_range`

**Files:**
- Modify: `KrabEar/backend/recorder.py` (рядом с `snapshot_audio`, ~строка 213)
- Test: `KrabEar/tests/test_meeting_recorder_range_W_C2a.py` (create)

- [ ] **Step 1: Написать падающий тест**

```python
"""snapshot_range: срез сырого буфера по диапазону секунд (C2a, спека §2.3)."""
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recorder import AudioRecorder  # noqa: E402


def _make_recorder_with_chunks(n_chunks: int, chunk_val_start: float = 0.0) -> AudioRecorder:
    """Рекордер с n_chunks чанками по 0.1с (1600 сэмплов при 16кГц).

    Значение сэмплов i-го чанка = chunk_val_start + i — по значению видно,
    какие чанки попали в срез.
    """
    rec = AudioRecorder(sample_rate=16000)
    with rec._lock:
        for i in range(n_chunks):
            rec._chunks.append(
                np.full(rec.chunk_size, chunk_val_start + i, dtype=np.float32)
            )
            rec._chunks_total_samples += rec.chunk_size
    return rec


class SnapshotRangeTestCase(unittest.TestCase):
    def test_middle_range_returns_exact_samples(self) -> None:
        rec = _make_recorder_with_chunks(30)  # 3.0 сек
        audio = rec.snapshot_range(1.0, 2.0)
        self.assertEqual(audio.size, 16000)  # ровно 1 секунда
        # Первый сэмпл диапазона — из чанка №10 (1.0с / 0.1с)
        self.assertAlmostEqual(float(audio[0]), 10.0, places=5)
        # Последний — из чанка №19
        self.assertAlmostEqual(float(audio[-1]), 19.0, places=5)

    def test_range_beyond_buffer_clamps(self) -> None:
        rec = _make_recorder_with_chunks(10)  # 1.0 сек
        audio = rec.snapshot_range(0.5, 99.0)
        self.assertEqual(audio.size, 8000)  # только доступная половина

    def test_empty_and_degenerate_ranges(self) -> None:
        rec = _make_recorder_with_chunks(10)
        self.assertEqual(rec.snapshot_range(2.0, 1.0).size, 0)   # from > to
        self.assertEqual(rec.snapshot_range(1.0, 1.0).size, 0)   # from == to
        self.assertEqual(rec.snapshot_range(5.0, 6.0).size, 0)   # за концом буфера
        empty = AudioRecorder(sample_rate=16000)
        self.assertEqual(empty.snapshot_range(0.0, 1.0).size, 0)  # пустой буфер

    def test_dtype_and_flat_shape(self) -> None:
        rec = _make_recorder_with_chunks(5)
        audio = rec.snapshot_range(0.0, 0.5)
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(audio.ndim, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_recorder_range_W_C2a.py -q -p no:cacheprovider`
Expected: FAIL / ERROR `AttributeError: 'AudioRecorder' object has no attribute 'snapshot_range'`

- [ ] **Step 3: Реализация в `recorder.py`** (после `snapshot_audio`)

```python
    def snapshot_range(self, from_sec: float, to_sec: float) -> np.ndarray:
        """Срез сырого буфера по диапазону секунд ОТ НАЧАЛА записи.

        Для meeting-аккумулятора (C2a): непересекающиеся чанки по курсору —
        полный транскрипт без дедупа. O(число чанков) на скан + O(диапазона)
        на копирование; полной конкатенации буфера нет (урок snapshot_audio).
        Диапазон за пределами буфера обрезается; вырожденный → пустой массив.
        """
        if to_sec <= from_sec:
            return np.array([], dtype=np.float32)
        from_sample = max(0, int(from_sec * self.sample_rate))
        to_sample = int(to_sec * self.sample_rate)

        with self._lock:
            chunks = list(self._chunks)

        collected: list[np.ndarray] = []
        offset = 0
        for chunk in chunks:
            flat = chunk.reshape(-1)
            chunk_end = offset + flat.size
            if chunk_end <= from_sample:
                offset = chunk_end
                continue
            if offset >= to_sample:
                break
            start = max(0, from_sample - offset)
            end = min(flat.size, to_sample - offset)
            collected.append(flat[start:end])
            offset = chunk_end

        if not collected:
            return np.array([], dtype=np.float32)
        return np.concatenate(collected, axis=0).astype(np.float32)
```

- [ ] **Step 4: Тест зелёный**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_recorder_range_W_C2a.py -q -p no:cacheprovider`
Expected: 4 passed

- [ ] **Step 5: Смежные тесты рекордера + flake8**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/ -q -p no:cacheprovider -k "recorder" && .venv_krab_ear/bin/python -m flake8 KrabEar/backend/recorder.py KrabEar/tests/test_meeting_recorder_range_W_C2a.py --max-line-length=150`
Expected: все passed, flake8 пусто

- [ ] **Step 6: Commit**

```bash
git add KrabEar/backend/recorder.py KrabEar/tests/test_meeting_recorder_range_W_C2a.py
git commit -m "feat(recorder): snapshot_range — срез буфера по диапазону секунд (C2a)"
```

---

### Task 2: `RealtimePartialTranscriber.pause()/resume()`

**Files:**
- Modify: `KrabEar/backend/realtime_partial.py`
- Test: `KrabEar/tests/test_meeting_partial_pause_W_C2a.py` (create)

- [ ] **Step 1: Падающий тест**

```python
"""pause()/resume() у RealtimePartialTranscriber (C2a, спека §2.2).

Во время паузы воркер НЕ снимает снапшоты и НЕ эмиттит события —
Metal-констрейнт: на время LLM/диар-вызова партиалы молчат.
"""
import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.realtime_partial import RealtimePartialTranscriber  # noqa: E402


class _SpyRecorder:
    def __init__(self) -> None:
        self.snapshot_calls = 0

    def snapshot_audio(self, max_duration_sec: float = 8.0):
        self.snapshot_calls += 1
        return np.ones(16000, dtype=np.float32), 1.0


class _SpyTranscriber:
    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return {"text": "чанк"}


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.lock = threading.Lock()

    def emit(self, event_type: str, payload: dict) -> None:
        with self.lock:
            self.events.append((event_type, payload))


class PartialPauseTestCase(unittest.TestCase):
    def _make(self) -> tuple[RealtimePartialTranscriber, _SpyRecorder, _SpyBus]:
        rec, bus = _SpyRecorder(), _SpyBus()
        rt = RealtimePartialTranscriber(
            transcriber=_SpyTranscriber(), recorder=rec, event_bus=bus,
            interval_sec=0.05, buffer_sec=1.0, privacy_getter=lambda: False,
        )
        return rt, rec, bus

    def test_pause_stops_snapshots_resume_restarts(self) -> None:
        rt, rec, _ = self._make()
        rt.start(session_id="s1", sample_rate=16000)
        try:
            time.sleep(0.3)
            self.assertGreater(rec.snapshot_calls, 0, "до паузы воркер должен работать")

            rt.pause()
            time.sleep(0.15)  # дать текущей итерации дожить
            calls_at_pause = rec.snapshot_calls
            time.sleep(0.3)
            self.assertEqual(
                rec.snapshot_calls, calls_at_pause,
                "во время паузы snapshot_audio не должен вызываться",
            )

            rt.resume()
            time.sleep(0.3)
            self.assertGreater(rec.snapshot_calls, calls_at_pause,
                               "после resume воркер должен продолжить")
        finally:
            rt.stop(timeout_sec=5.0)

    def test_pause_is_idempotent_and_stop_works_while_paused(self) -> None:
        rt, _, _ = self._make()
        rt.start(session_id="s2", sample_rate=16000)
        rt.pause()
        rt.pause()  # повторный вызов — no-op
        rt.stop(timeout_sec=5.0)  # stop из паузы не должен зависнуть
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_partial_pause_W_C2a.py -q -p no:cacheprovider`
Expected: ERROR `AttributeError: ... has no attribute 'pause'`

- [ ] **Step 3: Реализация**

В `__init__` (рядом с `self._stop_event = threading.Event()`, ~строка 73):

```python
        self._pause_event = threading.Event()  # C2a: пауза на время LLM/диар (Metal)
```

Методы после `stop()` (~строка 153):

```python
    def pause(self) -> None:
        """Приостановить снапшоты/эмиты без остановки треда (C2a, Metal-констрейнт).

        Idempotent. Текущая итерация (если уже в STT) дорабатывает — вызывающий
        GPU-слот сериализован, короткое перекрытие исключено его очередью.
        """
        self._pause_event.set()
        logger.debug("RealtimePartialTranscriber: pause (session=%s)", self._session_id)

    def resume(self) -> None:
        """Снять паузу. Idempotent."""
        self._pause_event.clear()
        logger.debug("RealtimePartialTranscriber: resume (session=%s)", self._session_id)
```

В `_worker` сразу после `self._stop_event.wait(self._interval_sec)` / проверки `is_set()` (~строка 170):

```python
            if self._pause_event.is_set():
                continue  # пауза: пропускаем итерацию, тред жив
```

- [ ] **Step 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_partial_pause_W_C2a.py -q -p no:cacheprovider`
Expected: 2 passed

- [ ] **Step 5: Существующие тесты партиалов не сломаны + flake8**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/ -q -p no:cacheprovider -k "realtime_partial or partial" && .venv_krab_ear/bin/python -m flake8 KrabEar/backend/realtime_partial.py KrabEar/tests/test_meeting_partial_pause_W_C2a.py --max-line-length=150`
Expected: passed, flake8 пусто

- [ ] **Step 6: Commit**

```bash
git add KrabEar/backend/realtime_partial.py KrabEar/tests/test_meeting_partial_pause_W_C2a.py
git commit -m "feat(realtime): pause/resume у партиалов — Metal-констрейнт meeting-слота (C2a)"
```

---

### Task 3: Аксессоры паузы в RecordingCoreService

**Files:**
- Modify: `KrabEar/backend/recording_core_service.py` (после `handle_stop_recording`, ~строка 419)
- Test: дополнение в `KrabEar/tests/test_meeting_partial_pause_W_C2a.py`

- [ ] **Step 1: Падающий тест** (дописать класс в конец test-файла Task 2, перед `if __name__`)

```python
class RecordingCorePauseAccessorsTestCase(unittest.TestCase):
    """Аксессоры RecordingCoreService: доступ к _rt_partial строго под _rt_lock."""

    def test_pause_resume_accessors_delegate(self) -> None:
        from backend.recording_core_service import RecordingCoreService

        svc = RecordingCoreService.__new__(RecordingCoreService)  # без полного __init__
        svc._rt_lock = threading.Lock()

        class _FakeRT:
            def __init__(self) -> None:
                self.paused = 0
                self.resumed = 0

            def pause(self) -> None:
                self.paused += 1

            def resume(self) -> None:
                self.resumed += 1

        fake = _FakeRT()
        svc._rt_partial = fake
        svc.pause_realtime_partials()
        svc.resume_realtime_partials()
        self.assertEqual((fake.paused, fake.resumed), (1, 1))

    def test_accessors_are_noop_without_instance(self) -> None:
        from backend.recording_core_service import RecordingCoreService

        svc = RecordingCoreService.__new__(RecordingCoreService)
        svc._rt_lock = threading.Lock()
        svc._rt_partial = None
        svc.pause_realtime_partials()  # не должно бросить
        svc.resume_realtime_partials()
        self.assertTrue(True)
```

- [ ] **Step 2: Убедиться, что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_partial_pause_W_C2a.py -q -p no:cacheprovider`
Expected: 2 passed, 2 failed (`AttributeError: ... pause_realtime_partials`)

- [ ] **Step 3: Реализация**

```python
    def pause_realtime_partials(self) -> None:
        """Пауза партиалов на время тяжёлой операции meeting-слота (C2a).

        Доступ к _rt_partial — под _rt_lock (конвенция lifecycle-лока);
        сам pause() зовётся вне лока (короткий, но чужой код).
        Нет активного инстанса → no-op.
        """
        with self._rt_lock:
            rt = self._rt_partial
        if rt is not None:
            try:
                rt.pause()
            except Exception:
                logger.warning("pause_realtime_partials: pause() упал", exc_info=True)

    def resume_realtime_partials(self) -> None:
        """Снять паузу партиалов (C2a). Нет инстанса → no-op."""
        with self._rt_lock:
            rt = self._rt_partial
        if rt is not None:
            try:
                rt.resume()
            except Exception:
                logger.warning("resume_realtime_partials: resume() упал", exc_info=True)
```

- [ ] **Step 4: Тесты зелёные + flake8**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_partial_pause_W_C2a.py -q -p no:cacheprovider && .venv_krab_ear/bin/python -m flake8 KrabEar/backend/recording_core_service.py --max-line-length=150`
Expected: 4 passed, flake8 пусто

- [ ] **Step 5: Commit**

```bash
git add KrabEar/backend/recording_core_service.py KrabEar/tests/test_meeting_partial_pause_W_C2a.py
git commit -m "feat(recording): pause/resume-аксессоры партиалов под _rt_lock (C2a)"
```

---

### Task 4: Настройки meeting_* (config + validator)

**Files:**
- Modify: `KrabEar/core/config.py` (в `DEFAULT_SETTINGS`, после блока `rt_partial_*`, ~строка 964)
- Modify: `KrabEar/backend/settings_validator.py` (в `_RANGE_FIELDS`, ~строка 62)
- Test: дополнение в `KrabEar/tests/test_meeting_session_service_W_C2a.py` придёт в Task 5 (валидатор уже покрыт существующим generic-тестом `_RANGE_FIELDS`); здесь только ручная проверка импорта.

- [ ] **Step 1: DEFAULT_SETTINGS**

```python
    # --- Live meeting overlay (C2a, спека 2026-07-10) ---
    "meeting_chunk_stt_interval_sec": 25.0,
    "meeting_items_interval_sec": 60.0,
    "meeting_items_language": "ru",
```

- [ ] **Step 2: _RANGE_FIELDS** (формат `(min, max, default, type)`)

```python
    "meeting_chunk_stt_interval_sec": (10.0, 120.0, 25.0, float),
    "meeting_items_interval_sec": (30.0, 600.0, 60.0, float),
```

- [ ] **Step 3: Проверка**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -c "from core.config import DEFAULT_SETTINGS as D; from backend.settings_validator import _RANGE_FIELDS as R; print(D['meeting_chunk_stt_interval_sec'], R['meeting_items_interval_sec'])" && PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/ -q -p no:cacheprovider -k "settings_validator" `
Expected: `25.0 (30.0, 600.0, 60.0, <class 'float'>)`, тесты валидатора passed

- [ ] **Step 4: Commit**

```bash
git add KrabEar/core/config.py KrabEar/backend/settings_validator.py
git commit -m "feat(settings): meeting_* интервалы с границами _RANGE_FIELDS (C2a)"
```

---

### Task 5: MeetingSessionService — ядро (аккумулятор + GPU-слот + CHUNK_STT + события)

**Files:**
- Create: `KrabEar/backend/meeting_session_service.py`
- Test: `KrabEar/tests/test_meeting_session_service_W_C2a.py` (create)

Дизайн-инвариант тестируемости: логика тактов — в чистом методе `_run_due_job_once(now)` (выбрать одну созревшую задачу по приоритету → исполнить → перепланировать от завершения). Тред — тонкая обёртка `while: wait(0.5); _run_due_job_once(monotonic())`. Юниты зовут `_run_due_job_once` напрямую, без тредов и sleep'ов.

- [ ] **Step 1: Падающие тесты (ядро)**

```python
"""MeetingSessionService: аккумулятор, GPU-слот, CHUNK_STT, события (C2a).

Все тесты — без тредов: _run_due_job_once(now) зовётся напрямую.
"""
import sys
import threading
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.meeting_session_service import (  # noqa: E402
    MeetingJob,
    MeetingSessionService,
)


class _FakeRecorder:
    def __init__(self, duration: float = 100.0) -> None:
        self.sample_rate = 16000
        self.is_recording = True
        self._duration = duration

    def get_duration_sec(self) -> float:
        return self._duration

    def snapshot_range(self, from_sec: float, to_sec: float) -> np.ndarray:
        n = max(0, int((to_sec - from_sec) * self.sample_rate))
        return np.ones(n, dtype=np.float32)


class _FakeTranscriber:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        self.calls.append(float(audio_data.size))
        return {"text": f"чанк{len(self.calls)}"}


class _FakeExtractorResult:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.action_items = []
        self.decisions = ["решение"]
        self.questions = []
        self.fallback_reason = None if ok else "llm_error"
        self.latency_ms = 5


class _FakeExtractor:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.texts: list[str] = []

    def extract(self, transcript: str, language: str = "ru"):
        self.texts.append(transcript)
        return _FakeExtractorResult(ok=self.ok)


class _FakeRecordingCore:
    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0
        self.started: list[dict] = []
        self.stopped: list[dict] = []

    def handle_start_recording(self, params):
        self.started.append(params)
        return {"status": "started", "is_recording": True}

    def handle_stop_recording(self, params):
        self.stopped.append(params)
        return {"history_id": "hist-1", "text": "финал"}

    def pause_realtime_partials(self) -> None:
        self.paused += 1

    def resume_realtime_partials(self) -> None:
        self.resumed += 1


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def emit(self, event_type: str, payload: dict) -> None:
        with self._lock:
            self.events.append((event_type, payload))

    def types(self) -> list[str]:
        with self._lock:
            return [t for t, _ in self.events]


def _make_svc(privacy: bool = False, extractor=None, recorder=None,
              settings_extra: dict | None = None):
    settings = {
        "privacy_mode_enabled": privacy,
        "meeting_chunk_stt_interval_sec": 25.0,
        "meeting_items_interval_sec": 60.0,
        "meeting_items_language": "ru",
        "llm_brain_lease_enabled": False,  # юниты: lease off (отдельный тест ниже)
    }
    settings.update(settings_extra or {})
    bus = _SpyBus()
    rec = recorder or _FakeRecorder()
    svc = MeetingSessionService(
        recorder=rec,
        transcriber=_FakeTranscriber(),
        recording_core=_FakeRecordingCore(),
        action_items_extractor=extractor,
        settings_get=lambda k, d=None: settings.get(k, d),
        event_bus=bus,
    )
    return svc, bus, rec


class MeetingStartStateTestCase(unittest.TestCase):
    def test_start_when_idle_starts_recording_and_session(self) -> None:
        svc, _, _ = _make_svc()
        svc._recording_core.__class__  # noqa: B018 -- доступность атрибута
        resp = svc.handle_meeting_start({})
        self.assertTrue(resp["ok"]) 
        self.assertFalse(resp["promoted"])
        state = svc.handle_get_meeting_live_state({})
        self.assertTrue(state["active"])
        svc.close()

    def test_start_when_recording_promotes_with_cursor(self) -> None:
        rec = _FakeRecorder(duration=42.0)
        svc, _, _ = _make_svc(recorder=rec)
        svc._recording_core.handle_start_recording = lambda p: {
            "status": "already_recording", "is_recording": True,
        }
        resp = svc.handle_meeting_start({})
        self.assertTrue(resp["promoted"])
        # курсор аккумулятора = текущая длительность (начало доберёт финальный отчёт)
        self.assertAlmostEqual(svc._session.cursor_sec, 42.0, places=3)
        svc.close()

    def test_start_is_idempotent(self) -> None:
        svc, _, _ = _make_svc()
        svc.handle_meeting_start({})
        resp2 = svc.handle_meeting_start({})
        self.assertTrue(resp2["ok"]) 
        self.assertTrue(resp2.get("already_active"))
        svc.close()

    def test_privacy_refuses_start(self) -> None:
        svc, _, _ = _make_svc(privacy=True)
        resp = svc.handle_meeting_start({})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp.get("skipped"), "privacy_mode")
        svc.close()


class ChunkSttJobTestCase(unittest.TestCase):
    def test_chunk_stt_appends_and_emits(self) -> None:
        svc, bus, _ = _make_svc()
        svc.handle_meeting_start({})
        ran = svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 0.1)
        self.assertEqual(ran, MeetingJob.CHUNK_STT)
        self.assertIn("meeting.transcript_appended", bus.types())
        state = svc.handle_get_meeting_live_state({})
        self.assertIn("чанк1", state["transcript_tail"])
        self.assertGreater(state["transcript_len"], 0)
        svc.close()

    def test_cursor_advances_no_overlap(self) -> None:
        rec = _FakeRecorder(duration=100.0)
        svc, _, _ = _make_svc(recorder=rec)
        svc.handle_meeting_start({})
        t1 = svc._next_due[MeetingJob.CHUNK_STT] + 0.1
        svc._run_due_job_once(now=t1)
        cursor_after_first = svc._session.cursor_sec
        self.assertAlmostEqual(cursor_after_first, 100.0, places=3)
        # второй тик: длительность не выросла -> пустой диапазон -> STT не зовётся
        calls_before = len(svc._transcriber.calls)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 0.1)
        self.assertEqual(len(svc._transcriber.calls), calls_before)
        svc.close()

    def test_no_job_before_due(self) -> None:
        svc, _, _ = _make_svc()
        svc.handle_meeting_start({})
        ran = svc._run_due_job_once(now=0.0)
        self.assertIsNone(ran)
        svc.close()

    def test_out_of_band_stop_finalizes(self) -> None:
        rec = _FakeRecorder()
        svc, bus, _ = _make_svc(recorder=rec)
        svc.handle_meeting_start({})
        rec.is_recording = False  # запись остановили в обход
        svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 0.1)
        self.assertIn("meeting.finished", bus.types())
        state = svc.handle_get_meeting_live_state({})
        self.assertFalse(state["active"])
        svc.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_session_service_W_C2a.py -q -p no:cacheprovider`
Expected: ERROR `ModuleNotFoundError: No module named 'backend.meeting_session_service'`

- [ ] **Step 3: Реализация `meeting_session_service.py`**

```python
"""MeetingSessionService — backend-ядро живой панели встречи (C2a).

Спека: docs/superpowers/specs/2026-07-10-c2-live-meeting-overlay-design.md §2.

Пассивен вне встречи. Внутри — один воркер-тред («GPU-слот»): на Metal не
больше одной тяжёлой операции meeting-механики одновременно. Типы задач —
enum MeetingJob; DIAR_WINDOW объявлен сразу (C2b добавит только исполнитель).
Приоритет при одновременной готовности: CHUNK_STT > ITEMS_LLM > DIAR_WINDOW.

Privacy: все хендлеры гейтятся privacy_mode_enabled; включение privacy
посреди встречи глушит live-обработку (воркер выходит, события прекращаются).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("krab_ear.backend")

_TAIL_CHARS = 600          # transcript_tail в get_meeting_live_state
_ITEMS_MIN_GROWTH = 200    # симв.: минимальный прирост текста для нового LLM-вызова
_LEASE_RENEW_SEC = 15.0    # период продления brain-lease
_LEASE_TTL_SEC = 45.0      # TTL lease (перекрывает период продления с запасом)
_WORKER_WAIT_SEC = 0.5     # шаг ожидания воркера


class MeetingJob(str, Enum):
    CHUNK_STT = "chunk_stt"
    ITEMS_LLM = "items_llm"
    DIAR_WINDOW = "diar_window"  # C2b: объявлен сейчас, исполнителя нет


@dataclass
class _MeetingSession:
    started_at: float = field(default_factory=time.time)
    promoted: bool = False
    language: str = "ru"
    cursor_sec: float = 0.0
    chunks: list[str] = field(default_factory=list)
    transcript_len: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    last_extract_len: int = 0
    degraded_llm: bool = False
    degraded_diarization: bool = False
    privacy_stopped: bool = False
    last_updated_ts: float = field(default_factory=time.time)

    def tail(self) -> str:
        return "".join(self.chunks)[-_TAIL_CHARS:]


class MeetingSessionService:
    """18-я сервис-экстракция: живая meeting-сессия поверх активной записи."""

    def __init__(
        self,
        recorder: Any,
        transcriber: Any,
        recording_core: Any,
        action_items_extractor: Any,
        settings_get: Callable[[str, Any], Any],
        event_bus: Any,
    ) -> None:
        self._recorder = recorder
        self._transcriber = transcriber
        self._recording_core = recording_core
        self._extractor = action_items_extractor
        self._settings_get = settings_get
        self._bus = event_bus

        self._lock = threading.Lock()          # состояние сессии
        self._session: _MeetingSession | None = None
        self._next_due: dict[MeetingJob, float] = {}
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------ IPC

    def handle_meeting_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC meeting_start: старт записи+сессии ИЛИ повышение идущей записи."""
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "skipped": "privacy_mode"}

        with self._lock:
            if self._session is not None and not self._session.privacy_stopped:
                return {"ok": True, "already_active": True,
                        "started_at": self._session.started_at,
                        "promoted": self._session.promoted}

        start_resp = self._recording_core.handle_start_recording({})
        promoted = start_resp.get("status") == "already_recording"

        session = _MeetingSession(
            promoted=promoted,
            language=str(params.get("language", self._settings_get(
                "meeting_items_language", "ru")) or "ru"),
            cursor_sec=float(self._recorder.get_duration_sec()) if promoted else 0.0,
        )
        now = time.monotonic()
        with self._lock:
            self._session = session
            self._next_due = {
                MeetingJob.CHUNK_STT: now + self._chunk_interval(),
                MeetingJob.ITEMS_LLM: now + self._items_interval(),
            }
        self._acquire_lease()
        self._start_worker()
        logger.info("meeting: сессия запущена", extra={
            "promoted": promoted, "language": session.language})
        return {"ok": True, "promoted": promoted, "started_at": session.started_at}

    def handle_meeting_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC meeting_stop: гасит live-сессию и останавливает запись обычным путём."""
        if self._settings_get("privacy_mode_enabled", False):
            # privacy включили посреди встречи: сессию всё равно закрываем,
            # но запись останавливает обычный privacy-путь записи.
            self._teardown_session(emit_finished=False)
            return {"ok": True, "skipped": "privacy_mode"}

        with self._lock:
            had_session = self._session is not None
        if not had_session:
            return {"ok": True, "active": False}

        self._stop_worker()
        self._emit("meeting.finalizing", {})
        stop_resp: dict[str, Any] = {}
        if getattr(self._recorder, "is_recording", False):
            stop_resp = self._recording_core.handle_stop_recording({})
        item_id = stop_resp.get("history_id")
        self._teardown_session(emit_finished=True, item_id=item_id)
        return {"ok": True, "item_id": item_id}

    def handle_get_meeting_live_state(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC get_meeting_live_state: снимок для панели/поллинга."""
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": True, "active": False, "privacy_mode_active": True}
        with self._lock:
            s = self._session
            if s is None or s.privacy_stopped:
                return {"ok": True, "active": False}
            return {
                "ok": True,
                "active": True,
                "started_at": s.started_at,
                "promoted": s.promoted,
                "transcript_len": s.transcript_len,
                "transcript_tail": s.tail(),
                "items": list(s.items),
                "decisions": list(s.decisions),
                "questions": list(s.questions),
                "speakers": [],  # C2b
                "degraded": {"llm": s.degraded_llm or self._extractor is None,
                             "diarization": s.degraded_diarization},
                "last_updated_ts": s.last_updated_ts,
            }

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Останов воркера (BackendService.close())."""
        self._stop_worker()
        with self._lock:
            self._session = None

    def _start_worker(self) -> None:
        self._stop_event.clear()
        t = threading.Thread(
            target=self._worker_loop, name="meeting-gpu-slot", daemon=True)
        self._worker = t
        t.start()

    def _stop_worker(self) -> None:
        self._stop_event.set()
        t = self._worker
        if t is not None and t.is_alive():
            t.join(timeout=30.0)
            if t.is_alive():
                logger.warning("meeting: воркер не завершился за 30с")
        self._worker = None

    def _teardown_session(self, emit_finished: bool,
                          item_id: Any = None) -> None:
        self._stop_worker()
        self._release_lease()
        if emit_finished:
            self._emit("meeting.finished", {"item_id": item_id})
        with self._lock:
            self._session = None
            self._next_due = {}

    # ---------------------------------------------------------------- worker

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(_WORKER_WAIT_SEC)
            if self._stop_event.is_set():
                break
            try:
                self._run_due_job_once(time.monotonic())
            except Exception:
                logger.exception("meeting: тик воркера упал")
            with self._lock:
                if self._session is None or self._session.privacy_stopped:
                    break

    def _run_due_job_once(self, now: float) -> MeetingJob | None:
        """Одна итерация слота: приоритетная созревшая задача. Тестируется напрямую."""
        with self._lock:
            s = self._session
        if s is None or s.privacy_stopped:
            return None

        # privacy посреди встречи: глушим live-обработку (спека §3)
        if self._settings_get("privacy_mode_enabled", False):
            with self._lock:
                s.privacy_stopped = True
            self._release_lease()
            logger.info("meeting: privacy включён посреди встречи — live-обработка остановлена")
            return None

        # запись остановили в обход meeting_stop -> финализируемся сами
        if not getattr(self._recorder, "is_recording", False):
            self._release_lease()
            self._emit("meeting.finalizing", {})
            self._emit("meeting.finished", {"item_id": None})
            with self._lock:
                self._session = None
                self._next_due = {}
            return None

        self._renew_lease_if_due(now)

        for job in (MeetingJob.CHUNK_STT, MeetingJob.ITEMS_LLM, MeetingJob.DIAR_WINDOW):
            due = self._next_due.get(job)
            if due is None or now < due:
                continue
            try:
                if job is MeetingJob.CHUNK_STT:
                    self._job_chunk_stt(s)
                elif job is MeetingJob.ITEMS_LLM:
                    self._job_items_llm(s)
                else:  # DIAR_WINDOW: исполнитель придёт в C2b
                    pass
            finally:
                # skip-tick: перепланируем от завершения, без лавины
                self._next_due[job] = time.monotonic() + self._job_interval(job)
            return job
        return None

    # ------------------------------------------------------------------ jobs

    def _job_chunk_stt(self, s: _MeetingSession) -> None:
        upto = float(self._recorder.get_duration_sec())
        if upto <= s.cursor_sec + 0.25:  # диапазон вырожден — нечего снимать
            return
        audio = self._recorder.snapshot_range(s.cursor_sec, upto)
        if getattr(audio, "size", 0) == 0:
            return
        payload = self._transcriber.transcribe_preview(
            audio_data=audio, quality_profile="balanced")
        text = payload.get("text") if isinstance(payload, dict) else str(payload or "")
        text = (text or "").strip()
        with self._lock:
            s.cursor_sec = upto
            if text:
                s.chunks.append(text + " ")
                s.transcript_len += len(text) + 1
                s.last_updated_ts = time.time()
        if text:
            self._emit("meeting.transcript_appended",
                       {"chunk_text": text, "total_len": s.transcript_len})

    def _job_items_llm(self, s: _MeetingSession) -> None:
        if self._extractor is None:
            with self._lock:
                s.degraded_llm = True
            return
        with self._lock:
            full_text = "".join(s.chunks)
        if len(full_text) - s.last_extract_len < _ITEMS_MIN_GROWTH:
            return  # текст почти не вырос — экономим LLM
        self._recording_core.pause_realtime_partials()
        try:
            result = self._extractor.extract(full_text, language=s.language)
        finally:
            self._recording_core.resume_realtime_partials()
        with self._lock:
            s.degraded_llm = not result.ok
            if result.ok:
                s.items = [ai.to_dict() if hasattr(ai, "to_dict") else dict(ai)
                           for ai in result.action_items]
                s.decisions = list(result.decisions)
                s.questions = list(result.questions)
                s.last_extract_len = len(full_text)
                s.last_updated_ts = time.time()
        if result.ok:
            self._emit("meeting.items_updated", {
                "items": list(s.items), "decisions": list(s.decisions),
                "questions": list(s.questions)})

    # ------------------------------------------------------------ intervals

    def _chunk_interval(self) -> float:
        return float(self._settings_get("meeting_chunk_stt_interval_sec", 25.0))

    def _items_interval(self) -> float:
        base = float(self._settings_get("meeting_items_interval_sec", 60.0))
        with self._lock:
            total = self._session.transcript_len if self._session else 0
        # адаптив (спека §2.2): на длинной встрече вызовы реже
        return max(base, total / 120.0)

    def _job_interval(self, job: MeetingJob) -> float:
        if job is MeetingJob.CHUNK_STT:
            return self._chunk_interval()
        if job is MeetingJob.ITEMS_LLM:
            return self._items_interval()
        return 120.0  # DIAR_WINDOW (C2b уточнит из настроек)

    # ---------------------------------------------------------------- lease

    def _lease_enabled(self) -> bool:
        return bool(self._settings_get("llm_brain_lease_enabled", True))

    def _acquire_lease(self) -> None:
        if not self._lease_enabled():
            return
        try:
            from backend.brain_lease import acquire_brain_lease
            acquire_brain_lease("krab_ear", ttl_sec=_LEASE_TTL_SEC)
            self._next_due[("lease",)] = time.monotonic() + _LEASE_RENEW_SEC  # type: ignore[index]
        except Exception as exc:
            logger.debug("meeting: brain-lease acquire error (ignored): %s", exc)

    def _renew_lease_if_due(self, now: float) -> None:
        if not self._lease_enabled():
            return
        due = self._next_due.get(("lease",))  # type: ignore[arg-type]
        if due is not None and now >= due:
            self._acquire_lease()

    def _release_lease(self) -> None:
        if not self._lease_enabled():
            return
        try:
            from backend.brain_lease import release_brain_lease
            release_brain_lease("krab_ear")
        except Exception as exc:
            logger.debug("meeting: brain-lease release error (ignored): %s", exc)

    # ---------------------------------------------------------------- events

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self._bus.emit(event_type, payload)
        except Exception:
            logger.warning("meeting: emit %s упал", event_type, exc_info=True)
```

Замечание для исполнителя: ключ lease в `_next_due` — кортеж `("lease",)`, чтобы не расширять enum служебным типом; mypy-игноры уже проставлены.

- [ ] **Step 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_session_service_W_C2a.py -q -p no:cacheprovider`
Expected: 8 passed

- [ ] **Step 5: flake8**

Run: `.venv_krab_ear/bin/python -m flake8 KrabEar/backend/meeting_session_service.py KrabEar/tests/test_meeting_session_service_W_C2a.py --max-line-length=150`
Expected: пусто

- [ ] **Step 6: Commit**

```bash
git add KrabEar/backend/meeting_session_service.py KrabEar/tests/test_meeting_session_service_W_C2a.py
git commit -m "feat(meeting): MeetingSessionService — GPU-слот, аккумулятор, CHUNK_STT, события (C2a)"
```

---

### Task 6: ITEMS_LLM + пауза партиалов + lease + meeting_stop (тесты поведения)

**Files:**
- Test: дополнение `KrabEar/tests/test_meeting_session_service_W_C2a.py` (реализация уже в Task 5 — этот таск ДОКАЗЫВАЕТ поведение тестами; найденные расхождения чинить в `meeting_session_service.py`)

- [ ] **Step 1: Дописать тесты** (в конец файла, перед `if __name__`)

```python
class ItemsLlmJobTestCase(unittest.TestCase):
    def _grow_transcript(self, svc, chars: int = 300) -> None:
        with svc._lock:
            svc._session.chunks.append("х" * chars)
            svc._session.transcript_len += chars

    def test_items_llm_pauses_partials_and_replaces_list(self) -> None:
        extractor = _FakeExtractor(ok=True)
        svc, bus, _ = _make_svc(extractor=extractor)
        svc.handle_meeting_start({})
        self._grow_transcript(svc)
        ran = svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        self.assertEqual(ran, MeetingJob.ITEMS_LLM)
        core = svc._recording_core
        self.assertEqual((core.paused, core.resumed), (1, 1),
                         "LLM-вызов обязан паузить и резюмить партиалы")
        self.assertIn("meeting.items_updated", bus.types())
        state = svc.handle_get_meeting_live_state({})
        self.assertEqual(state["decisions"], ["решение"])
        self.assertFalse(state["degraded"]["llm"])
        svc.close()

    def test_items_llm_resumes_partials_even_on_crash(self) -> None:
        class _BoomExtractor:
            def extract(self, transcript, language="ru"):
                raise RuntimeError("boom")

        svc, _, _ = _make_svc(extractor=_BoomExtractor())
        svc.handle_meeting_start({})
        self._grow_transcript(svc)
        with self.assertRaises(RuntimeError):
            svc._job_items_llm(svc._session)
        core = svc._recording_core
        self.assertEqual(core.resumed, core.paused, "resume обязан быть в finally")
        svc.close()

    def test_items_llm_skips_without_growth(self) -> None:
        extractor = _FakeExtractor(ok=True)
        svc, _, _ = _make_svc(extractor=extractor)
        svc.handle_meeting_start({})
        self._grow_transcript(svc, chars=300)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        # рост < 200 симв. -> extract не зовётся второй раз
        svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        self.assertEqual(len(extractor.texts), 1)
        svc.close()

    def test_no_extractor_sets_degraded(self) -> None:
        svc, _, _ = _make_svc(extractor=None)
        svc.handle_meeting_start({})
        self._grow_transcript(svc)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        state = svc.handle_get_meeting_live_state({})
        self.assertTrue(state["degraded"]["llm"])
        svc.close()

    def test_extract_failure_sets_degraded_keeps_old_items(self) -> None:
        extractor = _FakeExtractor(ok=True)
        svc, _, _ = _make_svc(extractor=extractor)
        svc.handle_meeting_start({})
        self._grow_transcript(svc)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        extractor.ok = False
        self._grow_transcript(svc)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        state = svc.handle_get_meeting_live_state({})
        self.assertTrue(state["degraded"]["llm"])
        self.assertEqual(state["decisions"], ["решение"], "старые items сохраняются")
        svc.close()


class MeetingStopTestCase(unittest.TestCase):
    def test_stop_delegates_and_returns_history_id(self) -> None:
        svc, bus, _ = _make_svc()
        svc.handle_meeting_start({})
        resp = svc.handle_meeting_stop({})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["item_id"], "hist-1")
        self.assertEqual(bus.types().count("meeting.finalizing"), 1)
        self.assertEqual(bus.types().count("meeting.finished"), 1)
        self.assertEqual(len(svc._recording_core.stopped), 1)
        state = svc.handle_get_meeting_live_state({})
        self.assertFalse(state["active"])

    def test_stop_without_session_is_noop(self) -> None:
        svc, _, _ = _make_svc()
        resp = svc.handle_meeting_stop({})
        self.assertTrue(resp["ok"])
        self.assertFalse(resp.get("active", False))

    def test_privacy_mid_meeting_stops_processing(self) -> None:
        settings_box = {"privacy": False}
        svc, bus, _ = _make_svc()
        svc._settings_get = lambda k, d=None: (
            settings_box["privacy"] if k == "privacy_mode_enabled"
            else {"meeting_chunk_stt_interval_sec": 25.0,
                  "meeting_items_interval_sec": 60.0,
                  "meeting_items_language": "ru",
                  "llm_brain_lease_enabled": False}.get(k, d))
        svc.handle_meeting_start({})
        settings_box["privacy"] = True
        ran = svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 0.1)
        self.assertIsNone(ran)
        events_after = len(bus.events)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 99.0)
        self.assertEqual(len(bus.events), events_after, "после privacy событий нет")
        state = svc.handle_get_meeting_live_state({})
        self.assertFalse(state["active"])
        svc.close()


class BrainLeaseTestCase(unittest.TestCase):
    def test_meeting_acquires_and_releases_lease(self) -> None:
        import backend.meeting_session_service as mss
        calls: list[tuple[str, Any]] = []

        class _FakeLeaseModule:
            @staticmethod
            def acquire_brain_lease(owner, ttl_sec=30.0, lock_path=None):
                calls.append(("acquire", owner))
                return True

            @staticmethod
            def release_brain_lease(owner, lock_path=None):
                calls.append(("release", owner))

        svc, _, _ = _make_svc(settings_extra={"llm_brain_lease_enabled": True})
        import sys as _sys
        real = _sys.modules.get("backend.brain_lease")
        _sys.modules["backend.brain_lease"] = _FakeLeaseModule()  # type: ignore[assignment]
        try:
            svc.handle_meeting_start({})
            svc.handle_meeting_stop({})
        finally:
            if real is not None:
                _sys.modules["backend.brain_lease"] = real
            else:
                _sys.modules.pop("backend.brain_lease", None)
        self.assertIn(("acquire", "krab_ear"), calls)
        self.assertIn(("release", "krab_ear"), calls)
        del mss  # noqa: F821 -- использован только для читаемости импорта
```

- [ ] **Step 2: Прогнать — часть может упасть; чинить РЕАЛИЗАЦИЮ Task 5, не тесты**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_session_service_W_C2a.py -q -p no:cacheprovider`
Expected после фиксов: 17 passed

- [ ] **Step 3: flake8 + commit**

```bash
.venv_krab_ear/bin/python -m flake8 KrabEar/backend/meeting_session_service.py KrabEar/tests/test_meeting_session_service_W_C2a.py --max-line-length=150
git add KrabEar/backend/meeting_session_service.py KrabEar/tests/test_meeting_session_service_W_C2a.py
git commit -m "test(meeting): ITEMS_LLM/пауза партиалов/lease/stop/privacy — поведение доказано (C2a)"
```

---

### Task 7: Проводка в BackendService + dispatch + privacy-тесты

**Files:**
- Modify: `KrabEar/backend/service.py`
- Test: `KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py` (create)

- [ ] **Step 1: Падающий тест**

```python
"""Dispatch-invariant + privacy + интеграция BackendService для meeting_* (C2a)."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SERVICE_PY = PROJECT_ROOT / "backend" / "service.py"

_METHODS = {"meeting_start", "meeting_stop", "get_meeting_live_state"}


class MeetingDispatchInvariantTestCase(unittest.TestCase):
    def test_methods_registered_in_dispatch_table(self) -> None:
        src = SERVICE_PY.read_text(encoding="utf-8")
        keys = set(re.findall(r'"([a-z][a-z0-9_]*)"\s*:', src))
        missing = _METHODS - keys
        self.assertSetEqual(missing, set(),
                            f"meeting-методы отсутствуют в dispatch: {missing}")

    def test_service_close_stops_meeting_worker(self) -> None:
        src = SERVICE_PY.read_text(encoding="utf-8")
        self.assertIn("_meeting_svc.close()", src,
                      "BackendService.close() обязан звать _meeting_svc.close()")


class MeetingBackendIntegrationTestCase(unittest.TestCase):
    """Полный BackendService с фейками: методы диспатчатся и privacy-гейтятся."""

    def setUp(self) -> None:
        from backend.service import BackendService
        from backend.state_store import StateStore
        from tests.test_backend_service import (
            FakeRecorder, FakeTranscriber, FakeTranslator,
        )
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store, recorder=FakeRecorder(),
            transcriber=FakeTranscriber(), translator=FakeTranslator(),
        )

    def tearDown(self) -> None:
        self.service.close()  # 🔴 правило #1782: daemon-треды

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self.service.handle_request(
            {"id": "t", "method": method, "params": params or {}})

    def test_live_state_inactive_by_default(self) -> None:
        resp = self._call("get_meeting_live_state")
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["result"]["active"])

    def test_start_stop_roundtrip(self) -> None:
        start = self._call("meeting_start")
        self.assertTrue(start["ok"])
        self.assertTrue(start["result"]["ok"])
        state = self._call("get_meeting_live_state")
        self.assertTrue(state["result"]["active"])
        stop = self._call("meeting_stop")
        self.assertTrue(stop["result"]["ok"])
        state2 = self._call("get_meeting_live_state")
        self.assertFalse(state2["result"]["active"])

    def test_privacy_gates_all_three(self) -> None:
        self._call("set_settings", {"privacy_mode_enabled": True})
        start = self._call("meeting_start")
        self.assertEqual(start["result"].get("skipped"), "privacy_mode")
        state = self._call("get_meeting_live_state")
        self.assertTrue(state["result"].get("privacy_mode_active"))
        stop = self._call("meeting_stop")
        self.assertTrue(stop["result"]["ok"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что падает** (methods not registered)

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py -q -p no:cacheprovider`
Expected: FAIL на dispatch-инварианте + integration

- [ ] **Step 3: Проводка в `service.py`**

(а) Импорт рядом с другими экстракциями:

```python
from backend.meeting_session_service import MeetingSessionService
```

(б) Конструирование в `__init__` ПОСЛЕ `self._recording_core_svc` и `self._action_items_extractor` (рядом с `_search_and_analysis_svc`, ~строка 975):

```python
        self._meeting_svc = MeetingSessionService(
            recorder=self.recorder,
            transcriber=self.transcriber,
            recording_core=self._recording_core_svc,
            action_items_extractor=self._action_items_extractor,
            settings_get=self._get_runtime_setting,
            event_bus=event_bus,
        )
```

(в) Три записи в `_build_dispatch_table` (рядом с `extract_action_items`):

```python
        "meeting_start": self._meeting_svc.handle_meeting_start,  # C2a: старт/повышение live-встречи
        "meeting_stop": self._meeting_svc.handle_meeting_stop,  # C2a: финализация live-встречи
        "get_meeting_live_state": self._meeting_svc.handle_get_meeting_live_state,  # C2a: снимок для панели
```

(г) В `BackendService.close()` (рядом с остановкой других коллабораторов):

```python
        try:
            self._meeting_svc.close()
        except Exception:
            logger.debug("close: meeting_svc.close() error (ignored)", exc_info=True)
```

Замечание: точное имя поля recording-сервиса проверь по файлу (`grep -n "_recording_core_svc\|RecordingCoreService(" KrabEar/backend/service.py`) — используй фактическое.

- [ ] **Step 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py KrabEar/tests/test_meeting_session_service_W_C2a.py -q -p no:cacheprovider`
Expected: все passed

- [ ] **Step 5: Смежные наборы (dispatch-инварианты всего сервиса + backend_service)**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_backend_service.py -q -p no:cacheprovider && make dispatch-tests`
Expected: passed

- [ ] **Step 6: flake8 + commit**

```bash
.venv_krab_ear/bin/python -m flake8 KrabEar/backend/service.py KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py --max-line-length=150
git add KrabEar/backend/service.py KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py
git commit -m "feat(ipc): meeting_start/meeting_stop/get_meeting_live_state — проводка + privacy (C2a)"
```

---

### Task 8: Документация IPC + живой e2e-смок

**Files:**
- Modify: `docs/IPC_API_REFERENCE.md` (новая секция рядом с meeting report)
- Create: `scripts/e2e_meeting_smoke.py`

- [ ] **Step 1: Документация** — добавить секцию «Live Meeting (C2a, 2026-07-11)» по образцу соседних: для каждого из 3 методов — назначение, params, response-схема с примером (взять фактические ключи из `handle_get_meeting_live_state`), пометка privacy-gated.

- [ ] **Step 2: e2e-скрипт** (по образцу `scripts/e2e_ipc_smoke.py`: socket-клиент `call(method, params)` newline-JSON over AF_UNIX; сокет — `<data-dir>/krabear.sock`):

```python
#!/usr/bin/env python3
"""Живой e2e-смок C2a: meeting-сессия против THROWAWAY backend.

Запуск (руками, НЕ CI):
  python KrabEar/main.py --data-dir /tmp/krab_ear_meeting_e2e &   # throwaway
  python3 scripts/e2e_meeting_smoke.py /tmp/krab_ear_meeting_e2e/krabear.sock

Проверяет: start -> активная сессия -> транскрипт растёт (реальный CHUNK_STT
по микрофону ЛИБО тишина -> len==0, оба валидны, важно отсутствие ошибок) ->
stop -> финальный history_id -> сессия неактивна. items требуют LM Studio —
проверяются мягко (degraded.llm допустим).
"""
import json
import socket
import sys
import time


def call(sock_path: str, method: str, params: dict | None = None) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(600)
    s.connect(sock_path)
    s.sendall(json.dumps({"id": "e2e", "method": method,
                          "params": params or {}}).encode() + b"\n")
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(1 << 20)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.decode())


def main() -> int:
    sock = sys.argv[1] if len(sys.argv) > 1 else "/tmp/krab_ear_meeting_e2e/krabear.sock"
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("OK  " if cond else "FAIL") + f" {name} {detail}")
        if not cond:
            fails.append(name)

    r = call(sock, "meeting_start")
    check("meeting_start ok", r.get("ok") and r["result"].get("ok"), str(r)[:200])

    time.sleep(30)  # один CHUNK_STT-такт (default 25с)
    st = call(sock, "get_meeting_live_state")["result"]
    check("state active", st.get("active") is True, str(st)[:200])
    check("no crash in degraded", isinstance(st.get("degraded"), dict))
    print(f"    transcript_len={st.get('transcript_len')} tail={st.get('transcript_tail', '')[:80]!r}")

    r = call(sock, "meeting_stop")
    check("meeting_stop ok", r.get("ok") and r["result"].get("ok"), str(r)[:200])
    print(f"    item_id={r['result'].get('item_id')}")

    st2 = call(sock, "get_meeting_live_state")["result"]
    check("inactive after stop", st2.get("active") is False)

    print("\n" + ("ALL GREEN" if not fails else f"FAILS: {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Прогнать e2e вживую** (throwaway data-dir, НЕ прод!)

```bash
source .venv_krab_ear/bin/activate
python KrabEar/main.py --data-dir /tmp/krab_ear_meeting_e2e &
sleep 8
python3 scripts/e2e_meeting_smoke.py /tmp/krab_ear_meeting_e2e/krabear.sock
kill %1
```
Expected: `ALL GREEN` (transcript_len может быть 0 при тишине в микрофоне — это валидно; важно отсутствие FAIL)

- [ ] **Step 4: Commit**

```bash
git add docs/IPC_API_REFERENCE.md scripts/e2e_meeting_smoke.py
git commit -m "docs(ipc)+e2e: live meeting методы + живой смок-скрипт (C2a)"
```

---

### Task 9: Полные гейты волны

- [ ] **Step 1: Все новые тест-файлы разом + смежные**

Run: `PYTHONPATH=$(pwd)/KrabEar python3 -m pytest KrabEar/tests/test_meeting_recorder_range_W_C2a.py KrabEar/tests/test_meeting_partial_pause_W_C2a.py KrabEar/tests/test_meeting_session_service_W_C2a.py KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py KrabEar/tests/test_backend_service.py -q -p no:cacheprovider`
Expected: все passed

- [ ] **Step 2: ubuntu-parity (обязателен для новых тест-файлов)**

Run: `bash scripts/pre_merge_py312_check.sh KrabEar/tests/test_meeting_recorder_range_W_C2a.py KrabEar/tests/test_meeting_partial_pause_W_C2a.py KrabEar/tests/test_meeting_session_service_W_C2a.py KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py`
Expected: `ALL GREEN (ubuntu-parity py3.12, mlx absent)`

- [ ] **Step 3: Аудиты (новый extracted-модуль — обязательно!)**

Run: `make audit-all`
Expected: all clean (meeting_session_service импортируется в service.py — dead-module guard доволен; декоративный-wiring guard видит вызовы `_meeting_svc.handle_*` в dispatch)

- [ ] **Step 4: flake8 всех тронутых файлов одной командой**

Run: `.venv_krab_ear/bin/python -m flake8 KrabEar/backend/recorder.py KrabEar/backend/realtime_partial.py KrabEar/backend/recording_core_service.py KrabEar/backend/meeting_session_service.py KrabEar/backend/service.py KrabEar/backend/settings_validator.py KrabEar/core/config.py scripts/e2e_meeting_smoke.py --max-line-length=150`
Expected: пусто

- [ ] **Step 5: Финальный commit (если были правки по ходу гейтов)**

```bash
git add -A && git status --short
git commit -m "chore(c2a): финальные гейты волны — тесты/parity/аудиты зелёные" || true
```

---

## Self-review плана (выполнен автором)

1. **Spec coverage**: §2.1 (3 IPC + privacy) → Tasks 5/7; §2.2 (слот, паузы, lease, skip-tick, degraded) → Tasks 2/3/5/6; §2.3 (snapshot_range, курсор, конкатенация) → Tasks 1/5; §2.4 (items замещение, growth-guard, extractor as-is) → Tasks 5/6; §2.6 backend-часть (события meeting.*) → Task 5; §2.8 (настройки+RANGE) → Task 4; §3 (деградации: llm/privacy/out-of-band) → Tasks 5/6; §4 (юниты/dispatch/privacy/e2e) → Tasks 1-8. DIAR_WINDOW — enum-слот без исполнителя (по заданию C2a). Адаптивный items-интервал — `_items_interval()`.
2. **Placeholder scan**: чисто; все код-шаги содержат полный код.
3. **Type consistency**: `MeetingJob`/`_MeetingSession`/`handle_meeting_*`/`snapshot_range`/`pause_realtime_partials` — имена согласованы во всех тасках; ответ stop использует `history_id` источника и отдаёт `item_id` наружу (зафиксировано в Task 5 коде и Task 6 тесте).
