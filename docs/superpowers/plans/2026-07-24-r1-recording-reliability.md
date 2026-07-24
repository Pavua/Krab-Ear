# R1 «Надёжность записи» — Implementation Plan (Фаза 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Записанное аудио (диктовка и встреча) переживает любую смерть backend-процесса (SIGTERM/SIGKILL/crash) и восстанавливается на следующем старте; каждый рестарт объясним постфактум.

**Architecture:** Continuous spill сырых PCM-фреймов на диск из воркера `AudioRecorder` (flush на каждый чанк); восстановление `.part`-файлов на старте (WAV → уведомление → транскрипция с privacy-гейтом); расширение `shutdown_info.json` + dirty-marker + сбор форензики свежего `log show` после некорректной смерти. Спека: `docs/superpowers/specs/2026-07-24-r1-recording-reliability-design.md`.

**Tech Stack:** Python 3.14 (`.venv_krab_ear`), numpy, stdlib `wave` (НЕ soundfile — ubuntu-CI без него живёт только через guarded import), threading, subprocess (`log show`), unittest.

**Фаза 2 (salvage WIP codex) в этот план НЕ входит** — по спеке §4.4/§5 она начинается с инвентаризации и получает СВОЙ план после завершения Фазы 1.

## Global Constraints

- Запуск тестов: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/<file> -v -p no:cacheprovider` из корня репо; venv: `source .venv_krab_ear/bin/activate`.
- Каждый тест, создающий `BackendService(...)`, ОБЯЗАН звать `self.service.close()` в tearDown (правило #1782). В этом плане BackendService в тестах не нужен — используем сервисы напрямую с фейками.
- Никакого I/O и локов внутри signal-callback (`GracefulShutdownHandler._signal_handler`) — только присваивания простых атрибутов (урок F1/F5 приёмки #1891).
- Никакого I/O под `AudioRecorder._lock` (история deadlock W1652/F3 — error_bus push уже вынесен из-под лока; spill-запись тоже строго вне лока).
- Runtime-настройки читать через `settings.get(...)`/`_get_runtime_setting`, НЕ `DEFAULT_SETTINGS.get` (урок Wave 58).
- Новые тест-файлы прогнать через `scripts/pre_merge_py312_check.sh <файлы>` (ubuntu-parity, только тест-файлы!) перед финишем задачи.
- flake8 по CI-команде (W293 в тестах НЕ расслаблен); не оставлять trailing whitespace.
- Коммиты с явными путями (`git add <files>`), НИКОГДА `git add -A`. Trailer: `Co-Authored-By: Claude <worker>`.
- Ошибки spill/rescue/forensics НИКОГДА не роняют запись/старт — fail-open с одним WARN (spill вспомогателен; направление отказа: работающая диктовка важнее защиты).

---

### Task 1: `RecordingSpillWriter` + финализация `.part` → WAV

**Files:**
- Create: `KrabEar/backend/recording_spill.py`
- Test: `KrabEar/tests/test_recording_spill.py`

**Interfaces:**
- Produces (для Task 2/3/4):
  - `RecordingSpillWriter(rescue_dir: Path, sample_rate: int, channels: int, source: str = "unknown")`
  - `.open() -> bool` — создаёт `<session_id>.f32.part` + `<session_id>.meta.json`; False при IO-ошибке.
  - `.append(chunk: np.ndarray) -> None` — write+flush; при ошибке диска один WARN и самоотключение (`.failed = True`), больше не пишет.
  - `.close() -> None` — закрыть fd, файлы ОСТАВИТЬ (сценарий спасения).
  - `.discard() -> None` — close + unlink `.part` и `.meta.json` (идемпотентно).
  - `.session_id: str`, `.part_path: Path`, `.failed: bool`
  - `finalize_part_to_wav(part_path: Path) -> Path | None` — по сайдкару собирает `<session_id>.rescued.wav` (float32 → int16, выравнивание по 4 байта), удаляет `.part`+`.meta.json` при успехе; None при ошибке/пустом файле (<0.5с аудио — мусор, файлы удаляются).

- [ ] **Step 1: Написать падающие тесты**

```python
"""Тесты RecordingSpillWriter (R1 Фаза 1, Task 1).

Без sounddevice: писатель работает с чистыми numpy-чанками.
"""
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_spill import RecordingSpillWriter, finalize_part_to_wav  # noqa: E402


class RecordingSpillWriterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rescue_dir = Path(self._tmp.name) / "rescue"

    def tearDown(self):
        self._tmp.cleanup()

    def _writer(self, **kw):
        params = dict(rescue_dir=self.rescue_dir, sample_rate=16000, channels=1, source="dictation")
        params.update(kw)
        return RecordingSpillWriter(**params)

    def test_open_creates_part_and_meta(self):
        w = self._writer()
        self.assertTrue(w.open())
        self.assertTrue(w.part_path.exists())
        meta = json.loads((self.rescue_dir / f"{w.session_id}.meta.json").read_text())
        self.assertEqual(meta["sample_rate"], 16000)
        self.assertEqual(meta["channels"], 1)
        self.assertEqual(meta["source"], "dictation")
        self.assertIn("started_at_iso", meta)
        w.discard()

    def test_append_flushes_to_disk_immediately(self):
        w = self._writer()
        self.assertTrue(w.open())
        chunk = np.ones(1600, dtype=np.float32) * 0.5
        w.append(chunk)
        # Данные должны быть на диске БЕЗ close() — переживание kill -9.
        self.assertEqual(w.part_path.stat().st_size, 1600 * 4)
        w.discard()

    def test_close_keeps_files_discard_removes(self):
        w = self._writer()
        self.assertTrue(w.open())
        w.append(np.zeros(160, dtype=np.float32))
        w.close()
        self.assertTrue(w.part_path.exists())
        w.discard()
        self.assertFalse(w.part_path.exists())
        self.assertFalse((self.rescue_dir / f"{w.session_id}.meta.json").exists())

    def test_discard_idempotent(self):
        w = self._writer()
        self.assertTrue(w.open())
        w.discard()
        w.discard()  # не должно бросать

    def test_append_io_error_disables_writer_not_raises(self):
        w = self._writer()
        self.assertTrue(w.open())
        w._fh.close()  # симулируем умерший дескриптор
        w.append(np.zeros(160, dtype=np.float32))  # не должно бросить
        self.assertTrue(w.failed)
        # Повторный append — тихий no-op
        w.append(np.zeros(160, dtype=np.float32))
        w.discard()

    def test_open_failure_returns_false(self):
        # rescue_dir указывает на ФАЙЛ → mkdir внутри провалится
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("x")
        w = RecordingSpillWriter(rescue_dir=blocker / "sub", sample_rate=16000,
                                 channels=1, source="dictation")
        self.assertFalse(w.open())
        self.assertTrue(w.failed)


class FinalizePartTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rescue_dir = Path(self._tmp.name) / "rescue"

    def tearDown(self):
        self._tmp.cleanup()

    def _make_part(self, samples: np.ndarray, sample_rate=16000) -> Path:
        w = RecordingSpillWriter(rescue_dir=self.rescue_dir, sample_rate=sample_rate,
                                 channels=1, source="dictation")
        self.assertTrue(w.open())
        w.append(samples.astype(np.float32))
        w.close()
        return w.part_path

    def test_finalize_produces_wav_and_cleans_part(self):
        part = self._make_part(np.ones(16000, dtype=np.float32) * 0.25)  # 1с
        wav_path = finalize_part_to_wav(part)
        self.assertIsNotNone(wav_path)
        self.assertTrue(wav_path.name.endswith(".rescued.wav"))
        self.assertFalse(part.exists())
        self.assertFalse(part.with_name(part.name.replace(".f32.part", ".meta.json")).exists())
        with wave.open(str(wav_path), "rb") as wf:
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getnframes(), 16000)

    def test_finalize_truncated_tail_rounds_down(self):
        part = self._make_part(np.zeros(16000, dtype=np.float32))
        with part.open("ab") as fh:
            fh.write(b"\x01\x02\x03")  # обрыв посреди семпла
        wav_path = finalize_part_to_wav(part)
        self.assertIsNotNone(wav_path)
        with wave.open(str(wav_path), "rb") as wf:
            self.assertEqual(wf.getnframes(), 16000)

    def test_finalize_too_short_removes_garbage(self):
        part = self._make_part(np.zeros(1000, dtype=np.float32))  # 62мс < 0.5с
        self.assertIsNone(finalize_part_to_wav(part))
        self.assertFalse(part.exists())

    def test_finalize_missing_meta_returns_none_keeps_part(self):
        part = self._make_part(np.zeros(16000, dtype=np.float32))
        part.with_name(part.name.replace(".f32.part", ".meta.json")).unlink()
        self.assertIsNone(finalize_part_to_wav(part))
        # Без сайдкара не знаем sample_rate — файл НЕ трогаем (не наш мусор).
        self.assertTrue(part.exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться, что тесты падают правильно**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_recording_spill.py -v -p no:cacheprovider`
Expected: FAIL/ERROR c `ModuleNotFoundError: No module named 'backend.recording_spill'`

- [ ] **Step 3: Реализация**

```python
"""Continuous spill сырого аудио записи на диск (R1 Фаза 1).

Во время записи AudioRecorder дописывает каждый чанк в
``<data_dir>/rescue/<session_id>.f32.part`` (+ JSON-сайдкар с параметрами).
При любой смерти процесса аудио уже на диске; восстановление —
``backend/recording_rescue.py``. Ошибки диска НИКОГДА не роняют запись:
писатель самоотключается с одним WARN (fail-open в сторону диктовки).
"""

from __future__ import annotations

import json
import logging
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

logger = logging.getLogger("KrabEar.Backend.RecordingSpill")

# Короче этого восстановленный файл считается мусором (щелчок старта записи).
_MIN_RESCUE_SEC = 0.5
_BYTES_PER_SAMPLE = 4  # float32


class RecordingSpillWriter:
    """Односессионный append-only писатель spill-файла.

    Потоко-дисциплина: append() зовёт ТОЛЬКО worker-тред AudioRecorder
    (строго вне recorder._lock); open/close/discard — lifecycle-код под
    recorder._lifecycle_lock. Собственный лок не нужен.
    """

    def __init__(self, rescue_dir: Path, sample_rate: int, channels: int,
                 source: str = "unknown") -> None:
        self.session_id = uuid.uuid4().hex
        self._rescue_dir = Path(rescue_dir)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.source = str(source)
        self.part_path = self._rescue_dir / f"{self.session_id}.f32.part"
        self._meta_path = self._rescue_dir / f"{self.session_id}.meta.json"
        self._fh = None
        self.failed = False

    def open(self) -> bool:
        try:
            self._rescue_dir.mkdir(parents=True, exist_ok=True)
            self._meta_path.write_text(json.dumps({
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "source": self.source,
                "started_at_iso": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False), encoding="utf-8")
            self._fh = self.part_path.open("ab")
            return True
        except Exception:
            self.failed = True
            logger.warning("RecordingSpill: open() провалился — spill выключен "
                           "для этой записи", exc_info=True)
            return False

    def append(self, chunk: "np.ndarray") -> None:
        if self.failed or self._fh is None:
            return
        try:
            self._fh.write(np.ascontiguousarray(
                chunk.reshape(-1), dtype=np.float32).tobytes())
            # Python-буфер не переживает kill -9; flush → данные в page cache ОС,
            # который переживает смерть процесса. Один syscall на 0.1с-чанк.
            self._fh.flush()
        except Exception:
            self.failed = True
            logger.warning("RecordingSpill: ошибка дозаписи — spill выключен "
                           "для этой записи", exc_info=True)

    def close(self) -> None:
        """Закрыть fd; файлы ОСТАВИТЬ (главный сценарий спасения)."""
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                logger.debug("RecordingSpill: close() error", exc_info=True)
            self._fh = None

    def discard(self) -> None:
        """close + удалить файлы. Идемпотентно; зовётся после персиста в history."""
        self.close()
        for p in (self.part_path, self._meta_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                logger.debug("RecordingSpill: discard unlink error", exc_info=True)


def finalize_part_to_wav(part_path: Path) -> Path | None:
    """Собрать ``<id>.rescued.wav`` из ``<id>.f32.part`` + сайдкара.

    Возвращает путь к WAV или None (ошибка / слишком коротко). Успех и
    «слишком коротко» удаляют исходные файлы; отсутствие сайдкара оставляет
    всё как есть (не знаем формат — не наш мусор).
    """
    part_path = Path(part_path)
    meta_path = part_path.with_name(part_path.name.replace(".f32.part", ".meta.json"))
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        sample_rate = int(meta["sample_rate"])
        channels = int(meta["channels"])
    except Exception:
        logger.warning("RecordingSpill: finalize без сайдкара — пропуск %s",
                       part_path.name, exc_info=True)
        return None

    def _cleanup() -> None:
        for p in (part_path, meta_path):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    try:
        raw = part_path.read_bytes()
        usable = len(raw) - (len(raw) % _BYTES_PER_SAMPLE)
        samples = np.frombuffer(raw[:usable], dtype=np.float32)
        if samples.size < _MIN_RESCUE_SEC * sample_rate * channels:
            logger.info("RecordingSpill: %s короче %.1fс — удаляю как мусор",
                        part_path.name, _MIN_RESCUE_SEC)
            _cleanup()
            return None
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
        wav_path = part_path.with_name(
            part_path.name.replace(".f32.part", ".rescued.wav"))
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16.tobytes())
        _cleanup()
        return wav_path
    except Exception:
        logger.warning("RecordingSpill: finalize %s провалился",
                       part_path.name, exc_info=True)
        return None
```

- [ ] **Step 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_recording_spill.py -v -p no:cacheprovider`
Expected: все PASS

- [ ] **Step 5: ubuntu-parity + flake8 + коммит**

```bash
scripts/pre_merge_py312_check.sh KrabEar/tests/test_recording_spill.py
flake8 KrabEar/backend/recording_spill.py KrabEar/tests/test_recording_spill.py
git add KrabEar/backend/recording_spill.py KrabEar/tests/test_recording_spill.py
git commit -m "feat(r1): RecordingSpillWriter + finalize .part→WAV (Task 1)"
```

---

### Task 2: интеграция spill в `AudioRecorder`

**Files:**
- Modify: `KrabEar/backend/recorder.py` (сигнатура `start()`, воркер-цикл, `stop()`, `abort()`)
- Test: `KrabEar/tests/test_recorder_spill_integration.py`

**Interfaces:**
- Consumes: `RecordingSpillWriter` (duck-typed: `.append/.close/.failed`) из Task 1.
- Produces (для Task 3): `AudioRecorder.start(spill=None) -> bool` — принимает открытый writer или None; recorder зовёт `spill.append(chunk)` из воркера (вне `_lock`) и `spill.close()` в `stop()`/`abort()`/авто-лимите. Recorder НИКОГДА не зовёт `discard()` — удаление принадлежит `RecordingCoreService` (Task 3).

**Точки врезки в `recorder.py` (номера строк на момент написания плана):**
1. `start()` (строка 89): новый kwarg `spill: "object | None" = None`; под `self._lock` — `self._spill = spill` (рядом с `self._chunks = []`).
2. `__init__`: `self._spill = None` (рядом с `self._pending_result`).
3. `_worker()` (строка 348): в начале — `with self._lock: ... spill = self._spill` (захватить локальную ссылку рядом с чтением `device`); в цикле сразу ПОСЛЕ блока `with self._lock:` (который делает `self._chunks.append(data.copy())`, строка ~393) и ДО `if _max_duration_exceeded:` — `if spill is not None and not _max_duration_exceeded: spill.append(data)`. Строго вне `_lock` (Global Constraints).
4. `stop()`: во ВСЕХ ветках выхода (успешный сбор; обе `pending`-ветки авто-лимита; ранний выход «нечего отдавать» — воркер умер мгновенно) — под `self._lock` забрать `spill_local = self._spill; self._spill = None`, после выхода из lock `if spill_local is not None: spill_local.close()`. Исключение — ветка `AudioRecorderStopTimeout` (raise): воркер завис и может ещё писать — спилл НЕ трогать.
5. `abort()`: в финальном `with self._lock:` (строка ~225) забрать и обнулить `self._spill`; после лока — `.close()` (файлы остаются — это shutdown-путь).
6. Ветка авто-лимита в `_worker` (строка ~399, `if _max_duration_exceeded:`): после установки `_pending_result` — `if spill is not None: spill.close()`.

- [ ] **Step 1: Падающие тесты**

```python
"""Интеграция spill в AudioRecorder (R1 Task 2) — без sounddevice."""
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recorder import AudioRecorder  # noqa: E402


class FakeSpill:
    def __init__(self):
        self.appended = []
        self.closed = False
        self.discarded = False
        self.failed = False

    def append(self, chunk):
        self.appended.append(np.asarray(chunk).size)

    def close(self):
        self.closed = True

    def discard(self):
        self.discarded = True


class RecorderSpillTest(unittest.TestCase):
    def test_start_accepts_spill_kwarg_and_stores_it(self):
        r = AudioRecorder()
        spill = FakeSpill()
        # sd отсутствует в CI: воркер умрёт сразу, но start() обязан принять kwarg
        r.start(spill=spill)
        self.assertIs(r._spill, spill)
        r.abort(timeout_sec=0.5)

    def test_stop_closes_spill_never_discards(self):
        r = AudioRecorder()
        spill = FakeSpill()
        # Симулируем состояние «запись шла»: без реального воркера
        with r._lock:
            r._spill = spill
            r._is_recording = True
            r._chunks = [np.zeros(160, dtype=np.float32)]
            r._chunks_total_samples = 160
        result = r.stop(timeout_sec=0.5)
        self.assertIsNotNone(result)
        self.assertTrue(spill.closed)
        self.assertFalse(spill.discarded)
        self.assertIsNone(r._spill)

    def test_abort_closes_spill_keeps_files(self):
        r = AudioRecorder()
        spill = FakeSpill()
        with r._lock:
            r._spill = spill
            r._is_recording = True
        self.assertTrue(r.abort(timeout_sec=0.5))
        self.assertTrue(spill.closed)
        self.assertFalse(spill.discarded)
        self.assertIsNone(r._spill)

    def test_start_without_spill_backward_compatible(self):
        r = AudioRecorder()
        r.start()
        self.assertIsNone(r._spill)
        r.abort(timeout_sec=0.5)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Убедиться в падении** (`TypeError: start() got an unexpected keyword argument 'spill'` / `AttributeError: _spill`)

- [ ] **Step 3: Реализация по «точкам врезки» выше.** Правки минимальны и точечны; НЕ трогать логику локов. В воркере — комментарий:

```python
                    # R1 spill: строго ВНЕ self._lock (I/O под локом — запретный
                    # класс W1652/F3). Ошибки диска гасятся внутри append().
                    if spill is not None and not _max_duration_exceeded:
                        spill.append(data)
```

- [ ] **Step 4: Зелёные тесты + регрессия существующих**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_recorder_spill_integration.py KrabEar/tests/test_audio_recorder_lifecycle.py -v -p no:cacheprovider`
Expected: все PASS (lifecycle-сьюта не должна заметить изменений)

- [ ] **Step 5: ubuntu-parity + коммит**

```bash
scripts/pre_merge_py312_check.sh KrabEar/tests/test_recorder_spill_integration.py KrabEar/tests/test_audio_recorder_lifecycle.py
git add KrabEar/backend/recorder.py KrabEar/tests/test_recorder_spill_integration.py
git commit -m "feat(r1): AudioRecorder принимает spill-writer (Task 2)"
```

---

### Task 3: проводка в `RecordingCoreService` + настройка + source из meeting

**Files:**
- Modify: `KrabEar/backend/recording_core_service.py` (`__init__`, `_handle_start_recording_locked`, `handle_stop_recording`)
- Modify: `KrabEar/backend/service.py` (конструирование RecordingCoreService — передать `rescue_dir`)
- Modify: `KrabEar/backend/meeting_session_service.py:214` (`handle_start_recording({"source": "meeting"})`)
- Modify: `KrabEar/core/config.py` (~строка 1046, рядом с `privacy_mode_enabled`): `"recording_spill_enabled": True,` с комментарием
- Test: `KrabEar/tests/test_recording_spill_wiring.py`

**Interfaces:**
- Consumes: `RecordingSpillWriter` (Task 1), `AudioRecorder.start(spill=...)` (Task 2).
- Produces (для Task 4): договорённость о каталоге `Path(store.data_dir) / "rescue"` и правилах удаления (`discard()` только после: персиста с `history_id`, dedup-скипа, silence-early-return).

**Логика:**
1. `__init__`: новый kwarg `rescue_dir: "Path | None" = None`; `self._rescue_dir = rescue_dir`; `self._active_spill: Any = None` (доступ только под `_recording_lifecycle_lock` — start и phase A уже живут под ним).
2. `_handle_start_recording_locked` — сразу после успешного `started = self.recorder.start()` → ПЕРЕДЕЛАТЬ на: собрать writer ДО `start()`:

```python
        spill = None
        if self._rescue_dir is not None and bool(
            _settings_pre.get("recording_spill_enabled", True)
        ):
            try:
                from backend.recording_spill import RecordingSpillWriter
                spill = RecordingSpillWriter(
                    rescue_dir=self._rescue_dir,
                    sample_rate=int(getattr(self.recorder, "sample_rate", 16000)),
                    channels=int(getattr(self.recorder, "channels", 1)),
                    source=str(params.get("source", "dictation")),
                )
                if not spill.open():
                    spill = None
            except Exception:
                logger.warning("RecordingSpill: не удалось создать writer — "
                               "запись продолжается без spill", exc_info=True)
                spill = None
        started = self.recorder.start(spill=spill)
        if not started:
            if spill is not None:
                spill.discard()  # запись не началась — файл-пустышка не нужен
            ...  # существующая ветка already_recording/recorder_stopping без изменений
        self._active_spill = spill
```

3. `handle_stop_recording` (оркестратор, строка 441): после `phase_a` без `early_return` — забрать writer:

```python
        # R1: спилл принадлежит завершившейся записи; новый start создаст свой.
        spill = self._active_spill
        self._active_spill = None
```

   (phase A выполняется под lifecycle-lock, но оркестратор — уже вне его; забор здесь безопасен, потому что конкурентный start_recording при живом воркере получает `already_recording` и `_active_spill` не трогает. При `recorder_timeout` early-return спилл НЕ забирать — worker завис, файл должен пережить возможный рестарт.)
   Дальше по веткам:
   - `phase_b.early_return` (тишина/фоновая речь — записи в history не будет, юзер получает явный ответ): `if spill: spill.discard()` перед return.
   - `phase_c`/`phase_d` early_return (STT упал / пустой текст): спилл ОСТАВИТЬ (`if spill: spill.close()` — close уже сделан recorder.stop(), повторный безопасен) — восстановление на следующем старте вернёт аудио, которое сейчас потеряно.
   - после `phase_e`: `resp = self._stop_recording_phase_e(...)`, затем

```python
        if spill is not None:
            if resp.get("history_id") or resp.get("skipped") == "duplicate":
                spill.discard()
            # иначе: оставить для восстановления (персист не состоялся)
        return resp
```

4. `service.py`: найти конструирование `RecordingCoreService(` (grep: `grep -n "RecordingCoreService(" KrabEar/backend/service.py`) и добавить `rescue_dir=Path(self.store.data_dir) / "rescue",` (Path уже импортирован в service.py; если нет — добавить импорт).
5. `meeting_session_service.py:214`: `start_resp = self._recording_core.handle_start_recording({"source": "meeting"})`.
6. `config.py` DEFAULT_SETTINGS:

```python
    # --- R1: continuous spill записи (crash-safety) ---
    # Во время записи сырые фреймы дублируются на диск (<data_dir>/rescue/);
    # при смерти backend аудио восстанавливается на следующем старте.
    "recording_spill_enabled": True,
```

- [ ] **Step 1: Падающие тесты** — `test_recording_spill_wiring.py`: фейковый recorder (записывает, что ему передали в `start(spill=...)`), фейковый store с `data_dir`, реальный `RecordingCoreService` с минимальными фейками по образцу `KrabEar/tests/test_recording_core_service.py` (скопировать оттуда набор фейк-коллабораторов). Кейсы:
  - `test_start_passes_open_spill_to_recorder` (setting включён → у recorder в start оказался writer с существующим `.part`);
  - `test_start_with_setting_disabled_passes_none`;
  - `test_start_source_param_reaches_meta` (source="meeting" → meta.json содержит meeting);
  - `test_stop_discards_after_persist` (фейковые фазы: прогнать `handle_stop_recording` с recorder, отдающим ненулевое аудио, store.add_history_item возвращает item с id → `.part` удалён);
  - `test_stop_keeps_spill_when_stt_fails` (transcriber бросает → early_return phase_c → `.part` жив);
  - `test_start_failure_discards_placeholder` (recorder.start → False → файл удалён).

Полные фейки писать по образцу существующего `test_recording_core_service.py` (worker обязан прочитать его setUp и переиспользовать структуру, НЕ изобретая свою).

- [ ] **Step 2: Verify FAIL** (`TypeError: __init__() got an unexpected keyword argument 'rescue_dir'`)
- [ ] **Step 3: Реализация по логике выше**
- [ ] **Step 4: Зелёные + регрессия**

Run: `PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_recording_spill_wiring.py KrabEar/tests/test_recording_core_service.py KrabEar/tests/test_meeting_session_service_W_C2a.py -v -p no:cacheprovider`
Expected: все PASS

- [ ] **Step 5: ubuntu-parity + коммит**

```bash
scripts/pre_merge_py312_check.sh KrabEar/tests/test_recording_spill_wiring.py
git add KrabEar/backend/recording_core_service.py KrabEar/backend/service.py \
  KrabEar/backend/meeting_session_service.py KrabEar/core/config.py \
  KrabEar/tests/test_recording_spill_wiring.py
git commit -m "feat(r1): проводка spill в RecordingCoreService + source из meeting (Task 3)"
```

---

### Task 4: восстановление на старте (`recording_rescue.py`) + код ошибки

**Files:**
- Create: `KrabEar/backend/recording_rescue.py`
- Modify: `KrabEar/backend/error_codes.py` (после блока `audio.max_duration_reached`, ~строка 742)
- Modify: `KrabEar/backend/service.py` (запуск фонового rescue-треда в конце `__init__`; grep-якорь: рядом с существующими стартами фоновых компонентов, напр. `DiskSpaceMonitor`)
- Test: `KrabEar/tests/test_recording_rescue.py`

**Interfaces:**
- Consumes: `finalize_part_to_wav` (Task 1); `recording_core.handle_transcribe_paths({"paths": [...]})` (существующий, sync — возвращает `{"items": [...], "processed": int, "errors": [...]}`); ErrorBus (`error_bus.push(KrabError)` — образец в `recorder.py:_push_max_duration_error`); CollectionManager (найти атрибут на BackendService: `grep -n "CollectionManager(" KrabEar/backend/service.py`).
- Produces: `run_rescue_scan(rescue_dir, recording_core, error_bus, settings_get, collection_manager) -> dict` (`{"rescued": int, "transcribed": int, "kept_wavs": int}`), single-flight.

**Логика `run_rescue_scan`:**
1. Module-level `_scan_lock = threading.Lock()`; `acquire(blocking=False)` — второй вызов немедленно выходит `{"rescued": 0, ...}` (restart-шторм-гард).
2. `sorted(rescue_dir.glob("*.f32.part"))`, максимум 10 за проход (лимит из спеки §4.2).
3. Каждый: `finalize_part_to_wav(part)` → None → continue; иначе push `audio.recording_rescued` в ErrorBus (контекст: `{"wav": имя-файла-без-пути, "source": из meta}` — БЕЗ абсолютного пути в user-сообщении).
4. Если `settings_get("privacy_mode_enabled", False)` → WAV оставить, транскрипцию НЕ делать (`kept_wavs += 1`), continue.
5. Иначе `resp = recording_core.handle_transcribe_paths({"paths": [str(wav)]})`; при `resp.get("processed", 0) > 0 и not resp.get("errors")`:
   - item_id из `resp["items"][0]["id"]` (проверить фактическую форму item в `_transcribe_paths_core` return — worker обязан свериться с кодом, строка ~2260 `return {`);
   - добавить в коллекцию «Восстановленные записи»: `collection_manager` — найти существующие методы (grep `def .*collection` в `KrabEar/backend/collection_manager.py`), создать коллекцию, если нет, добавить item; всё в try/except (fail-open, коллекция — украшение, не гарантия);
   - `wav.unlink(missing_ok=True)`.
   Иначе — WAV оставить (`kept_wavs += 1`), WARN.
6. Никогда не бросает; всё под общим try/except с `logger.exception`.

**error_codes.py** (вставить после блока `audio.max_duration_reached`):

```python
    # ── R1 (2026-07-24): аудио записи восстановлено после смерти backend ─────
    # recording_rescue.run_rescue_scan() пушит при находке .part-файла на старте.
    # actionable=False: восстановление уже произошло автоматически; уведомление
    # информирует (item появится в history / WAV лежит в rescue/ при privacy).
    "audio.recording_rescued": {
        "user_msg_ru": "Найдена незавершённая запись — аудио восстановлено после сбоя",
        "actionable": False,
        "action_id": None,
        "action_label": "",
        "severity": "warn",
        "dedupe_seconds": 5,
    },
```

(dedupe 5с, не 60: при восстановлении ДВУХ файлов подряд оба уведомления должны пройти; 5с хватает от дублей одного файла.)

**service.py** (конец `__init__`, рядом с запуском других фоновых компонентов):

```python
        # R1: восстановление незавершённых записей прошлой жизни процесса.
        # Фоновый тред — старт IPC не ждёт (спека §4.2).
        try:
            from backend.recording_rescue import run_rescue_scan
            _rescue_dir = Path(self.store.data_dir) / "rescue"
            threading.Thread(
                target=run_rescue_scan,
                kwargs=dict(
                    rescue_dir=_rescue_dir,
                    recording_core=self._recording_core,
                    error_bus=self._error_bus,
                    settings_get=self._get_runtime_setting,
                    collection_manager=self._collection_manager,
                ),
                daemon=True,
                name="recording-rescue-scan",
            ).start()
        except Exception:
            logger.warning("recording_rescue: старт скана провалился", exc_info=True)
```

(Точные имена атрибутов `self._recording_core`/`self._error_bus`/`self._collection_manager` worker ОБЯЗАН сверить grep-ом по `service.py` — не угадывать.)

- [ ] **Step 1: Падающие тесты** — `test_recording_rescue.py` с фейками (FakeRecordingCore возвращает управляемый resp; FakeErrorBus копит push'и; FakeCollectionManager копит вызовы; settings_get — lambda):
  - `test_scan_finalizes_and_transcribes` (сеять .part+meta через RecordingSpillWriter → скан → transcribe вызван, WAV удалён, push случился, счётчики верны);
  - `test_privacy_mode_keeps_wav_no_transcription`;
  - `test_transcribe_failure_keeps_wav`;
  - `test_single_flight` (двойной вызов из двух тредов — второй немедленно нулевой);
  - `test_scan_never_raises_on_garbage` (битый .part без meta → скан молча продолжает);
  - `test_limit_10_per_pass` (11 файлов → 10 обработано).
- [ ] **Step 2: Verify FAIL** (ModuleNotFoundError)
- [ ] **Step 3: Реализация**
- [ ] **Step 4: Зелёные** — `pytest KrabEar/tests/test_recording_rescue.py -v`
- [ ] **Step 5: ubuntu-parity + коммит** (add: `recording_rescue.py`, `error_codes.py`, `service.py`, тест)

---

### Task 5: расширение `shutdown_info.json` (сигнал + состояние записи)

**Files:**
- Modify: `KrabEar/backend/shutdown_handler.py` (`__init__`, `_signal_handler`, `shutdown()`, `_persist`)
- Test: `KrabEar/tests/test_shutdown_info_r1_fields.py` (новый файл — существующие shutdown-тесты НЕ трогать)

**Interfaces:**
- Produces (для Task 6): поля payload `shutdown_info.json`: `signal: str|None`, `uptime_sec: float`, `recording_active: bool`, `meeting_active: bool`, `pid: int` (аддитивно к существующим `last_shutdown_time/clean/elapsed_ms/errors`).

**Логика (все ограничения F1/F5 приёмки #1891 сохраняются):**
1. `__init__`: `self._started_monotonic = time.monotonic()`; `self._signal_context: dict | None = None`.
2. `_signal_handler` (строка 501) — ТОЛЬКО присваивания простых объектов, без локов/I/O/логов:

```python
    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Signal-safe запрос: teardown выполнит владелец обычного control-flow."""
        del frame
        service = self._service
        # R1: снимок контекста БЕЗ локов — только голые чтения атрибутов.
        # recorder.is_recording (property) берёт recorder._lock → запрещено здесь;
        # читаем приватный _is_recording напрямую: racy-but-safe (bool, CPython
        # атомарное чтение; худший случай — устаревшее значение в диагностике).
        recorder = getattr(service, "recorder", None)
        meeting = getattr(service, "_meeting_service", None)
        self._signal_context = {
            "signal": signum,
            "recording_active": bool(getattr(recorder, "_is_recording", False)),
            "meeting_active": getattr(meeting, "_session", None) is not None,
        }
        server = getattr(service, "_ipc_server", None)
        request_stop = getattr(server, "request_stop_from_signal", None)
        if callable(request_stop):
            request_stop()
```

   (Имя атрибута meeting-сервиса на BackendService worker обязан сверить: `grep -n "MeetingSessionService(" KrabEar/backend/service.py` — подставить фактическое.)
3. `_persist` — новые поля в payload:

```python
        ctx = self._signal_context or {}
        sig_num = ctx.get("signal")
        try:
            sig_name = signal.Signals(sig_num).name if sig_num is not None else None
        except ValueError:
            sig_name = str(sig_num)
        payload.update({
            "signal": sig_name,
            "uptime_sec": round(time.monotonic() - self._started_monotonic, 1),
            "recording_active": bool(ctx.get("recording_active", False)),
            "meeting_active": bool(ctx.get("meeting_active", False)),
            "pid": os.getpid(),
        })
```

- [ ] **Step 1: Падающие тесты**: (a) `_signal_handler` с фейк-сервисом (recorder._is_recording=True) → `handler._signal_context` заполнен и `request_stop_from_signal` вызван; (b) полный `bind + _signal_handler(SIGTERM) + shutdown()` с temp data_dir → в shutdown_info.json есть `"signal": "SIGTERM"`, `recording_active: true`, `uptime_sec >= 0`, `pid`; (c) shutdown БЕЗ сигнала → `"signal": null`; (d) source-contract: тело `_signal_handler` не содержит вызовов `.is_recording`/`with `/`logger.` (AST/греп по исходнику метода — по образцу существующих source-contract тестов в `test_shutdown_handler_wired_in_main.py`).
- [ ] **Step 2: Verify FAIL**
- [ ] **Step 3: Реализация**
- [ ] **Step 4: Зелёные + регрессия существующих**: `pytest KrabEar/tests/test_shutdown_info_r1_fields.py KrabEar/tests/test_shutdown_handler.py KrabEar/tests/test_shutdown_handler_deep.py -v`
- [ ] **Step 5: ubuntu-parity + коммит**

---

### Task 6: dirty-marker + сбор форензики (`shutdown_forensics.py`)

**Files:**
- Create: `KrabEar/backend/shutdown_forensics.py`
- Modify: `KrabEar/backend/shutdown_handler.py` (удаление маркера в `_persist` — одна строка)
- Modify: `KrabEar/backend/service.py` (в rescue-тред из Task 4 добавить ПЕРВЫМ шагом вызов форензики — один общий тред `startup-recovery`)
- Test: `KrabEar/tests/test_shutdown_forensics.py`

**Interfaces:**
- Consumes: поля Task 5 (читает прошлый shutdown_info.json для контекста отчёта).
- Produces: `write_alive_marker(data_dir) -> None`; `check_and_collect(data_dir, log_dirs: list[Path], timeout_sec: float = 30.0) -> str` (возврат `"clean" | "unclean_collected" | "unclean_collect_failed" | "first_run"`); `_MARKER = "runtime_alive.marker"`.

**Логика:**
1. Маркер: `write_alive_marker` пишет `{"pid": os.getpid(), "started_at_iso": ...}` в `<data_dir>/runtime_alive.marker`. Вызывается на старте (см. п.4). `GracefulShutdownHandler._persist` ПОСЛЕ успешной записи shutdown_info удаляет маркер (`(self._data_dir / "runtime_alive.marker").unlink(missing_ok=True)` в try/except). Смерть без graceful → маркер остаётся → следующий старт видит его = UNCLEAN. Детерминированно, без эвристик по mtime.
2. `check_and_collect`:
   - маркера нет и shutdown_info есть → `"clean"`; ни того ни другого → `"first_run"`;
   - маркер ЕСТЬ → UNCLEAN: прочитать из него `started_at_iso` прошлой жизни; окно смерти = [из маркера, сейчас]; собрать в `<data_dir>/forensics/<YYYYmmdd_HHMMSS>/`:
     - `log_show.txt`: `subprocess.run(["log", "show", "--start", <10 мин назад от "сейчас" — окно смерти неизвестно точнее, свежий хвост ценнее>, "--predicate", 'eventMessage CONTAINS[c] "krab"', "--style", "compact"], timeout=timeout_sec)` — при таймауте/ошибке файл с текстом ошибки;
     - `launchctl_print.txt`: `subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/ai.krab.ear.backend"], ...)`;
     - `own_logs_tail.txt`: последние 300 строк каждого файла из `log_dirs` (существующих `logs/krab-ear-backend.{out,err}.log` — пути передаёт вызыватель, отсутствующие пропускаются);
     - `prev_shutdown_info.json` + `stale_marker.json`: копии для контекста;
   - после сбора маркер удалить; retention: оставить 5 новейших подкаталогов `forensics/`, старшие `shutil.rmtree`;
   - весь метод обёрнут try/except → `"unclean_collect_failed"`, никогда не бросает и не блокирует старт (зовётся из фонового треда).
3. Linux/CI-гард: `log`/`launchctl` отсутствуют → ветка ошибки пишет файл с сообщением, статус всё равно `"unclean_collected"` (собрано, что было; тесты стабят subprocess).
4. `service.py`: тред из Task 4 переименовать в `startup-recovery` и последовательность: `check_and_collect(...)` → `write_alive_marker(...)` → `run_rescue_scan(...)`. (Маркер пишется ПОСЛЕ сбора — иначе свежий маркер затрёт улику прошлой жизни.)

- [ ] **Step 1: Падающие тесты** (subprocess везде застаблен `unittest.mock.patch`):
  - `test_first_run_no_marker_no_info`;
  - `test_clean_shutdown_no_collection` (нет маркера, info есть);
  - `test_unclean_collects_and_removes_marker` (маркер есть → каталог forensics с own_logs_tail.txt из подложенного лог-файла, маркер удалён, subprocess вызван с "log");
  - `test_retention_keeps_5`;
  - `test_collect_failure_never_raises` (subprocess бросает → `"unclean_collect_failed"`);
  - `test_persist_removes_marker` (полный handler.bind+shutdown с temp dir → маркер исчез) — этот кейс живёт в этом же файле, использует реальный GracefulShutdownHandler.
- [ ] **Step 2: Verify FAIL**
- [ ] **Step 3: Реализация**
- [ ] **Step 4: Зелёные + регрессия shutdown-тестов** (те же три файла, что в Task 5)
- [ ] **Step 5: ubuntu-parity + коммит**

---

### Task 7: privacy-purge покрытие `rescue/` + `forensics/`

**Files:**
- Modify: `KrabEar/backend/history_service.py` (`handle_purge_all_data`, ~строка 2104+ — добавить wipe-шаги по идиоме существующих: try/except, счётчик, errors-list; образец — transcripts-шаг там же)
- Test: `KrabEar/tests/test_purge_rescue_forensics_r1.py`

**Interfaces:** Consumes пути из Task 3/6: `<data_dir>/rescue/`, `<data_dir>/forensics/`, `<data_dir>/runtime_alive.marker` (маркер НЕ юзер-данные, но пусть purge чистит и его — ноль вреда).

**Код шага (вставить рядом с transcripts-шагом, повторив его идиому):**

```python
        # --- R1: rescue-аудио и форензика — голос пользователя и хвосты логов ---
        rescue_deleted = 0
        try:
            _rescue = Path(self.store.data_dir) / "rescue"
            if _rescue.is_dir():
                for f in _rescue.iterdir():
                    f.unlink(missing_ok=True)
                    rescue_deleted += 1
        except Exception:
            errors.append("rescue")
            logger.exception("purge: rescue/ не очищен")
        try:
            _forensics = Path(self.store.data_dir) / "forensics"
            if _forensics.is_dir():
                shutil.rmtree(_forensics, ignore_errors=False)
        except Exception:
            errors.append("forensics")
            logger.exception("purge: forensics/ не очищен")
        try:
            (Path(self.store.data_dir) / "runtime_alive.marker").unlink(missing_ok=True)
        except Exception:
            errors.append("runtime_alive_marker")
```

(Точное имя списка ошибок/счётчиков сверить с телом хендлера; `rescue_deleted` добавить в возвращаемый dict. `shutil` — проверить импорт в файле.)

- [ ] **Step 1: Падающий тест**: сеять файлы в rescue/+forensics/+marker в temp store → `handle_purge_all_data({"confirm": True})` → всё исчезло, `rescue_deleted` в ответе. Фикстуры — по образцу существующих purge-тестов (`grep -rln "handle_purge_all_data" KrabEar/tests/ | head -3` — взять свежайший как шаблон).
- [ ] **Step 2: Verify FAIL** → **Step 3: Реализация** → **Step 4: Зелёные + `make audit-purge-coverage`** (guard обязан пройти строго; если guard потребует другое оформление шага — оформить, как он требует)
- [ ] **Step 5: ubuntu-parity + коммит**

---

### Task 8: живой e2e-смок `scripts/e2e_rescue_smoke.py`

**Files:**
- Create: `scripts/e2e_rescue_smoke.py`

**Interfaces:** Consumes весь стек Фазы 1. Паттерн процесса — по образцу `scripts/run_e2e_smokes.command` + `scripts/e2e_ipc_smoke.py` (одноразовый data_dir в /tmp, `python KrabEar/main.py --data-dir <tmp>`, сокет `<tmp>/backend.sock`, teardown через trap/finally).

**Сценарий (весь на throwaway dev-backend, прод не трогается):**
1. Поднять backend с временным data_dir; дождаться ping (сокет-петля с таймаутом 60с).
2. IPC `start_recording` (реальный микрофон машины; тишина — норм, спилл всё равно пишется).
3. Подождать 3с; проверить, что `<data_dir>/rescue/*.f32.part` существует и растёт (два замера размера) — FAIL, если нет.
4. `os.kill(pid, signal.SIGKILL)` — жёсткая смерть посреди записи.
5. Поднять backend НА ТОМ ЖЕ data_dir; дождаться ping.
6. В цикле до 30с ждать: (a) `rescue/*.rescued.wav` появился И исчез (транскрибирован) ИЛИ остался (если STT дал пусто на тишине — допустимо: тогда проверить, что WAV существует); (b) IPC `list_recent_errors {since_seq: 0}` содержит `audio.recording_rescued`.
7. Проверить, что `<data_dir>/forensics/` непуст (SIGKILL = UNCLEAN → сбор второй жизни сработал).
8. Штатно погасить второй процесс SIGTERM; проверить `shutdown_info.json`: `signal == "SIGTERM"`, `recording_active == false`.
9. Печать `ALL GREEN` / диагностика и exit code.

- [ ] **Step 1: Написать скрипт** (это e2e — TDD-цикл не применяется; поведенческая правда уже покрыта unit-тестами Task 1-7)
- [ ] **Step 2: Прогнать живьём**: `python scripts/e2e_rescue_smoke.py` → ALL GREEN. Любой FAIL = реальный баг Фазы 1 → чинить в соответствующей задаче, НЕ ослаблять смок.
- [ ] **Step 3: Коммит** (`git add scripts/e2e_rescue_smoke.py`)

---

### Task 9: финальные гейты + доки

**Files:**
- Modify: `docs/ROADMAP-2026H2.md` (журнальная запись о Фазе 1), `CLAUDE.md` (одна строка в карту модулей backend: `recording_spill.py`, `recording_rescue.py`, `shutdown_forensics.py`)

- [ ] **Step 1: Полные гейты**

```bash
PYTHONPATH=$(pwd)/KrabEar python -m pytest \
  KrabEar/tests/test_recording_spill.py \
  KrabEar/tests/test_recorder_spill_integration.py \
  KrabEar/tests/test_recording_spill_wiring.py \
  KrabEar/tests/test_recording_rescue.py \
  KrabEar/tests/test_shutdown_info_r1_fields.py \
  KrabEar/tests/test_shutdown_forensics.py \
  KrabEar/tests/test_purge_rescue_forensics_r1.py \
  KrabEar/tests/test_recording_core_service.py \
  KrabEar/tests/test_audio_recorder_lifecycle.py \
  KrabEar/tests/test_shutdown_handler.py \
  KrabEar/tests/test_shutdown_handler_deep.py \
  KrabEar/tests/test_shutdown_handler_wired_in_main.py \
  -v -p no:cacheprovider
make audit-all
scripts/pre_merge_py312_check.sh KrabEar/tests/test_recording_spill.py \
  KrabEar/tests/test_recorder_spill_integration.py \
  KrabEar/tests/test_recording_spill_wiring.py KrabEar/tests/test_recording_rescue.py \
  KrabEar/tests/test_shutdown_info_r1_fields.py KrabEar/tests/test_shutdown_forensics.py \
  KrabEar/tests/test_purge_rescue_forensics_r1.py
python scripts/e2e_rescue_smoke.py
```

Expected: всё зелёное. Дополнительно прогнать существующий `scripts/run_e2e_smokes.command` (регрессия 37 методов + privacy).

- [ ] **Step 2: Доки + коммит** — журнал ROADMAP (дата, объём, находки), строки в CLAUDE.md.

---

## Порядок и параллелизм

- Task 1 → 2 → 3 → 4 — строгая цепочка (интерфейсные зависимости).
- Task 5 → 6 — цепочка, может идти ПАРАЛЛЕЛЬНО цепочке 1-4 (не пересекаются по файлам; единственное пересечение — `service.py` в Task 4 и 6 решается тем, что Task 6 правит тот же тред, который создал Task 4 → Task 6 исполнять ПОСЛЕ Task 4 либо тем же воркером).
- Task 7 — после 3 и 6. Task 8 — после всех. Task 9 — финал.
- Рекомендуемое исполнение: два Sonnet-воркера (цепочка A: 1→2→3→4; цепочка B: 5→6), затем один воркер 7→8, координатор — 9 + личный гейт каждой задачи + adversarial-ревью всего диффа перед мержем (конвейер §1 ROADMAP).
