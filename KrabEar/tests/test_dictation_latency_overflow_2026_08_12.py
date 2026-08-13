"""F1/F2 — задержка финализации диктовки + переполнение аудиобуфера.

Спека: docs/superpowers/specs/2026-08-12-dictation-latency-and-overflow-design.md

F1: гейт диаризации по длительности в RecordingCoreService._stop_recording_phase_c
    (новая настройка diarization_min_duration_sec, дефолт 90.0 — короткая диктовка
    больше не тащит ~1x-realtime прогон диаризации наравне с часовой записью).
    Путь встречи (MeetingSessionService._job_diar_window → self._diarize_window(...))
    этим гейтом НЕ затрагивается — другой метод (engine.diarize_window(), а не
    _maybe_run_diarization()), см. TestMeetingDiarizationPathNotGated ниже.

F2: превью-воркер (_preview_loop) отступает при росте AudioRecorder.overflow_count —
    пропуск транскрибации этой итерации + экспоненциальный бэкофф (кап 8с),
    плавное снятие после 3 чистых итераций подряд (деление пополам до 0.0).
"""

from __future__ import annotations

import ast
import inspect
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService
from backend.settings_validator import _RANGE_FIELDS
from backend.state_store import StateStore
from core.config import DEFAULT_SETTINGS


# ---------------------------------------------------------------------------
# Общие фейки (по образцу test_recording_core_service.py _make_service)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    is_recording = False
    sample_rate = 16000

    def start(self, spill=None) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        audio = (np.sin(2.0 * np.pi * 440.0 * t) * 0.3).astype(np.float32)
        return audio, 1.0

    def snapshot_audio(self, max_duration_sec=12.0):
        return None


class _FakeTranslator:
    def translate(self, text, **kwargs):
        from backend.translator import TranslationResult
        return TranslationResult(
            text=text, status="skipped", source_lang="auto",
            target_lang="ru", mode="auto", engine="fake",
        )


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


class _FakeSettingsSvc:
    """cached_settings() отдаёт заранее заданный словарь (по умолчанию пустой)."""

    def __init__(self, settings: dict | None = None):
        self._settings = dict(settings or {})

    def cached_settings(self):
        return dict(self._settings)

    def invalidate_cache(self):
        pass


class _DefaultTranscriber:
    def transcribe(self, audio, **kwargs):
        return {"text": "hello", "confidence": 0.9, "engine": "fake"}

    def transcribe_preview(self, audio, **kwargs):
        return {"text": "", "confidence": 0.0, "engine": "fake"}


def _make_service(
    tmp_dir, *, recorder=None, transcriber=None, settings_svc=None, extra_kwargs=None
) -> RecordingCoreService:
    """Utility: construct a RecordingCoreService with minimal fakes."""
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.get_words.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    kwargs = dict(
        recorder=recorder or _FakeRecorder(),
        transcriber=transcriber or _DefaultTranscriber(),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=settings_svc or _FakeSettingsSvc(),
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
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    return RecordingCoreService(**kwargs)


# ===========================================================================
# F1 — гейт диаризации по длительности
# ===========================================================================

class _CapturingTranscriber:
    """Захватывает kwargs последнего вызова transcribe() — гейт диаризации
    проверяется через переданный туда diarize=..."""

    def __init__(self):
        self.last_kwargs: dict | None = None

    def transcribe(self, audio, **kwargs):
        self.last_kwargs = kwargs
        return {"text": "привет мир", "confidence": 0.9, "engine": "fake"}

    def transcribe_preview(self, audio, **kwargs):
        return {"text": "", "confidence": 0.0, "engine": "fake"}


def _sr(**overrides) -> dict:
    base = {"quality_profile": "balanced", "cleanup_profile": "soft", "lang_hint": None}
    base.update(overrides)
    return base


class TestDiarizationMinDurationSettingPresence(unittest.TestCase):
    """F1: новая настройка объявлена в DEFAULT_SETTINGS и в валидаторе диапазонов."""

    def test_default_settings_has_diarization_min_duration(self):
        self.assertIn("diarization_min_duration_sec", DEFAULT_SETTINGS)
        self.assertEqual(DEFAULT_SETTINGS["diarization_min_duration_sec"], 90.0)

    def test_range_fields_has_diarization_min_duration(self):
        self.assertIn("diarization_min_duration_sec", _RANGE_FIELDS)
        min_v, max_v, default, coerce = _RANGE_FIELDS["diarization_min_duration_sec"]
        self.assertEqual(min_v, 0.0, "0 обязан быть в допустимом диапазоне — это валидное 'гейт выключен'")
        self.assertEqual(max_v, 3600.0)
        self.assertEqual(default, 90.0)
        self.assertIs(coerce, float)


class TestDiarizationDurationGate(unittest.TestCase):
    """F1: аудио короче diarization_min_duration_sec не диаризуется."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _run(self, *, duration_sec, settings) -> _CapturingTranscriber:
        transcriber = _CapturingTranscriber()
        svc = _make_service(
            self._tmp, transcriber=transcriber,
            settings_svc=_FakeSettingsSvc(settings),
        )
        audio = np.zeros(1600, dtype=np.float32)
        svc._stop_recording_phase_c(audio, duration_sec, _sr())
        return transcriber

    def test_short_audio_with_diarization_enabled_is_not_diarized(self):
        """42с диктовка (живой инцидент) короче дефолтного порога 90с."""
        transcriber = self._run(duration_sec=42.0, settings={"diarization_enabled": True})
        self.assertIsNone(transcriber.last_kwargs["diarize"])

    def test_long_audio_with_diarization_enabled_is_diarized_as_before(self):
        transcriber = self._run(duration_sec=120.0, settings={"diarization_enabled": True})
        self.assertTrue(transcriber.last_kwargs["diarize"])

    def test_audio_exactly_at_threshold_is_diarized(self):
        """Порог — строгое '<', не '<=': длительность РОВНО на пороге не режется."""
        transcriber = self._run(
            duration_sec=90.0,
            settings={"diarization_enabled": True, "diarization_min_duration_sec": 90.0},
        )
        self.assertTrue(transcriber.last_kwargs["diarize"])

    def test_zero_threshold_restores_previous_behavior(self):
        """0 — гейт выключен целиком, даже для секундной записи."""
        transcriber = self._run(
            duration_sec=1.0,
            settings={"diarization_enabled": True, "diarization_min_duration_sec": 0.0},
        )
        self.assertTrue(transcriber.last_kwargs["diarize"])

    def test_negative_threshold_restores_previous_behavior(self):
        transcriber = self._run(
            duration_sec=1.0,
            settings={"diarization_enabled": True, "diarization_min_duration_sec": -5.0},
        )
        self.assertTrue(transcriber.last_kwargs["diarize"])

    def test_diarization_disabled_stays_disabled_regardless_of_duration(self):
        """diarization_enabled=False — гейт даже не должен вмешиваться (diarize=None)."""
        transcriber = self._run(duration_sec=500.0, settings={"diarization_enabled": False})
        self.assertIsNone(transcriber.last_kwargs["diarize"])

    def test_custom_threshold_respected(self):
        transcriber = self._run(
            duration_sec=25.0,
            settings={"diarization_enabled": True, "diarization_min_duration_sec": 30.0},
        )
        self.assertIsNone(transcriber.last_kwargs["diarize"])

    def test_unparseable_threshold_fails_open_and_still_diarizes(self):
        """Fail-open: испорченное значение порога (напр. settings.json, правленный
        руками в обход валидатора) не должно тихо ронять фичу — диаризуем как раньше."""
        transcriber = self._run(
            duration_sec=5.0,
            settings={"diarization_enabled": True, "diarization_min_duration_sec": "не число"},
        )
        self.assertTrue(transcriber.last_kwargs["diarize"])


class TestMeetingDiarizationPathNotGated(unittest.TestCase):
    """F1 регрессия: гейт diarization_min_duration_sec НЕ должен просочиться в
    путь встречи. MeetingSessionService._job_diar_window зовёт
    self._diarize_window(...) напрямую (engine.diarize_window(), НЕ
    _maybe_run_diarization()) — другой метод, гейта там никогда не было и быть
    не должно. Регрессия «гейт уехал во встречи» обязана ронять этот тест."""

    def test_job_diar_window_calls_diarize_window_directly_and_unconditionally(self):
        from backend.meeting_session_service import MeetingSessionService

        source = textwrap.dedent(inspect.getsource(MeetingSessionService._job_diar_window))
        tree = ast.parse(source)
        func_node = tree.body[0]
        self.assertIsInstance(func_node, ast.FunctionDef)

        diarize_calls = [
            n for n in ast.walk(func_node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_diarize_window"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "self"
        ]
        self.assertEqual(
            len(diarize_calls), 1,
            "_job_diar_window должен звать self._diarize_window(...) РОВНО один раз, напрямую",
        )

        gate_refs = [
            n for n in ast.walk(func_node)
            if isinstance(n, ast.Constant) and n.value == "diarization_min_duration_sec"
        ]
        self.assertEqual(
            gate_refs, [],
            "F1 duration-гейт (diarization_min_duration_sec) не должен появляться в пути встречи",
        )


# ===========================================================================
# F2 — превью-воркер отступает при переполнении аудиобуфера
# ===========================================================================

class _CountingStopEvent:
    """Duck-typed threading.Event: считает вызовы wait() и взводит is_set()
    после max_iterations — без реального сна, тест остаётся быстрым и
    детерминированным вместо ожидания настоящих секунд бэкоффа."""

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


class _OverflowPreviewRecorder:
    """Фейковый рекордер для _preview_loop: overflow_count управляется заранее
    заданной последовательностью значений — по одному на КАЖДОЕ обращение
    свойства (включая единственное стартовое чтение до входа в while).

    R3 (2026-08-13): _preview_loop больше не зовёт snapshot_audio() — курсор
    вместо окна, см. get_duration_sec()/snapshot_range() ниже (тот же
    growth-per-call паттерн: длительность растёт на 1.0с за вызов
    get_duration_sec(), что надёжно проходит гейт "хвост < 0.9с").
    """

    is_recording = True
    sample_rate = 16000

    def __init__(self, overflow_sequence: list[int]):
        self._overflow_sequence = list(overflow_sequence)
        self._idx = 0
        self._duration_calls = 0

    @property
    def overflow_count(self) -> int:
        idx = min(self._idx, len(self._overflow_sequence) - 1)
        value = self._overflow_sequence[idx]
        self._idx += 1
        return value

    def get_duration_sec(self) -> float:
        self._duration_calls += 1
        return float(self._duration_calls)

    def snapshot_range(self, from_sec: float, to_sec: float) -> np.ndarray:
        n = max(0, int(round((to_sec - from_sec) * self.sample_rate)))
        return np.ones(n, dtype=np.float32)


class _NoOverflowAttrRecorder:
    """Легаси-фейк БЕЗ overflow_count вовсе — F2 fail-safe: цикл не должен
    падать/включать бэкофф просто из-за отсутствия атрибута у рекордера."""

    is_recording = True
    sample_rate = 16000

    def __init__(self) -> None:
        self._duration_calls = 0

    def get_duration_sec(self) -> float:
        self._duration_calls += 1
        return float(self._duration_calls)

    def snapshot_range(self, from_sec: float, to_sec: float) -> np.ndarray:
        n = max(0, int(round((to_sec - from_sec) * self.sample_rate)))
        return np.ones(n, dtype=np.float32)


class _CountingPreviewTranscriber:
    def __init__(self) -> None:
        self.transcribe_preview_calls = 0

    def transcribe_preview(self, audio_data, **kwargs):
        self.transcribe_preview_calls += 1
        return {"text": "", "confidence": 0.0, "engine": "fake"}

    def transcribe(self, audio, **kwargs):  # pragma: no cover — не используется здесь
        return {"text": "hello", "confidence": 0.9, "engine": "fake"}


class TestPreviewLoopOverflowBackoff(unittest.TestCase):
    """F2: рост AudioRecorder.overflow_count заставляет _preview_loop
    пропустить транскрибацию и отступить, вместо того чтобы продолжать
    конкурировать с потоком захвата аудио за GPU/CPU."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_overflow_growth_skips_transcription_and_escalates_backoff(self):
        # [0 (стартовое чтение до while), 5, 10, 20, 30, 999] — рост на КАЖДОЙ
        # итерации → каждый раз пропуск + удвоение бэкоффа, кап на 8с.
        recorder = _OverflowPreviewRecorder([0, 5, 10, 20, 30, 999])
        transcriber = _CountingPreviewTranscriber()
        svc = _make_service(self._tmp, recorder=recorder, transcriber=transcriber)
        stop_event = _CountingStopEvent(max_iterations=5)

        svc._preview_loop("balanced", stop_event=stop_event)

        self.assertEqual(
            transcriber.transcribe_preview_calls, 0,
            "рост overflow_count на каждой итерации — транскрибация не должна запускаться вовсе",
        )
        self.assertEqual(stop_event.wait_calls, [0.5, 1.0, 2.0, 4.0, 8.0])
        self.assertEqual(svc.preview_overflow_backoff_sec, 8.0)

    def test_overflow_warning_logged_once_per_episode_not_every_iteration(self):
        recorder = _OverflowPreviewRecorder([0, 5, 10, 20, 30, 999])
        transcriber = _CountingPreviewTranscriber()
        svc = _make_service(self._tmp, recorder=recorder, transcriber=transcriber)
        stop_event = _CountingStopEvent(max_iterations=5)

        with self.assertLogs("KrabEar.Backend.RecordingCore", level="WARNING") as cm:
            svc._preview_loop("balanced", stop_event=stop_event)

        overflow_warnings = [line for line in cm.output if "переполнение аудиобуфера" in line]
        self.assertEqual(
            len(overflow_warnings), 1,
            "WARNING логируется один раз на ЭПИЗОД бэкоффа, не на каждую итерацию (иначе шторм в логе)",
        )

    def test_three_clean_iterations_halve_the_backoff(self):
        # [0, 10 (growth→0.5), 10, 10, 10 (3 чистых → halve 0.5→0.25)]
        recorder = _OverflowPreviewRecorder([0, 10, 10, 10, 10])
        transcriber = _CountingPreviewTranscriber()
        svc = _make_service(self._tmp, recorder=recorder, transcriber=transcriber)
        stop_event = _CountingStopEvent(max_iterations=4)

        svc._preview_loop("balanced", stop_event=stop_event)

        self.assertEqual(svc.preview_overflow_backoff_sec, 0.25)
        # 3 чистые итерации всё же дошли до реальной транскрибации (снятие
        # бэкоффа не блокирует превью насовсем, только пока идёт рост).
        self.assertEqual(transcriber.transcribe_preview_calls, 3)

    def test_backoff_fully_clears_after_enough_clean_iterations(self):
        # 1 рост (backoff=0.5) + 12 чистых итераций (4 деления пополам:
        # 0.5→0.25→0.125→0.0625→0.03125<0.05→снято до 0.0).
        overflow_sequence = [0, 10] + [10] * 12
        recorder = _OverflowPreviewRecorder(overflow_sequence)
        transcriber = _CountingPreviewTranscriber()
        svc = _make_service(self._tmp, recorder=recorder, transcriber=transcriber)
        stop_event = _CountingStopEvent(max_iterations=13)

        svc._preview_loop("balanced", stop_event=stop_event)

        self.assertEqual(svc.preview_overflow_backoff_sec, 0.0)

    def test_recorder_without_overflow_count_never_triggers_backoff(self):
        """Fail-safe: легаси-фейки рекордера в тестах без overflow_count не
        ломают цикл — бэкофф просто никогда не включается."""
        recorder = _NoOverflowAttrRecorder()
        transcriber = _CountingPreviewTranscriber()
        svc = _make_service(self._tmp, recorder=recorder, transcriber=transcriber)
        stop_event = _CountingStopEvent(max_iterations=3)

        svc._preview_loop("balanced", stop_event=stop_event)

        self.assertEqual(transcriber.transcribe_preview_calls, 3)
        self.assertEqual(svc.preview_overflow_backoff_sec, 0.0)


if __name__ == "__main__":
    unittest.main()
