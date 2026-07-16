# C2b «Спикеры-лайт» Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Живые чипы спикеров в meeting-сессии: DIAR_WINDOW-тик диаризует окно последних 90с, сшивает спикеров между окнами по эмбеддингам и отдаёт `speakers` в `get_meeting_live_state` + событие `meeting.speakers_updated`.

**Architecture:** Спека `docs/superpowers/specs/2026-07-10-c2-live-meeting-overlay-design.md` §2.5 **+ обязательный амендмент §2.5a** (эмбеддинги берутся из `DiarizeOutput.speaker_embeddings` одного диар-прогона — НЕ из `SpeakerManager`; дефолт тика 90с; DIAR_WINDOW планируется в `_next_due` за рубильником). Новый узкий хелпер `AudioEngine.diarize_window(path)`; `LiveSpeakerTracker` — сессионный реестр центроидов со сшивкой по cosine; исполнитель `_job_diar_window` в существующем GPU-слоте `MeetingSessionService`.

**Tech Stack:** Python 3.14 (`.venv_krab_ear`), pyannote.audio 4.x (torch 2.11, MPS), numpy, soundfile; unittest.

**Конвенции проекта (ОБЯЗАТЕЛЬНЫ, из CLAUDE.md):**
- Тесты гонять узко, по файлам: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/<file> -v -p no:cacheprovider`. НИКОГДА не запускать всю сьюту вслепую.
- ubuntu-CI (py3.12, БЕЗ mlx/torch/pyannote/soundfile wheels) — реальный гейт: все новые тесты обязаны работать на фейках, тяжёлые импорты только лениво/под guard. Перед мержем: `make pre-merge-check` (или `scripts/pre_merge_py312_check.sh <files>`).
- flake8 CI-командой: `flake8 <files> --max-line-length=150 --extend-ignore=E501`.
- Логирование: `logger.info("сообщение", extra={...})`, без `print()` в проде.
- Комментарии/докстринги — по-русски, в стиле окружающего кода.
- Коммиты с трейлером `Co-Authored-By:` (см. шаги Commit).
- Тесты НЕ инстанцируют `BackendService` (иначе обязателен `close()` в tearDown) — здесь он и не нужен: все тесты уровня `MeetingSessionService`/`AudioEngine`-заглушек.

---

## Карта файлов

| Файл | Что меняется |
|---|---|
| `KrabEar/core/config.py` | +4 ключа `DEFAULT_SETTINGS` (meeting_diar_*, threshold, рубильник) |
| `KrabEar/backend/settings_validator.py` | +3 в `_RANGE_FIELDS`, +1 в `_BOOL_FIELDS` |
| `KrabEar/core/engine.py` | `_diarization_run_lock` (общий для полной диаризации и окна) + `diarize_window()` |
| `KrabEar/backend/meeting_session_service.py` | `LiveSpeakerTracker`, поля сессии, `_job_diar_window`, планирование DIAR_WINDOW, `speakers` в state, kwargs `diarize_window`/`data_dir` |
| `KrabEar/backend/service.py` | проводка двух новых kwargs в конструктор `MeetingSessionService` |
| `scripts/e2e_meeting_smoke.py` | проверка поля `speakers` |
| `scripts/e2e_speakers_smoke.py` | НОВЫЙ живой смок (macOS, вне CI): реальный pyannote на синтетике двух голосов |
| `docs/IPC_API_REFERENCE.md` | `speakers` в `get_meeting_live_state`, событие `meeting.speakers_updated` |
| Тесты | 4 новых файла `*_W_C2b.py` (см. задачи) |

Все задачи строго последовательны (Task 3/4 меняют один файл).

---

### Task 1: Настройки C2b (config + validator)

**Files:**
- Modify: `KrabEar/core/config.py` (~строка 977, после `"meeting_items_language": "ru",`)
- Modify: `KrabEar/backend/settings_validator.py` (`_RANGE_FIELDS` ~строка 80, `_BOOL_FIELDS` ~строка 113)
- Test: `KrabEar/tests/test_meeting_settings_W_C2b.py` (создать)

- [ ] **Step 1: Написать падающий тест**

```python
"""Настройки C2b «спикеры-лайт»: дефолты, клампы, рубильник.

Спека: docs/superpowers/specs/2026-07-10-c2-live-meeting-overlay-design.md §2.8 + §2.5a.
"""
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import DEFAULT_SETTINGS  # noqa: E402
from backend.settings_validator import SettingsValidator  # noqa: E402


class MeetingSpeakerSettingsTests(unittest.TestCase):
    def test_defaults_present(self):
        self.assertEqual(DEFAULT_SETTINGS["meeting_diar_interval_sec"], 90.0)
        self.assertEqual(DEFAULT_SETTINGS["meeting_diar_window_sec"], 90.0)
        self.assertEqual(DEFAULT_SETTINGS["meeting_speaker_match_threshold"], 0.72)
        self.assertIs(DEFAULT_SETTINGS["meeting_live_speakers_enabled"], True)

    def test_range_clamping(self):
        v = SettingsValidator()
        out = v.validate({
            "meeting_diar_interval_sec": 1.0,          # ниже минимума 60
            "meeting_diar_window_sec": 999.0,          # выше максимума 180
            "meeting_speaker_match_threshold": 0.1,    # ниже минимума 0.5
        })
        s = out["settings"] if isinstance(out, dict) and "settings" in out else out
        self.assertEqual(s["meeting_diar_interval_sec"], 60.0)
        self.assertEqual(s["meeting_diar_window_sec"], 180.0)
        self.assertEqual(s["meeting_speaker_match_threshold"], 0.5)

    def test_bool_field_normalized(self):
        v = SettingsValidator()
        out = v.validate({"meeting_live_speakers_enabled": "false"})
        s = out["settings"] if isinstance(out, dict) and "settings" in out else out
        self.assertIn(s["meeting_live_speakers_enabled"], (False, "false"))
        # Главный инвариант: ключ известен валидатору (не отбрасывается).
        self.assertIn("meeting_live_speakers_enabled", s)
```

ВАЖНО: перед написанием посмотри СОСЕДНИЙ тест на этот же механизм —
`KrabEar/tests/test_wake_word_watchdog_settings.py` — и приведи вызовы
`SettingsValidator` к точно той же форме (сигнатура `validate()` и форма
возврата должны браться из него, а не из этого чернового кода).

- [ ] **Step 2: Убедиться, что тест падает**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_meeting_settings_W_C2b.py -v -p no:cacheprovider`
Expected: FAIL — `KeyError: 'meeting_diar_interval_sec'` (ключей нет в DEFAULT_SETTINGS).

- [ ] **Step 3: Реализация**

В `KrabEar/core/config.py` сразу после строки `"meeting_items_language": "ru",` (блок meeting_* в `DEFAULT_SETTINGS`):

```python
    # C2b — спикеры-лайт (спека §2.5 + амендмент §2.5a)
    "meeting_diar_interval_sec": 90.0,        # тик DIAR_WINDOW; §2.5a: 90 = сплошное покрытие
    "meeting_diar_window_sec": 90.0,          # длина диаризуемого окна
    "meeting_speaker_match_threshold": 0.72,  # cosine-порог сшивки спикеров между окнами
    "meeting_live_speakers_enabled": True,    # рубильник C2b; False = байт-в-байт C2a
```

В `KrabEar/backend/settings_validator.py`, в `_RANGE_FIELDS` после строки `"meeting_items_interval_sec": (30.0, 600.0, 60.0, float),`:

```python
    "meeting_diar_interval_sec": (60.0, 600.0, 90.0, float),
    "meeting_diar_window_sec": (30.0, 180.0, 90.0, float),
    "meeting_speaker_match_threshold": (0.5, 0.95, 0.72, float),
```

В `_BOOL_FIELDS` (рядом с `"wake_word_watchdog_enabled": True,`):

```python
    "meeting_live_speakers_enabled": True,
```

- [ ] **Step 4: Тест зелёный**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_meeting_settings_W_C2b.py -v -p no:cacheprovider`
Expected: PASS (3 теста).

- [ ] **Step 5: Регрессия соседей + flake8**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_settings_validator.py KrabEar/tests/test_wake_word_watchdog_settings.py -v -p no:cacheprovider 2>/dev/null || true` (если файлов нет под этими именами — найти через `ls KrabEar/tests/ | grep -i "settings_valid"` и прогнать найденные)
Run: `.venv_krab_ear/bin/flake8 KrabEar/core/config.py KrabEar/backend/settings_validator.py KrabEar/tests/test_meeting_settings_W_C2b.py --max-line-length=150 --extend-ignore=E501`
Expected: PASS / пусто.

- [ ] **Step 6: Commit**

```bash
git add KrabEar/core/config.py KrabEar/backend/settings_validator.py KrabEar/tests/test_meeting_settings_W_C2b.py
git commit -m "feat(meeting): настройки C2b — диар-тик/окно/порог/рубильник спикеров

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `AudioEngine.diarize_window` + общий run-lock диаризации

**Files:**
- Modify: `KrabEar/core/engine.py` (`__init__` ~строка 387; `_run_diarization_impl` ~строка 3278; новый метод после `_run_diarization_impl`, ~строка 3313)
- Test: `KrabEar/tests/test_engine_diarize_window_W_C2b.py` (создать)

**Контекст.** Полная диаризация (`_run_diarization_impl`) сегодня НИКОГДА не бежит одновременно с чем-либо ещё pyannote-шным. C2b добавляет второй вход в тот же pipeline-объект (DIAR_WINDOW-тик). Классовый риск: юзер жмёт стоп записи хоткеем (в обход `meeting_stop`) → phase-C полная диаризация стартует, пока тик ещё в полёте → два конкурентных инференса одного `Pipeline` на MPS. Закрываем классом: один `threading.Lock` вокруг ОБОИХ вызовов pipeline (sibling-gate симметрия — правка обоих мест в одном коммите).

- [ ] **Step 1: Написать падающий тест**

```python
"""AudioEngine.diarize_window (C2b): сегменты + speaker_embeddings из одного прогона.

Pipeline мокается объектом-фейком — тест не требует pyannote/torch (ubuntu-CI safe).
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _Turn:
    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end


class _FakeAnnotation:
    """Мимикрия pyannote.core.Annotation: itertracks + labels."""

    def __init__(self, tracks):
        self._tracks = tracks  # list[(start, end, label)]

    def itertracks(self, yield_label=False):
        for start, end, label in self._tracks:
            yield _Turn(start, end), "_", label

    def labels(self):
        seen = []
        for _, _, label in self._tracks:
            if label not in seen:
                seen.append(label)
        return seen


class _FakeDiarizeOutput:
    """Мимикрия pyannote 4.x DiarizeOutput."""

    def __init__(self, tracks, embeddings):
        self.speaker_diarization = _FakeAnnotation(tracks)
        self.speaker_embeddings = embeddings


class _FakePipeline:
    def __init__(self, output, lock_probe=None):
        self._output = output
        self._lock_probe = lock_probe
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if self._lock_probe is not None:
            self._lock_probe()
        return self._output


def _make_engine():
    # Лёгкая инстанциация: не грузим модели, только объект.
    from core.engine import AudioEngine
    eng = AudioEngine.__new__(AudioEngine)
    eng._diarization_pipeline = None
    eng._diarization_load_error = ""
    eng._diarization_load_lock = threading.RLock()
    eng._diarization_run_lock = threading.Lock()
    return eng


class DiarizeWindowTests(unittest.TestCase):
    def _run(self, tracks, embeddings):
        eng = _make_engine()
        out = _FakeDiarizeOutput(tracks, embeddings)
        eng._diarization_pipeline = _FakePipeline(out)
        return eng.diarize_window("/tmp/win.wav")

    def test_segments_and_embeddings_shape(self):
        tracks = [(0.0, 2.5, "SPEAKER_00"), (2.5, 4.0, "SPEAKER_01"),
                  (4.0, 6.0, "SPEAKER_00")]
        emb = np.stack([np.ones(256, dtype=np.float32),
                        np.full(256, 2.0, dtype=np.float32)])
        result = self._run(tracks, emb)
        self.assertEqual(len(result["segments"]), 3)
        self.assertEqual(result["segments"][0],
                         {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00"})
        self.assertEqual(set(result["speaker_embeddings"]),
                         {"SPEAKER_00", "SPEAKER_01"})
        self.assertEqual(len(result["speaker_embeddings"]["SPEAKER_00"]), 256)

    def test_nan_embedding_row_skipped(self):
        # pyannote отдаёт NaN-строку для спикера без чистых фреймов — не тащим её в сшивку.
        tracks = [(0.0, 1.0, "SPEAKER_00"), (1.0, 2.0, "SPEAKER_01")]
        emb = np.stack([np.ones(256, dtype=np.float32),
                        np.full(256, np.nan, dtype=np.float32)])
        result = self._run(tracks, emb)
        self.assertEqual(set(result["speaker_embeddings"]), {"SPEAKER_00"})
        self.assertEqual(len(result["segments"]), 2)  # сегменты остаются

    def test_no_embeddings_attr(self):
        # Annotation без speaker_embeddings (старый pyannote) — пустой словарь, без падения.
        eng = _make_engine()
        ann = _FakeAnnotation([(0.0, 1.0, "SPEAKER_00")])
        eng._diarization_pipeline = _FakePipeline(ann)
        result = eng.diarize_window("/tmp/win.wav")
        self.assertEqual(result["speaker_embeddings"], {})
        self.assertEqual(len(result["segments"]), 1)

    def test_run_lock_held_during_inference(self):
        eng = _make_engine()
        held = []
        out = _FakeDiarizeOutput([(0.0, 1.0, "SPEAKER_00")],
                                 np.ones((1, 256), dtype=np.float32))
        eng._diarization_pipeline = _FakePipeline(
            out, lock_probe=lambda: held.append(eng._diarization_run_lock.locked()))
        eng.diarize_window("/tmp/win.wav")
        self.assertEqual(held, [True])

    def test_full_diarization_shares_run_lock(self):
        # Sibling-gate: _run_diarization_impl держит ТОТ ЖЕ лок во время инференса.
        eng = _make_engine()
        held = []
        out = _FakeDiarizeOutput([(0.0, 1.0, "SPEAKER_00")],
                                 np.ones((1, 256), dtype=np.float32))
        eng._diarization_pipeline = _FakePipeline(
            out, lock_probe=lambda: held.append(eng._diarization_run_lock.locked()))
        with patch.object(type(eng), "_prepare_audio_for_diarization",
                          lambda self, p: (p, False), create=True):
            eng._run_diarization_impl("/tmp/full.wav")
        self.assertEqual(held, [True])
```

- [ ] **Step 2: Убедиться, что падает правильно**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_engine_diarize_window_W_C2b.py -v -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'AudioEngine' object has no attribute 'diarize_window'` (и lock-тест `_run_diarization_impl` падает на `held == [False]`).

Если сам ИМПОРТ `core.engine` падает на ubuntu-подобном окружении — это существующий паттерн, посмотри как другие engine-тесты гвардят импорт (`grep -l "core.engine" KrabEar/tests/ | head -3`) и повтори их guard.

- [ ] **Step 3: Реализация в engine.py**

(a) В `__init__`, рядом со строкой `self._diarization_load_lock: threading.RLock = threading.RLock()` (~387):

```python
        # C2b: сериализация САМИХ инференсов pyannote (полная диаризация phase C
        # vs DIAR_WINDOW-тик meeting-сессии). Load-lock выше защищает только загрузку.
        self._diarization_run_lock: threading.Lock = threading.Lock()
```

(b) В `_run_diarization_impl` обернуть вызов pipeline (строка `diarization = pipeline(prepared_audio_path)`):

```python
        try:
            with self._diarization_run_lock:
                diarization = pipeline(prepared_audio_path)
```

(остальное тело try/except/finally не трогать).

(c) Новый метод сразу ПОСЛЕ `_run_diarization_impl`:

```python
    def diarize_window(self, audio_path: str) -> dict[str, Any]:
        """Узкий хелпер C2b (спека §2.5a): диаризация КОРОТКОГО окна встречи.

        Один прогон pipeline даёт и сегменты, и центроиды спикеров окна
        (pyannote 4.x: DiarizeOutput.speaker_embeddings, wespeaker 256-dim,
        порядок строк = diarization.labels()). NaN-строки (спикер без чистых
        фреймов) отбрасываются. НЕ трогает _maybe_run_diarization/phase C.

        Returns: {"segments": [{start, end, speaker}], "speaker_embeddings":
        {label: list[float]}} — времена относительны начала окна.
        """
        import gc
        pipeline = self._load_diarization_pipeline()
        try:
            with self._diarization_run_lock:
                out = pipeline(audio_path)
        finally:
            # Паттерн утечки MPS — как в _run_diarization_impl.
            gc.collect()
            if torch is not None and hasattr(torch, "mps") and torch.backends.mps.is_available():
                try:
                    torch.mps.empty_cache()
                except Exception:
                    pass
        diarization = getattr(out, "speaker_diarization", out)
        labels = list(diarization.labels())
        raw_emb = getattr(out, "speaker_embeddings", None)
        embeddings: dict[str, list[float]] = {}
        if raw_emb is not None:
            arr = np.asarray(raw_emb, dtype=np.float32)
            for i, label in enumerate(labels):
                if i < arr.shape[0] and not np.isnan(arr[i]).any():
                    embeddings[str(label)] = arr[i].tolist()
        segments: list[dict[str, Any]] = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append({
                "start": round(float(turn.start), 3),
                "end": round(float(turn.end), 3),
                "speaker": str(speaker),
            })
        return {"segments": segments, "speaker_embeddings": embeddings}
```

Проверь, что `np` уже импортирован на уровне модуля engine.py (`grep -n "^import numpy\|import numpy as np" KrabEar/core/engine.py`) — если вдруг только внутри функций, импортируй лениво внутри метода.

- [ ] **Step 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_engine_diarize_window_W_C2b.py -v -p no:cacheprovider`
Expected: PASS (5 тестов).

- [ ] **Step 5: Регрессия существующих диар-тестов**

Run: `ls KrabEar/tests/ | grep -iE "diariz" ` → прогнать каждый найденный файл отдельно тем же pytest-вызовом.
Expected: PASS (лок — нулевая семантика при последовательных вызовах).

- [ ] **Step 6: flake8 + Commit**

Run: `.venv_krab_ear/bin/flake8 KrabEar/core/engine.py KrabEar/tests/test_engine_diarize_window_W_C2b.py --max-line-length=150 --extend-ignore=E501`

```bash
git add KrabEar/core/engine.py KrabEar/tests/test_engine_diarize_window_W_C2b.py
git commit -m "feat(engine): diarize_window — сегменты+эмбеддинги одним прогоном + общий run-lock диаризации

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: LiveSpeakerTracker (сессионный реестр + cosine-сшивка)

**Files:**
- Modify: `KrabEar/backend/meeting_session_service.py` (новый класс module-level, после констант, ПЕРЕД `class MeetingJob`)
- Test: `KrabEar/tests/test_meeting_speaker_tracker_W_C2b.py` (создать)

- [ ] **Step 1: Написать падающий тест**

```python
"""LiveSpeakerTracker (C2b): сшивка спикеров между окнами на fake-эмбеддингах."""
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.meeting_session_service import LiveSpeakerTracker  # noqa: E402


def _emb(direction: int, dim: int = 8) -> list[float]:
    v = np.zeros(dim, dtype=np.float32)
    v[direction] = 1.0
    return v.tolist()


def _near(direction: int, dim: int = 8) -> list[float]:
    # cosine ~0.995 к _emb(direction) — заведомо выше порога 0.72
    v = np.zeros(dim, dtype=np.float32)
    v[direction] = 1.0
    v[(direction + 1) % dim] = 0.1
    return v.tolist()


class LiveSpeakerTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tr = LiveSpeakerTracker(threshold=0.72)

    def test_first_window_creates_speakers(self):
        self.tr.ingest(
            segments=[{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"},
                      {"start": 2.0, "end": 5.0, "speaker": "SPEAKER_01"}],
            embeddings={"SPEAKER_00": _emb(0), "SPEAKER_01": _emb(1)},
            now_ts=1000.0)
        snap = self.tr.snapshot()
        self.assertEqual([s["label"] for s in snap], ["Спикер 1", "Спикер 2"])
        self.assertAlmostEqual(snap[0]["talk_sec"], 2.0)
        self.assertAlmostEqual(snap[1]["talk_sec"], 3.0)
        self.assertEqual(snap[0]["last_active_ts"], 1000.0)

    def test_second_window_matches_same_speaker(self):
        self.tr.ingest(segments=[{"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00"}],
                       embeddings={"SPEAKER_00": _emb(0)}, now_ts=1000.0)
        # В новом окне локальная метка ДРУГАЯ, но голос тот же (близкий вектор).
        self.tr.ingest(segments=[{"start": 0.0, "end": 4.0, "speaker": "SPEAKER_01"}],
                       embeddings={"SPEAKER_01": _near(0)}, now_ts=1090.0)
        snap = self.tr.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertAlmostEqual(snap[0]["talk_sec"], 6.0)
        self.assertEqual(snap[0]["last_active_ts"], 1090.0)

    def test_below_threshold_creates_new_speaker(self):
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
                       embeddings={"SPEAKER_00": _emb(0)}, now_ts=1.0)
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
                       embeddings={"SPEAKER_00": _emb(1)}, now_ts=2.0)  # ортогонален
        self.assertEqual(len(self.tr.snapshot()), 2)

    def test_centroid_running_mean(self):
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "A"}],
                       embeddings={"A": _emb(0)}, now_ts=1.0)
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "B"}],
                       embeddings={"B": _near(0)}, now_ts=2.0)
        # Центроид сдвинулся: третье окно с _near(0) всё ещё матчится в того же.
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "C"}],
                       embeddings={"C": _near(0)}, now_ts=3.0)
        self.assertEqual(len(self.tr.snapshot()), 1)
        self.assertAlmostEqual(self.tr.snapshot()[0]["talk_sec"], 3.0)

    def test_segment_without_embedding_counts_no_speaker(self):
        # Метка есть в сегментах, но эмбеддинг отброшен (NaN в engine) — спикер не создаётся.
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"},
                                 {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_01"}],
                       embeddings={"SPEAKER_00": _emb(0)}, now_ts=1.0)
        self.assertEqual(len(self.tr.snapshot()), 1)

    def test_zero_norm_embedding_skipped(self):
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "A"}],
                       embeddings={"A": [0.0] * 8}, now_ts=1.0)
        self.assertEqual(self.tr.snapshot(), [])

    def test_snapshot_returns_copies(self):
        self.tr.ingest(segments=[{"start": 0.0, "end": 1.0, "speaker": "A"}],
                       embeddings={"A": _emb(0)}, now_ts=1.0)
        snap = self.tr.snapshot()
        snap[0]["talk_sec"] = 999.0
        self.assertNotEqual(self.tr.snapshot()[0]["talk_sec"], 999.0)
```

- [ ] **Step 2: Убедиться, что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_meeting_speaker_tracker_W_C2b.py -v -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'LiveSpeakerTracker'`.

- [ ] **Step 3: Реализация**

В `KrabEar/backend/meeting_session_service.py`: добавить `import numpy as np` к импортам модуля (numpy уже транзитивно обязателен — recorder отдаёт ndarray) и новый класс после констант, перед `class MeetingJob`:

```python
class LiveSpeakerTracker:
    """Сессионный реестр спикеров C2b (спека §2.5 + §2.5a).

    Локальные метки pyannote внутри окна анонимны и нестабильны между
    прогонами — идентичность спикеров держится ТОЛЬКО на эмбеддингах:
    cosine центроида окна против скользящего среднего центроида спикера.
    Реестр живёт в памяти сессии, на диск не пишется.

    Потокобезопасность НЕ нужна: все вызовы — из одного GPU-слот-треда;
    снапшот для IPC копируется в состояние сессии под её локом.
    """

    def __init__(self, threshold: float) -> None:
        self._threshold = float(threshold)
        # список спикеров: label, centroid (unit-norm np.ndarray), n_windows,
        # talk_sec, last_active_ts
        self._speakers: list[dict[str, Any]] = []

    @staticmethod
    def _unit(vec: Any) -> "np.ndarray | None":
        arr = np.asarray(vec, dtype=np.float32).flatten()
        norm = float(np.linalg.norm(arr))
        if not np.isfinite(norm) or norm < 1e-8:
            return None
        return arr / norm

    def ingest(self, segments: list[dict[str, Any]],
               embeddings: dict[str, Any], now_ts: float) -> None:
        """Одно окно диаризации: сегменты + центроиды локальных меток."""
        talk_by_label: dict[str, float] = {}
        for seg in segments:
            dur = max(0.0, float(seg.get("end", 0.0)) - float(seg.get("start", 0.0)))
            talk_by_label[str(seg.get("speaker"))] = (
                talk_by_label.get(str(seg.get("speaker")), 0.0) + dur)

        for label, raw in embeddings.items():
            emb = self._unit(raw)
            if emb is None:
                continue
            talk = talk_by_label.get(str(label), 0.0)
            best, best_cos = None, -1.0
            for sp in self._speakers:
                cos = float(np.dot(sp["centroid"], emb))
                if cos > best_cos:
                    best, best_cos = sp, cos
            if best is not None and best_cos >= self._threshold:
                n = best["n_windows"]
                merged = self._unit(best["centroid"] * n + emb)
                if merged is not None:
                    best["centroid"] = merged
                best["n_windows"] = n + 1
                best["talk_sec"] += talk
                best["last_active_ts"] = now_ts
            else:
                self._speakers.append({
                    "label": f"Спикер {len(self._speakers) + 1}",
                    "centroid": emb,
                    "n_windows": 1,
                    "talk_sec": talk,
                    "last_active_ts": now_ts,
                })

    def snapshot(self) -> list[dict[str, Any]]:
        """Снимок для get_meeting_live_state / события (без numpy-объектов)."""
        return [{
            "label": sp["label"],
            "talk_sec": round(float(sp["talk_sec"]), 1),
            "last_active_ts": sp["last_active_ts"],
        } for sp in self._speakers]
```

- [ ] **Step 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_meeting_speaker_tracker_W_C2b.py -v -p no:cacheprovider`
Expected: PASS (7 тестов).

- [ ] **Step 5: Регрессия C2a-тестов сервиса**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_meeting_session_service_W_C2a.py -v -p no:cacheprovider`
Expected: PASS (класс добавлен, поведение сервиса не тронуто).

- [ ] **Step 6: flake8 + Commit**

Run: `.venv_krab_ear/bin/flake8 KrabEar/backend/meeting_session_service.py KrabEar/tests/test_meeting_speaker_tracker_W_C2b.py --max-line-length=150 --extend-ignore=E501`

```bash
git add KrabEar/backend/meeting_session_service.py KrabEar/tests/test_meeting_speaker_tracker_W_C2b.py
git commit -m "feat(meeting): LiveSpeakerTracker — сессионная сшивка спикеров по cosine центроидов

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: DIAR_WINDOW — планирование, исполнитель, speakers в state/событиях

**Files:**
- Modify: `KrabEar/backend/meeting_session_service.py` (dataclass `_MeetingSession`; `__init__`; `handle_meeting_start`; `handle_get_meeting_live_state`; `_run_due_job_once`; `_job_interval`; новый `_job_diar_window`)
- Test: `KrabEar/tests/test_meeting_diar_job_W_C2b.py` (создать)

**Контракт (сверься с §2.5a перед началом):**
- Рубильник читается ОДИН раз в `handle_meeting_start` (как `language`) и фиксируется на сессии; выключен → `_next_due` без DIAR_WINDOW, `s.tracker is None` — байт-в-байт C2a.
- Темп-WAV: `<data_dir>/tmp_meeting/diar_<uuid>.wav`, 16 кГц mono float32, удаляется в `finally` каждого тика.
- Партиалы паузятся на время тика (симметрия с `_job_items_llm`).
- Исключение внутри тика → `degraded_diarization=True`, тик пропущен, воркер живёт; успех → `False`.
- Событие `meeting.speakers_updated {speakers: [...]}` — только при успехе.

- [ ] **Step 1: Написать падающий тест**

```python
"""DIAR_WINDOW-тик C2b: планирование за рубильником, исполнитель, state/события."""
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.meeting_session_service import (  # noqa: E402
    MeetingJob,
    MeetingSessionService,
)

# Фейки — копия конвенций test_meeting_session_service_W_C2a.py
# (если там появится общий helper — переиспользуй его, не дублируй).


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


class _FakeRecordingCore:
    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0

    def handle_start_recording(self, params):
        return {"status": "started", "is_recording": True}

    def handle_stop_recording(self, params):
        return {"history_id": "hist-1"}

    def pause_realtime_partials(self):
        self.paused += 1

    def resume_realtime_partials(self):
        self.resumed += 1


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type, payload):
        self.events.append((event_type, payload))


class _FakeWavModule:
    """Подмена soundfile: записывает вызовы, создаёт файл-пустышку."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, int, int]] = []

    def write(self, path, data, samplerate):
        Path(path).write_bytes(b"RIFFfake")
        self.writes.append((str(path), int(getattr(data, "size", 0)), int(samplerate)))


def _diar_result(label="SPEAKER_00", direction=0):
    v = [0.0] * 8
    v[direction] = 1.0
    return {"segments": [{"start": 0.0, "end": 3.0, "speaker": label}],
            "speaker_embeddings": {label: v}}


def _make_service(tmp: str, enabled: bool = True, diarize=None,
                  settings_extra: dict | None = None):
    settings: dict[str, Any] = {
        "meeting_live_speakers_enabled": enabled,
        "meeting_diar_interval_sec": 90.0,
        "meeting_diar_window_sec": 90.0,
        "meeting_speaker_match_threshold": 0.72,
        "llm_brain_lease_enabled": False,
    }
    settings.update(settings_extra or {})
    bus = _FakeBus()
    core = _FakeRecordingCore()
    svc = MeetingSessionService(
        recorder=_FakeRecorder(),
        transcriber=None,
        recording_core=core,
        action_items_extractor=None,
        settings_get=lambda k, d=None: settings.get(k, d),
        event_bus=bus,
        diarize_window=diarize,
        data_dir=Path(tmp),
    )
    return svc, bus, core


class _SfPatchMixin(unittest.TestCase):
    """Обратимая подмена модульной _sf (урок «sys.modules-стаб без снятия»:
    невосстановленный модульный стаб отравляет соседей по CI-чанку)."""

    def setUp(self):
        super().setUp()
        import backend.meeting_session_service as mss
        self.fake_sf = _FakeWavModule()
        orig = mss._sf
        mss._sf = self.fake_sf
        self.addCleanup(lambda: setattr(mss, "_sf", orig))


class DiarSchedulingTests(_SfPatchMixin):
    def test_toggle_on_schedules_diar_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, *_ = _make_service(tmp, enabled=True, diarize=lambda p: _diar_result())
            self.assertTrue(svc.handle_meeting_start({})["ok"])
            try:
                self.assertIn(MeetingJob.DIAR_WINDOW, svc._next_due)
                self.assertIsNotNone(svc._session.tracker)
            finally:
                svc.close()

    def test_toggle_off_is_byte_identical_c2a(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            svc, *_ = _make_service(tmp, enabled=False,
                                    diarize=lambda p: calls.append(p))
            self.assertTrue(svc.handle_meeting_start({})["ok"])
            try:
                jobs = {k for k in svc._next_due if isinstance(k, MeetingJob)}
                self.assertEqual(jobs, {MeetingJob.CHUNK_STT, MeetingJob.ITEMS_LLM})
                self.assertIsNone(svc._session.tracker)
                svc._run_due_job_once(1e9)  # далёкое будущее: диар всё равно не зовётся
                self.assertEqual(calls, [])
            finally:
                svc.close()

    def test_job_interval_reads_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, *_ = _make_service(tmp, settings_extra={"meeting_diar_interval_sec": 61.0})
            self.assertEqual(svc._job_interval(MeetingJob.DIAR_WINDOW), 61.0)


class DiarJobTests(_SfPatchMixin):
    def _started(self, tmp, **kw):
        svc, bus, core = _make_service(tmp, **kw)
        self.assertTrue(svc.handle_meeting_start({})["ok"])
        return svc, bus, core

    def test_tick_produces_speakers_state_and_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, bus, core = self._started(tmp, diarize=lambda p: _diar_result())
            try:
                svc._job_diar_window(svc._session)
                state = svc.handle_get_meeting_live_state({})
                self.assertEqual(len(state["speakers"]), 1)
                self.assertEqual(state["speakers"][0]["label"], "Спикер 1")
                self.assertAlmostEqual(state["speakers"][0]["talk_sec"], 3.0)
                self.assertFalse(state["degraded"]["diarization"])
                names = [e[0] for e in bus.events]
                self.assertIn("meeting.speakers_updated", names)
                self.assertEqual(core.paused, 1)
                self.assertEqual(core.resumed, 1)
                self.assertEqual(len(self.fake_sf.writes), 1)
            finally:
                svc.close()

    def test_temp_wav_removed_even_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            def boom(path):
                raise RuntimeError("pipeline упал")
            svc, bus, core = self._started(tmp, diarize=boom)
            try:
                svc._job_diar_window(svc._session)  # не должен поднять исключение
                state = svc.handle_get_meeting_live_state({})
                self.assertTrue(state["degraded"]["diarization"])
                self.assertEqual(core.resumed, 1)  # resume в finally
                leftovers = list((Path(tmp) / "tmp_meeting").glob("*.wav"))
                self.assertEqual(leftovers, [])
            finally:
                svc.close()

    def test_success_resets_degraded_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, bus, core = self._started(tmp, diarize=lambda p: _diar_result())
            try:
                svc._session.degraded_diarization = True
                svc._job_diar_window(svc._session)
                self.assertFalse(
                    svc.handle_get_meeting_live_state({})["degraded"]["diarization"])
            finally:
                svc.close()

    def test_short_session_skips_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            svc, bus, core = self._started(
                tmp, diarize=lambda p: calls.append(p) or _diar_result())
            try:
                svc._recorder._duration = 3.0  # < минимума 5с
                svc._job_diar_window(svc._session)
                self.assertEqual(calls, [])
                self.assertEqual(self.fake_sf.writes, [])
            finally:
                svc.close()

    def test_no_diarize_callable_marks_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, bus, core = self._started(tmp, diarize=None)
            try:
                svc._job_diar_window(svc._session)
                self.assertTrue(
                    svc.handle_get_meeting_live_state({})["degraded"]["diarization"])
            finally:
                svc.close()

    def test_cross_window_stitching_accumulates(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = [_diar_result("SPEAKER_00", 0), _diar_result("SPEAKER_03", 0)]
            svc, bus, core = self._started(tmp, diarize=lambda p: results.pop(0))
            try:
                svc._job_diar_window(svc._session)
                svc._job_diar_window(svc._session)
                speakers = svc.handle_get_meeting_live_state({})["speakers"]
                self.assertEqual(len(speakers), 1)  # разные метки, один голос
                self.assertAlmostEqual(speakers[0]["talk_sec"], 6.0)
            finally:
                svc.close()
```

- [ ] **Step 2: Убедиться, что падает**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_meeting_diar_job_W_C2b.py -v -p no:cacheprovider`
Expected: FAIL — `TypeError: MeetingSessionService.__init__() got an unexpected keyword argument 'diarize_window'`.

- [ ] **Step 3: Реализация**

Все правки — `KrabEar/backend/meeting_session_service.py`.

(a) Импорты модуля: добавить

```python
import uuid
from pathlib import Path

try:  # ubuntu-CI без libsndfile: деградация, не падение импорта модуля
    import soundfile as _sf  # type: ignore
except Exception:  # pragma: no cover
    _sf = None
```

и константу к остальным:

```python
_DIAR_MIN_AUDIO_SEC = 5.0  # окно короче — эмбеддинги шумные, тик пропускаем
```

(b) `_MeetingSession` — новые поля (после `privacy_stopped`):

```python
    speakers_enabled: bool = False
    tracker: Any = None                       # LiveSpeakerTracker | None
    speakers: list = field(default_factory=list)  # снапшот для IPC/событий
```

(c) `__init__` — два новых kwargs (после `event_bus`), с сохранением:

```python
        diarize_window: Callable[[str], dict[str, Any]] | None = None,
        data_dir: Any = None,
```
```python
        self._diarize_window = diarize_window
        self._data_dir = Path(data_dir) if data_dir is not None else None
```

(d) `handle_meeting_start`: при создании `session = _MeetingSession(...)` добавить чтение рубильника (один раз на сессию, как `language`):

```python
            speakers_enabled = bool(self._settings_get(
                "meeting_live_speakers_enabled", True))
            session = _MeetingSession(
                promoted=promoted,
                language=str(params.get("language", self._settings_get(
                    "meeting_items_language", "ru")) or "ru"),
                cursor_sec=float(self._recorder.get_duration_sec()) if promoted else 0.0,
                speakers_enabled=speakers_enabled,
            )
            if speakers_enabled:
                session.tracker = LiveSpeakerTracker(threshold=float(
                    self._settings_get("meeting_speaker_match_threshold", 0.72)))
```

и в блоке `self._next_due = {...}`:

```python
                self._next_due = {
                    MeetingJob.CHUNK_STT: now + self._chunk_interval(),
                    MeetingJob.ITEMS_LLM: now + self._items_interval(),
                }
                if speakers_enabled:
                    self._next_due[MeetingJob.DIAR_WINDOW] = now + self._diar_interval()
```

(e) `_run_due_job_once` — ветка исполнителя вместо `pass`:

```python
                elif job is MeetingJob.ITEMS_LLM:
                    self._job_items_llm(s)
                else:
                    self._job_diar_window(s)
```

(f) Интервалы — новый `_diar_interval` рядом с `_items_interval` и правка `_job_interval`:

```python
    def _diar_interval(self) -> float:
        return float(self._settings_get("meeting_diar_interval_sec", 90.0))
```
```python
    def _job_interval(self, job: MeetingJob) -> float:
        if job is MeetingJob.CHUNK_STT:
            return self._chunk_interval()
        if job is MeetingJob.ITEMS_LLM:
            return self._items_interval()
        return self._diar_interval()
```

(g) Новый job после `_job_items_llm`:

```python
    def _job_diar_window(self, s: _MeetingSession) -> None:
        """DIAR_WINDOW-тик (C2b, спека §2.5a): окно → WAV → диаризация+эмбеддинги
        одним прогоном → сшивка в сессионный реестр. Исключения гасятся в
        degraded-флаг — воркер и встреча живут дальше."""
        if s.tracker is None:
            return
        if self._diarize_window is None or _sf is None or self._data_dir is None:
            with self._lock:
                s.degraded_diarization = True
            return
        try:
            upto = float(self._recorder.get_duration_sec())
            window = float(self._settings_get("meeting_diar_window_sec", 90.0))
            start = max(0.0, upto - window)
            if upto - start < _DIAR_MIN_AUDIO_SEC:
                return
            audio = self._recorder.snapshot_range(start, upto)
            if getattr(audio, "size", 0) == 0:
                return
            tmp_dir = self._data_dir / "tmp_meeting"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            wav_path = tmp_dir / f"diar_{uuid.uuid4().hex}.wav"
            try:
                _sf.write(str(wav_path), audio,
                          int(getattr(self._recorder, "sample_rate", 16000)))
                self._recording_core.pause_realtime_partials()
                try:
                    result = self._diarize_window(str(wav_path))
                finally:
                    self._recording_core.resume_realtime_partials()
            finally:
                wav_path.unlink(missing_ok=True)
            s.tracker.ingest(
                segments=result.get("segments", []),
                embeddings=result.get("speaker_embeddings", {}),
                now_ts=time.time())
            snap = s.tracker.snapshot()
            with self._lock:
                s.speakers = snap
                s.degraded_diarization = False
                s.last_updated_ts = time.time()
            self._emit("meeting.speakers_updated", {"speakers": list(snap)})
        except Exception:
            logger.warning("meeting: DIAR_WINDOW-тик упал", exc_info=True)
            with self._lock:
                s.degraded_diarization = True
```

(h) `handle_get_meeting_live_state`: заменить `"speakers": [],  # C2b` на

```python
                "speakers": [dict(x) for x in s.speakers],
```

- [ ] **Step 4: Тесты зелёные**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_meeting_diar_job_W_C2b.py -v -p no:cacheprovider`
Expected: PASS (9 тестов).

- [ ] **Step 5: Регрессия ВСЕХ meeting-тестов**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_meeting_session_service_W_C2a.py KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py KrabEar/tests/test_meeting_partial_pause_W_C2a.py KrabEar/tests/test_meeting_recorder_range_W_C2a.py KrabEar/tests/test_meeting_speaker_tracker_W_C2b.py -v -p no:cacheprovider`
Expected: PASS. ВНИМАНИЕ: C2a-тесты создают сервис БЕЗ новых kwargs — оба обязаны иметь default (`None`), иначе это регрессия сигнатуры.

- [ ] **Step 6: flake8 + Commit**

Run: `.venv_krab_ear/bin/flake8 KrabEar/backend/meeting_session_service.py KrabEar/tests/test_meeting_diar_job_W_C2b.py --max-line-length=150 --extend-ignore=E501`

```bash
git add KrabEar/backend/meeting_session_service.py KrabEar/tests/test_meeting_diar_job_W_C2b.py
git commit -m "feat(meeting): DIAR_WINDOW-тик — планирование за рубильником, спикеры в state и событиях

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Проводка в service.py + e2e-смок + доки + живой спикер-смок

**Files:**
- Modify: `KrabEar/backend/service.py` (~строка 1072, конструктор `MeetingSessionService`)
- Modify: `scripts/e2e_meeting_smoke.py` (проверка `speakers`)
- Create: `scripts/e2e_speakers_smoke.py`
- Modify: `docs/IPC_API_REFERENCE.md` (секция meeting)

- [ ] **Step 1: Проводка в service.py**

Заменить вызов конструктора (строка ~1072):

```python
        self._meeting_svc = MeetingSessionService(
            recorder=self.recorder,
            transcriber=self.transcriber,
            recording_core=self._recording_core_svc,
            action_items_extractor=self._action_items_extractor,
            settings_get=self._get_runtime_setting,
            event_bus=event_bus,
            diarize_window=self.transcriber.engine.diarize_window,
            data_dir=self.store.data_dir,
        )
```

Прим.: `self.transcriber.engine` существует всегда (Transcriber создаёт AudioEngine в `__init__`); сам `diarize_window` лениво грузит pipeline только при первом тике — стартовый путь не тяжелеет.

- [ ] **Step 2: Дымовая проверка проводки**

Run: `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py -v -p no:cacheprovider`
Expected: PASS (dispatch-инварианты не тронуты).

- [ ] **Step 3: e2e-смок — поле speakers**

В `scripts/e2e_meeting_smoke.py` найти место, где проверяется ответ `get_meeting_live_state` (grep `get_meeting_live_state`), и добавить рядом с существующими check():

```python
        check("live_state: speakers — список",
              isinstance(res.get("speakers"), list), str(res.get("speakers"))[:120])
```

(содержимое не ассертим: смок идёт на живом микрофоне с тишиной — спикеров может не быть; контракт — само поле).

- [ ] **Step 4: Живой спикер-смок (macOS, вне CI)**

Создать `scripts/e2e_speakers_smoke.py`:

```python
#!/usr/bin/env python3
"""Живой смок C2b (macOS, ручной гейт; НЕ для CI — требует pyannote/torch/say).

Синтетическая «встреча двух голосов» (say -v Milena / -v Yuri) →
AudioEngine.diarize_window на реальном pipeline → LiveSpeakerTracker →
ожидаем РОВНО 2 спикеров после сшивки двух окон.

Запуск из корня репо:
  PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python scripts/e2e_speakers_smoke.py
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "KrabEar"))

MILENA = [
    "Коллеги, начнём встречу. Сегодня обсуждаем план релиза на следующую неделю.",
    "Решение такое: релиз переносим на четверг, тестирование начинаем завтра.",
    "Запиши задачу: подготовить черновик документации до среды.",
]
YURI = [
    "Да, согласен. Ещё нужно решить вопрос с дизайном плавающей панели.",
    "Принято. Я возьму на себя задачу по настройке сервера сборки.",
    "Спасибо всем, хорошая встреча. До связи.",
]


def build_wavs(tmp: Path) -> list[Path]:
    parts = []
    for i, (m, y) in enumerate(zip(MILENA, YURI)):
        for voice, text in (("Milena", m), ("Yuri", y)):
            aiff = tmp / f"{voice}_{i}.aiff"
            subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
            parts.append(aiff)
    lst = tmp / "list.txt"
    wavs = []
    for p in parts:
        wav = p.with_suffix(".wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(p),
                        "-ar", "16000", "-ac", "1", str(wav)], check=True)
        wavs.append(wav)
    lst.write_text("".join(f"file '{w}'\n" for w in wavs))
    full = tmp / "meeting.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-ar", "16000", "-ac", "1", str(full)], check=True)
    half1, half2 = tmp / "w1.wav", tmp / "w2.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(full),
                    "-t", "30", str(half1)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(full),
                    "-ss", "30", str(half2)], check=True)
    return [half1, half2]


def main() -> int:
    from core.engine import AudioEngine
    from backend.meeting_session_service import LiveSpeakerTracker

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        windows = build_wavs(tmp)
        engine = AudioEngine()
        tracker = LiveSpeakerTracker(threshold=0.72)
        for w in windows:
            t0 = time.monotonic()
            result = engine.diarize_window(str(w))
            print(f"{w.name}: {len(result['segments'])} сегм., "
                  f"{len(result['speaker_embeddings'])} эмб., "
                  f"{time.monotonic() - t0:.1f}с")
            tracker.ingest(result["segments"], result["speaker_embeddings"],
                           now_ts=time.time())
        snap = tracker.snapshot()
        print("Спикеры после сшивки:", snap)
        if len(snap) != 2:
            print(f"FAIL: ожидали 2 спикеров, получили {len(snap)}")
            return 1
        print("OK: ровно 2 спикера, сшивка между окнами работает")
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

Прим.: если `AudioEngine()` требует обязательных аргументов — посмотри, как его создаёт `Transcriber.__init__` (`KrabEar/backend/transcriber.py:39`), и повтори минимальную форму.

Run (живой гейт, ~1-2 мин): `PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python scripts/e2e_speakers_smoke.py`
Expected: `OK: ровно 2 спикера...` и время окна в пределах ~2-9с.

- [ ] **Step 5: Доки IPC**

В `docs/IPC_API_REFERENCE.md` найти описание `get_meeting_live_state` (grep). Обновить поле `speakers`: было `[]` / «C2b», стало:

```markdown
- `speakers` — список чипов спикеров (C2b): `[{label: "Спикер N", talk_sec: float,
  last_active_ts: float}]`. Пустой, пока диар-тик не отработал или
  `meeting_live_speakers_enabled=false`. Метки сессионные (без кросс-сессионной
  идентичности); live-данные — черновик, финальный отчёт пересчитывает начисто.
```

Рядом с описаниями событий `meeting.*` добавить:

```markdown
- `meeting.speakers_updated` — `{speakers: [{label, talk_sec, last_active_ts}]}` —
  после каждого успешного DIAR_WINDOW-тика (интервал `meeting_diar_interval_sec`, деф. 90с).
```

- [ ] **Step 6: Commit**

```bash
git add KrabEar/backend/service.py scripts/e2e_meeting_smoke.py scripts/e2e_speakers_smoke.py docs/IPC_API_REFERENCE.md
git commit -m "feat(meeting): проводка diarize_window/data_dir + speakers в e2e-смоках и доках IPC

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Финальные гейты волны

**Files:** без новых правок (только фиксы, если гейты красные).

- [ ] **Step 1: Все C2a+C2b тесты одним прогоном**

Run:
```bash
PYTHONPATH=$(pwd)/KrabEar .venv_krab_ear/bin/python -m pytest \
  KrabEar/tests/test_meeting_settings_W_C2b.py \
  KrabEar/tests/test_engine_diarize_window_W_C2b.py \
  KrabEar/tests/test_meeting_speaker_tracker_W_C2b.py \
  KrabEar/tests/test_meeting_diar_job_W_C2b.py \
  KrabEar/tests/test_meeting_session_service_W_C2a.py \
  KrabEar/tests/test_meeting_dispatch_privacy_W_C2a.py \
  KrabEar/tests/test_meeting_partial_pause_W_C2a.py \
  KrabEar/tests/test_meeting_recorder_range_W_C2a.py \
  -v -p no:cacheprovider
```
Expected: все PASS.

- [ ] **Step 2: flake8 по всем тронутым файлам**

```bash
.venv_krab_ear/bin/flake8 \
  KrabEar/core/config.py KrabEar/backend/settings_validator.py \
  KrabEar/core/engine.py KrabEar/backend/meeting_session_service.py \
  KrabEar/backend/service.py \
  KrabEar/tests/test_meeting_settings_W_C2b.py \
  KrabEar/tests/test_engine_diarize_window_W_C2b.py \
  KrabEar/tests/test_meeting_speaker_tracker_W_C2b.py \
  KrabEar/tests/test_meeting_diar_job_W_C2b.py \
  scripts/e2e_speakers_smoke.py \
  --max-line-length=150 --extend-ignore=E501
```
Expected: пусто.

- [ ] **Step 3: ubuntu-parity (обязателен: новые тесты не должны требовать mlx/torch/soundfile)**

Run: `scripts/pre_merge_py312_check.sh KrabEar/tests/test_meeting_settings_W_C2b.py KrabEar/tests/test_engine_diarize_window_W_C2b.py KrabEar/tests/test_meeting_speaker_tracker_W_C2b.py KrabEar/tests/test_meeting_diar_job_W_C2b.py`
Expected: все файлы PASS. Провал импорта = тесты тянут тяжёлую зависимость — чинить guard'ом, не skip'ом всего файла без разбора.

- [ ] **Step 4: audit-скрипты**

Run: `make audit-all`
Expected: чисто (новых extracted-модулей нет, но гейт обязателен для core/backend-правок).

- [ ] **Step 5: Commit (только если были фиксы)**

```bash
git add -u
git commit -m "test(meeting): фиксы финальных гейтов C2b

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Вне плана (координатор, после мержа)

Живой e2e против throwaway-backend (`scripts/run_e2e_smokes.command` + `e2e_meeting_smoke.py`), `e2e_speakers_smoke.py` на реальном pipeline, деплой (`launchctl kickstart -k` + верификация PID), ROADMAP-журнал, релизная отметка v2.9.x — делает координатор сессии, не воркеры.
