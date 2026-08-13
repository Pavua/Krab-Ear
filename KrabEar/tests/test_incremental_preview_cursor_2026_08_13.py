"""R3 — инкрементальное превью диктовки: курсор вместо скользящего окна.

Спека: docs/superpowers/specs/2026-08-13-incremental-preview-design.md

Живой замер владельца (2026-08-13 03:18) показал 39.2с записи → 14 превью-
транскрибаций → 29.2с суммарного времени STT (75% duty cycle параллельно с
захватом аудио) → 4 переполнения буфера PortAudio. Причина — скользящее окно
``snapshot_audio(max_duration_sec=12.0)``: первые 12с речи распознавались
заново ~8 раз. Фикс — курсор: ``_preview_loop`` теперь распознаёт только
НОВЫЙ хвост аудио через ``snapshot_range(cursor_sec, upto)`` (тот же
примитив, что ``MeetingSessionService._job_chunk_stt`` уже использует для
аккумулятора встреч), и фиксирует (``committed_text += text``) только когда
хвост дорос и (кончается тишиной ИЛИ дорос до потолка).

Тесты используют РЕАЛЬНУЮ RMS-проверку тишины из production-кода
(``RecordingCoreService._preview_tail_is_silent``) на синтезированном аудио
(0.3 — «громкая речь», 0.0 — «тишина»), а не подставленный булев флаг —
иначе тест доказывал бы фантазию, а не поведение кода (см. codex: мок обязан
отражать реальное поведение зависимости).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Общие фейки
# ---------------------------------------------------------------------------

class _TimelineRecorder:
    """Фейковый рекордер: длительность идёт по заранее заданной
    последовательности (по одному значению на вызов ``get_duration_sec()``,
    последнее значение повторяется при исчерпании — как
    ``_OverflowPreviewRecorder`` в test_dictation_latency_overflow_2026_08_12.py).

    ``snapshot_range`` синтезирует РЕАЛЬНОЕ аудио по списку интервалов
    тишины ``silent_intervals`` (абсолютные секунды от начала записи) — RMS-
    проверка тишины в ``_preview_loop`` работает на настоящих сэмплах.
    """

    is_recording = True
    sample_rate = 16000
    overflow_count = 0  # константа — F2-бэкофф в этих тестах не участвует

    def __init__(self, duration_sequence, silent_intervals=()):
        self._durations = list(duration_sequence)
        self._idx = 0
        self._silent_intervals = list(silent_intervals)
        self.snapshot_calls: list[tuple[float, float]] = []

    def get_duration_sec(self) -> float:
        idx = min(self._idx, len(self._durations) - 1)
        value = self._durations[idx]
        self._idx += 1
        return value

    def snapshot_range(self, from_sec: float, to_sec: float) -> np.ndarray:
        self.snapshot_calls.append((from_sec, to_sec))
        n = max(0, int(round((to_sec - from_sec) * self.sample_rate)))
        if n == 0:
            return np.array([], dtype=np.float32)
        times = from_sec + np.arange(n) / self.sample_rate
        silent_mask = np.zeros(n, dtype=bool)
        for a, b in self._silent_intervals:
            silent_mask |= (times >= a) & (times < b)
        return np.where(silent_mask, 0.0, 0.3).astype(np.float32)


class _AmplitudeDrivenTranscriber:
    """STT-фейк: непустой текст, если в поданном хвосте ЕСТЬ громкий сигнал
    (реальная речь где-то в диапазоне), пустой — если хвост целиком тихий.
    Текст варьируется по номеру вызова, чтобы не задеть анти-петлевой
    фильтр ``_postprocess_preview_text._looks_like_looping_artifact``."""

    def __init__(self) -> None:
        self.calls: list[int] = []  # audio.size на каждый вызов

    def transcribe_preview(self, audio_data, **kwargs):
        self.calls.append(int(np.asarray(audio_data).size))
        peak = float(np.max(np.abs(audio_data))) if audio_data.size else 0.0
        if peak < 0.05:
            return {"text": "", "confidence": 0.0, "engine": "fake"}
        return {"text": f"слово{len(self.calls)}", "confidence": 0.9, "engine": "fake"}


class _ScriptedTextTranscriber:
    """STT-фейк с заранее заданной последовательностью результатов —
    независимо от содержимого аудио (симулирует, например, transient-отказ
    transcribe_preview из-за занятого mlx_lock: пустой текст ПРИ громком
    хвосте — см. transcriber.py::transcribe_preview)."""

    def __init__(self, results: list[str]) -> None:
        self._results = list(results)
        self.calls = 0

    def transcribe_preview(self, audio_data, **kwargs):
        idx = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return {"text": self._results[idx]}


class _CountingStopEvent:
    """Duck-typed threading.Event: считает вызовы wait() и взводит is_set()
    после max_iterations — без реального сна (по образцу
    test_dictation_latency_overflow_2026_08_12.py)."""

    def __init__(self, max_iterations: int):
        self._set = False
        self._max_iterations = max_iterations
        self.wait_calls: list = []

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout=None) -> bool:
        self.wait_calls.append(timeout)
        if len(self.wait_calls) >= self._max_iterations:
            self._set = True
        return self._set


class _GrowingBufferRecorder:
    """Симулирует реальный AudioRecorder для теста снижения нагрузки (DoD):
    непрерывный поток, часы продвигает duck-typed stop_event.wait() на
    РЕАЛЬНО запрошенный _preview_loop timeout (не фиксированный шаг) — тот
    же принцип, что и в проде: время идёт, пока цикл ждёт своего poll.
    Периодические паузы (``pause_every_sec``/``pause_len_sec``) имитируют
    естественные паузы речи."""

    sample_rate = 16000
    overflow_count = 0

    def __init__(self, total_duration_sec: float, pause_every_sec: float, pause_len_sec: float):
        self.is_recording = True
        self._total = total_duration_sec
        self._current = 0.0
        self._pause_every = pause_every_sec
        self._pause_len = pause_len_sec
        self.snapshot_calls: list[tuple[float, float]] = []

    def advance(self, step_sec: float) -> None:
        self._current = min(self._current + max(0.0, step_sec), self._total)
        if self._current >= self._total:
            self.is_recording = False

    def get_duration_sec(self) -> float:
        return self._current

    def snapshot_range(self, from_sec: float, to_sec: float) -> np.ndarray:
        self.snapshot_calls.append((from_sec, to_sec))
        from_sec = max(0.0, float(from_sec))
        to_sec = min(float(to_sec), self._current)
        n = max(0, int(round((to_sec - from_sec) * self.sample_rate)))
        if n == 0:
            return np.array([], dtype=np.float32)
        times = from_sec + np.arange(n) / self.sample_rate
        silent_mask = (times % self._pause_every) < self._pause_len
        return np.where(silent_mask, 0.0, 0.3).astype(np.float32)


class _ClockAdvancingStopEvent:
    """wait(timeout) продвигает часы ``_GrowingBufferRecorder`` РОВНО на
    timeout — ту же величину, что реальный ``_preview_loop`` решил ждать.
    Останавливается, когда рекордер сигналит конец записи, либо по
    избыточному max_iterations (защита от бесконечного цикла в тесте)."""

    def __init__(self, recorder: _GrowingBufferRecorder, max_iterations: int = 5000):
        self._recorder = recorder
        self._max_iterations = max_iterations
        self._iterations = 0

    def is_set(self) -> bool:
        return not self._recorder.is_recording

    def wait(self, timeout=None) -> bool:
        self._iterations += 1
        self._recorder.advance(float(timeout or 0.0))
        if self._iterations >= self._max_iterations:
            self._recorder.is_recording = False
        return self.is_set()


class _FakeSettingsSvc:
    def cached_settings(self) -> dict:
        return {}

    def invalidate_cache(self) -> None:
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


class _FakeTranslator:
    def translate(self, text, **kwargs):
        from backend.translator import TranslationResult
        return TranslationResult(
            text=text, status="skipped", source_lang="auto",
            target_lang="ru", mode="auto", engine="fake",
        )


def _make_service(tmp_dir, recorder, transcriber) -> RecordingCoreService:
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.get_words.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    return RecordingCoreService(
        recorder=recorder,
        transcriber=transcriber,
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=_FakeSettingsSvc(),
        llm_rewriter=None,
        auto_glossary=None,
        semantic_searcher=_FakeSemanticSearcher(),
        context_memory=None,
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=session_tracker,
        action_items_extractor=None,
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )


# ===========================================================================
# Непересекающиеся диапазоны + конкатенация (DoD п.1, п.6)
# ===========================================================================

class TestPreviewLoopNonOverlappingRangesAndConcatenation(unittest.TestCase):
    """Один прогон с двумя фиксациями (первая — непустой текст с тихим
    хвостом, вторая — форс-фиксация тихим хвостом на дальней паузе):
    проверяет НЕПЕРЕСЕЧЕНИЕ диапазонов snapshot_range по всей
    последовательности и итоговую конкатенацию committed_text + хвост."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        # Речь 0-3.0с, тишина [3.0,3.7) → 1я фиксация на 3.5с. Речь дальше,
        # тишина [7.4,8.1) → 2я фиксация на 8.0с. Хвост после — не фиксирован.
        self.duration_sequence = [round(i * 0.5, 2) for i in range(1, 19)]
        self.silent_intervals = [(3.0, 3.7), (7.4, 8.1)]
        self.recorder = _TimelineRecorder(self.duration_sequence, self.silent_intervals)
        self.transcriber = _AmplitudeDrivenTranscriber()
        self.svc = _make_service(self._tmp, self.recorder, self.transcriber)
        stop_event = _CountingStopEvent(max_iterations=len(self.duration_sequence))
        self.svc._preview_loop("balanced", stop_event=stop_event)

    def test_ranges_never_overlap_or_repeat_committed_audio(self) -> None:
        calls = self.recorder.snapshot_calls
        self.assertGreaterEqual(len(calls), 3, "сценарий должен дать несколько итераций")
        for i in range(1, len(calls)):
            prev_from, prev_to = calls[i - 1]
            cur_from, cur_to = calls[i]
            self.assertIn(
                cur_from, (prev_from, prev_to),
                f"диапазон #{i} {calls[i]} пересекается или повторяет уже "
                f"зафиксированное аудио предыдущего вызова {calls[i - 1]}",
            )

    def test_two_commits_actually_happened_at_expected_cursor_points(self) -> None:
        # Курсор виден по смене from_sec в последовательности вызовов.
        cursors = sorted({from_sec for from_sec, _ in self.recorder.snapshot_calls})
        self.assertEqual(cursors, [0.0, 3.5, 8.0])

    def test_preview_text_equals_committed_plus_tail(self) -> None:
        # committed_text после двух фиксаций — текст 6-го вызова (граница
        # первой фиксации) + текст 14-го вызова (граница второй фиксации).
        # Финальный хвост (после cursor=8.0, ещё не зафиксирован) — текст
        # последнего (15-го) вызова.
        self.assertEqual(len(self.transcriber.calls), 15)
        committed_prefix = "слово6" + "слово14"
        tail_text = "слово15"
        self.assertEqual(self.svc.preview_text, committed_prefix + tail_text)


# ===========================================================================
# Тихий хвост фиксируется, committed_text НЕ меняется (DoD)
# ===========================================================================

class TestPreviewLoopSilentTailCommitsCursorWithoutAddingText(unittest.TestCase):
    def test_silent_tail_advances_cursor_but_leaves_committed_text_untouched(self) -> None:
        tmp = tempfile.mkdtemp()
        # Речь 0-3.0с → фиксация #1 на 3.5с (тихий хвост [3.0,3.7)) с текстом.
        # Затем ДОЛГАЯ пауза [3.0, 8.0) — курсор растёт до 3.5, потом хвост
        # от 3.5 тоже целиком тихий: до 6.5с (tail=3.0>=COMMIT_MIN) — вторая
        # фиксация БЕЗ текста (курсор идёт вперёд, committed_text не растёт).
        duration_sequence = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5]
        silent_intervals = [(3.0, 8.0)]
        recorder = _TimelineRecorder(duration_sequence, silent_intervals)
        transcriber = _AmplitudeDrivenTranscriber()
        svc = _make_service(tmp, recorder, transcriber)
        stop_event = _CountingStopEvent(max_iterations=len(duration_sequence))

        svc._preview_loop("balanced", stop_event=stop_event)

        # Первая фиксация дала текст 6-го вызова ("слово6"); все последующие
        # вызовы (хвост целиком в тишине) вернули пустой текст — 2я фиксация
        # (тихая) не должна был добавить ничего к committed_text.
        self.assertEqual(svc.preview_text, "слово6")
        # Курсор реально продвинулся ко второй фиксации: последний вызов
        # начинается с 3.5 (границы первой фиксации), а не с 0.0.
        self.assertEqual(recorder.snapshot_calls[-1][0], 3.5)

    def test_silent_tail_below_commit_min_sec_does_not_move_cursor(self) -> None:
        """Короткая тишина (< COMMIT_MIN_SEC) — короче обычной паузы внутри
        фразы — НЕ должна дробить курсор раньше времени."""
        tmp = tempfile.mkdtemp()
        duration_sequence = [1.0]  # tail=1.0 < COMMIT_MIN_SEC(3.0)
        silent_intervals = [(0.0, 1.0)]  # весь хвост тихий
        recorder = _TimelineRecorder(duration_sequence, silent_intervals)
        transcriber = _AmplitudeDrivenTranscriber()
        svc = _make_service(tmp, recorder, transcriber)
        stop_event = _CountingStopEvent(max_iterations=1)

        svc._preview_loop("balanced", stop_event=stop_event)

        self.assertEqual(recorder.snapshot_calls, [(0.0, 1.0)])
        self.assertEqual(svc.preview_text, "")


class _MlxBusyTranscriber:
    """STT-фейк: ВСЕГДА возвращает маркер промаха bounded mlx_lock.

    Реальный `Transcriber.transcribe_preview` отдаёт ровно такой словарь
    (`transcriber.py:152`), когда не смог захватить лок за отведённый бюджет.
    Пустой текст здесь НИЧЕГО не говорит о содержимом хвоста."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def transcribe_preview(self, audio_data, **kwargs):
        self.calls.append(int(np.asarray(audio_data).size))
        return {"text": "", "skipped": "mlx_busy"}


class TestPreviewLoopMlxBusyNeverCommitsEvenOnSilentTail(unittest.TestCase):
    """Гейт-находка 2026-08-13: пустой текст имеет ДВА источника — реальная
    тишина и промах mlx_lock. Совпадение «промах + последние 0.4с тихие»
    двигало бы курсор через речь, прозвучавшую раньше в том же хвосте."""

    def test_mlx_busy_marker_blocks_cursor_advance_on_silent_tail(self) -> None:
        tmp = tempfile.mkdtemp()
        # Хвост 3.5с: речь в начале, пауза в конце — последние 0.4с тихие,
        # то есть без учёта маркера ветка tail_silent зафиксировала бы курсор.
        duration_sequence = [3.5, 7.0]
        silent_intervals = [(3.1, 3.5), (6.6, 7.0)]
        recorder = _TimelineRecorder(duration_sequence, silent_intervals)
        transcriber = _MlxBusyTranscriber()
        svc = _make_service(tmp, recorder, transcriber)
        stop_event = _CountingStopEvent(max_iterations=len(duration_sequence))

        svc._preview_loop("balanced", stop_event=stop_event)

        # Курсор НЕ двинулся: оба снимка начинаются с 0.0.
        self.assertEqual([c[0] for c in recorder.snapshot_calls], [0.0, 0.0])
        # Отображение не тронуто — отката показанного текста не случилось.
        self.assertEqual(svc.preview_text, "")


# ===========================================================================
# Пустой текст БЕЗ тишины — не фиксируем, отображение не трогаем (DoD, §3)
# ===========================================================================

class TestPreviewLoopEmptyTextWithoutSilenceNeverCommitsOrLosesDisplay(unittest.TestCase):
    def test_transient_empty_result_on_loud_tail_preserves_prior_display(self) -> None:
        """Имитирует transcribe_preview, вернувший пустой текст из-за
        занятого bounded mlx_lock (см. transcriber.py) — хвост ГРОМКИЙ (не
        тишина), значит мог содержать реальную речь: фиксировать нельзя,
        а отображение НЕ должно откатиться к committed_text (потеря того,
        что уже было показано пользователю)."""
        tmp = tempfile.mkdtemp()
        recorder = _TimelineRecorder([4.0, 4.9], silent_intervals=())  # никогда не тихо
        transcriber = _ScriptedTextTranscriber(["тест", ""])
        svc = _make_service(tmp, recorder, transcriber)
        stop_event = _CountingStopEvent(max_iterations=2)

        svc._preview_loop("balanced", stop_event=stop_event)

        # Курсор НЕ сдвинулся: оба вызова начинаются с 0.0.
        self.assertEqual(recorder.snapshot_calls, [(0.0, 4.0), (0.0, 4.9)])
        # Отображение осталось тем, что было показано на первой итерации —
        # НЕ пустое и НЕ откатилось к committed_text ("").
        self.assertEqual(svc.preview_text, "тест")


# ===========================================================================
# Форс-фиксация при tail_sec >= MAX_TAIL_SEC без тишины (DoD)
# ===========================================================================

class TestPreviewLoopForceCommitsAtMaxTailWithoutSilence(unittest.TestCase):
    def test_continuous_non_silent_speech_force_commits_at_eight_seconds(self) -> None:
        tmp = tempfile.mkdtemp()
        duration_sequence = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 8.5]
        recorder = _TimelineRecorder(duration_sequence, silent_intervals=())  # без пауз
        transcriber = _AmplitudeDrivenTranscriber()
        svc = _make_service(tmp, recorder, transcriber)
        stop_event = _CountingStopEvent(max_iterations=len(duration_sequence))

        svc._preview_loop("balanced", stop_event=stop_event)

        # 8 вызовов на диапазоне (0.0, upto) для upto=1..8 — форс-фиксация
        # СРАБОТАЛА ровно на 8.0с (MAX_TAIL_SEC), без единого события тишины.
        self.assertEqual(
            recorder.snapshot_calls,
            [(0.0, 1.0), (0.0, 2.0), (0.0, 3.0), (0.0, 4.0),
             (0.0, 5.0), (0.0, 6.0), (0.0, 7.0), (0.0, 8.0)],
        )
        # После фиксации committed_text == текст последнего (8го) вызова;
        # 9й duration (8.5) даёт tail=0.5 < MIN_TAIL_SEC — ещё не звали STT.
        self.assertEqual(svc.preview_text, "слово8")

    def test_force_commit_requires_commit_min_sec_even_without_silence(self) -> None:
        """tail < MAX_TAIL_SEC (и < COMMIT_MIN_SEC) без тишины — фиксации
        не будет вовсе, даже если бы MAX-условие было проверено раньше."""
        tmp = tempfile.mkdtemp()
        recorder = _TimelineRecorder([2.0], silent_intervals=())
        transcriber = _AmplitudeDrivenTranscriber()
        svc = _make_service(tmp, recorder, transcriber)
        stop_event = _CountingStopEvent(max_iterations=1)

        svc._preview_loop("balanced", stop_event=stop_event)

        self.assertEqual(recorder.snapshot_calls, [(0.0, 2.0)])
        self.assertEqual(svc.preview_text, "слово1")


# ===========================================================================
# Сброс состояния на новую запись (DoD)
# ===========================================================================

class TestPreviewLoopResetsStateOnEveryLoopEntry(unittest.TestCase):
    def test_second_recording_starts_cursor_from_zero_without_leaking_prior_text(self) -> None:
        tmp = tempfile.mkdtemp()
        transcriber = _AmplitudeDrivenTranscriber()
        recorder_run1 = _TimelineRecorder([5.0], silent_intervals=())
        svc = _make_service(tmp, recorder_run1, transcriber)
        stop_event1 = _CountingStopEvent(max_iterations=1)
        svc._preview_loop("balanced", stop_event=stop_event1)
        self.assertEqual(recorder_run1.snapshot_calls, [(0.0, 5.0)])
        self.assertEqual(svc.preview_text, "слово1")

        # Новая запись (новый поток на _preview_loop в реальном коде) — тот
        # же паттерн, что self._preview_overflow_backoff_sec: свежий вход.
        recorder_run2 = _TimelineRecorder([2.0], silent_intervals=())
        svc.recorder = recorder_run2
        stop_event2 = _CountingStopEvent(max_iterations=1)
        svc._preview_loop("balanced", stop_event=stop_event2)

        # Курсор новой записи начинается с 0.0 (не продолжает committed
        # хвост первой записи), и текст НЕ содержит следов первой записи.
        self.assertEqual(recorder_run2.snapshot_calls, [(0.0, 2.0)])
        self.assertEqual(svc.preview_text, "слово2")
        self.assertNotIn("слово1", svc.preview_text)


# ===========================================================================
# pause_realtime_partials()/resume_realtime_partials() не трогают курсор
# ===========================================================================

class TestPauseRealtimePartialsDoesNotDisturbPreviewCursor(unittest.TestCase):
    """C2a: pause_realtime_partials()/resume_realtime_partials() управляют
    ТОЛЬКО RealtimePartialTranscriber (self._rt_partial) — состояние курсора
    _preview_loop им физически недоступно (локальные переменные функции).
    Регрессионный тест фиксирует это архитектурное разделение: вызов
    пауз(ы)/резюме между итерациями превью не должен как-либо повлиять на
    непересекающуюся последовательность snapshot_range."""

    def test_pause_and_resume_between_iterations_do_not_reset_cursor(self) -> None:
        tmp = tempfile.mkdtemp()
        duration_sequence = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
        silent_intervals = [(3.0, 3.7)]
        recorder = _TimelineRecorder(duration_sequence, silent_intervals)
        transcriber = _AmplitudeDrivenTranscriber()
        svc = _make_service(tmp, recorder, transcriber)

        class _PausingStopEvent(_CountingStopEvent):
            """Каждый wait() дополнительно дёргает pause/resume — имитирует
            встречу, зовущую эти методы во время активного превью."""

            def __init__(self, target_svc, max_iterations):
                super().__init__(max_iterations)
                self._svc = target_svc

            def wait(self, timeout=None):
                self._svc.pause_realtime_partials()
                self._svc.resume_realtime_partials()
                return super().wait(timeout)

        stop_event = _PausingStopEvent(svc, max_iterations=len(duration_sequence))
        # pause/resume — no-op без _rt_partial (None по умолчанию), не должны бросать.
        svc._preview_loop("balanced", stop_event=stop_event)

        calls = recorder.snapshot_calls
        self.assertGreaterEqual(len(calls), 3)
        for i in range(1, len(calls)):
            prev_from, prev_to = calls[i - 1]
            cur_from, cur_to = calls[i]
            self.assertIn(
                cur_from, (prev_from, prev_to),
                "pause/resume между итерациями не должны провоцировать "
                "пересечение или повтор диапазонов курсора",
            )
        # Фиксация всё же произошла (курсор реально продвинулся) — pause/resume
        # не заблокировали и не сбросили механизм фиксации.
        self.assertTrue(any(f == 3.5 for f, _ in calls), "фиксация на 3.5с не произошла")


# ===========================================================================
# RMS-проверка тишины хвоста — юнит на сам хелпер
# ===========================================================================

class TestPreviewTailIsSilentHelper(unittest.TestCase):
    def test_loud_tail_is_not_silent(self) -> None:
        audio = np.full(16000, 0.3, dtype=np.float32)
        self.assertFalse(RecordingCoreService._preview_tail_is_silent(audio, 16000))

    def test_fully_silent_tail_is_silent(self) -> None:
        audio = np.zeros(16000, dtype=np.float32)
        self.assertTrue(RecordingCoreService._preview_tail_is_silent(audio, 16000))

    def test_only_trailing_window_matters(self) -> None:
        # Первые 0.6с громкие, последние 0.4с тихие — хвост «кончается тишиной».
        loud = np.full(int(16000 * 0.6), 0.3, dtype=np.float32)
        silent = np.zeros(int(16000 * 0.4), dtype=np.float32)
        audio = np.concatenate([loud, silent])
        self.assertTrue(RecordingCoreService._preview_tail_is_silent(audio, 16000))

    def test_trailing_loud_after_leading_silence_is_not_silent(self) -> None:
        silent = np.zeros(int(16000 * 0.6), dtype=np.float32)
        loud = np.full(int(16000 * 0.4), 0.3, dtype=np.float32)
        audio = np.concatenate([silent, loud])
        self.assertFalse(RecordingCoreService._preview_tail_is_silent(audio, 16000))

    def test_empty_audio_is_not_silent_fail_safe(self) -> None:
        """Пустой массив — не наш случай тишины (её нельзя измерить); вызывающий
        код (_preview_loop) уже отфильтровывает пустые снапшоты раньше."""
        audio = np.array([], dtype=np.float32)
        self.assertFalse(RecordingCoreService._preview_tail_is_silent(audio, 16000))


# ===========================================================================
# DoD: суммарное аудио, поданное в transcribe_preview, падает >= 3х (§4)
# ===========================================================================

class TestPreviewLoopAudioVolumeReductionVsSlidingWindow(unittest.TestCase):
    """Симулирует ~39с записи (тот же порядок, что живой инцидент 2026-08-13)
    с естественными паузами речи каждые ~4с. Сравнивает суммарный
    audio.size, реально поданный в transcribe_preview НОВЫМ курсорным
    _preview_loop, против того, что подала бы СТАРАЯ формула скользящего
    окна (min(upto, 12.0)*sample_rate) в ТЕ ЖЕ моменты принятия решения
    (те же вызовы snapshot_range/transcribe_preview, реально случившиеся) —
    честное сравнение «на этих же точках раньше кормили X, теперь кормим Y».
    """

    def test_total_audio_fed_to_stt_drops_at_least_3x(self) -> None:
        tmp = tempfile.mkdtemp()
        total_duration_sec = 39.2
        # Естественная пауза ~0.8с каждые 4с речи — типичный ритм диктовки.
        recorder = _GrowingBufferRecorder(
            total_duration_sec, pause_every_sec=4.0, pause_len_sec=0.8,
        )
        transcriber = _AmplitudeDrivenTranscriber()
        svc = _make_service(tmp, recorder, transcriber)
        stop_event = _ClockAdvancingStopEvent(recorder, max_iterations=5000)

        svc._preview_loop("balanced", stop_event=stop_event)

        self.assertGreater(len(transcriber.calls), 0, "хотя бы одна транскрибация должна была случиться")

        new_total_samples = sum(transcriber.calls)
        old_total_samples = sum(
            round(min(to_sec, 12.0) * recorder.sample_rate)
            for (_, to_sec) in recorder.snapshot_calls
        )

        self.assertGreater(old_total_samples, 0)
        ratio = old_total_samples / new_total_samples
        self.assertGreaterEqual(
            ratio, 3.0,
            f"новый курсорный _preview_loop подал {new_total_samples} семплов "
            f"против {old_total_samples} у скользящего окна — падение всего "
            f"в {ratio:.2f}х, а не минимум в 3х (DoD §4)",
        )


if __name__ == "__main__":
    unittest.main()
