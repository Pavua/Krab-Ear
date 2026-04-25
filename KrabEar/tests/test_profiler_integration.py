"""Integration tests: PerformanceProfiler wiring в STT/translate/LLM path'ы.

Проверяем что profiler singleton собирает span'ы когда реально проходит:
- AudioEngine.transcribe()  -> stt_with_fallback, stt_model_<short_name>
- Translator.translate()    -> translate_<mode>
- LLMRewriter.rewrite()     -> llm_rewrite (span фиксируется до HTTP-вызова)

И что _handle_get_diagnostics включает ключ "profiler" с ожидаемой структурой
(methods/slowest_methods/total_profiled_time_sec).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers — лёгкие фейки (идентичны паттернам из test_backend_service.py)
# ---------------------------------------------------------------------------


class _FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000

    def start(self) -> bool:
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        self.is_recording = False
        return None

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        import numpy as np
        return np.zeros(int(max_duration_sec * 16000), dtype=np.float32), max_duration_sec


class _FakeTranscriber:
    """Минимальный transcriber для BackendService (get_diagnostics читает .engine)."""

    def __init__(self) -> None:
        self.counter = 0

        class _FakeEngine:
            quality_profile = "balanced"
            current_model = "mlx-community/whisper-tiny"

            def _resolve_diarization_device(self):
                return "cpu"

        self.engine = _FakeEngine()

    def transcribe(
        self,
        audio_data,
        quality_profile: str = "balanced",
        cleanup_profile: str = "soft",
        domain: str = "casual",
        extra_vocabulary=None,
        lang_hint=None,
        history_context=None,
        stt_hotwords=None,
    ) -> str:
        self.counter += 1
        return "fake text"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return "preview"


class _FakeTranslator:
    """Мок переводчик — в этом файле не используем (translate() тестируем на
    реальном Translator, чтобы действительно пройти через span)."""

    def translate(self, *args, **kwargs):
        from backend.translator import TranslationResult

        return TranslationResult(
            text="",
            status="not_requested",
            source_lang="",
            target_lang="",
            mode="off",
            engine="fake",
        )


# ---------------------------------------------------------------------------
# Test case
# ---------------------------------------------------------------------------


class ProfilerIntegrationTestCase(unittest.TestCase):
    """Проверяет что profiler.get_profile_report() содержит ожидаемые span'ы
    после прохождения через engine/translator/LLM слои."""

    def setUp(self) -> None:
        # Global singleton — сбрасываем между тестами чтобы не копились span'ы
        # из других тестов / предыдущих инвойсов.
        from backend.performance_profiler import profiler

        profiler.reset()
        self._profiler = profiler

    # ------------------------------------------------------------------
    # STT spans
    # ------------------------------------------------------------------

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_model")
    def test_transcribe_records_stt_spans(self, mock_model, mock_diar):
        """AudioEngine.transcribe() регистрирует stt_with_fallback и stt_model_<name> span'ы."""
        from core.engine import AudioEngine

        # _transcribe_model замокан — реальный MLX не вызывается, но вокруг него
        # выполняются два span'а: stt_with_fallback (внешний) и stt_model_<short> (внутренний).
        mock_model.return_value = {
            "text": "hello world",
            "segments": [{"avg_logprob": -0.2}],
            "engine": "fake",
            "model_used": "fake",
            "language": "en",
        }
        mock_diar.return_value = {
            "enabled": False,
            "speaker_segments": [],
            "annotated_segments": [],
            "speaker_turns": [],
        }

        engine = AudioEngine()
        result = engine.transcribe(audio_data="fake.wav")
        self.assertIsInstance(result, dict)
        self.assertIn("text", result)

        report = self._profiler.get_profile_report()
        method_names = list(report["methods"].keys())

        # Проверяем наличие хотя бы одного stt_* span'а
        stt_spans = [name for name in method_names if name.startswith("stt_")]
        self.assertGreaterEqual(
            len(stt_spans),
            1,
            f"ожидался минимум один stt_* span, получено: {method_names}",
        )

        # И конкретно — должен быть внешний span fallback chain
        self.assertIn("stt_with_fallback", method_names)

        # stt_model_<short_name> — имя формируется из последнего сегмента пути модели
        stt_model_spans = [name for name in method_names if name.startswith("stt_model_")]
        self.assertGreaterEqual(
            len(stt_model_spans),
            1,
            f"ожидался минимум один stt_model_* span, получено: {method_names}",
        )

    # ------------------------------------------------------------------
    # Translate span
    # ------------------------------------------------------------------

    def test_translate_records_translate_span(self):
        """Translator.translate() регистрирует translate_<mode> span даже когда mode=off."""
        from backend.translator import Translator

        translator = Translator()
        # mode=off — быстрый путь, не требует внешней модели
        result = translator.translate(text="hello", mode="off", network_mode="offline_default")
        self.assertEqual(result.mode, "off")

        report = self._profiler.get_profile_report()
        method_names = list(report["methods"].keys())
        self.assertIn("translate_off", method_names)

    # ------------------------------------------------------------------
    # LLM rewrite span
    # ------------------------------------------------------------------

    def test_llm_rewrite_records_llm_rewrite_span_on_empty_input(self):
        """LLMRewriter.rewrite("") регистрирует llm_rewrite span (fast path — до HTTP)."""
        from backend.llm_rewriter import LLMRewriter

        rewriter = LLMRewriter(
            base_url="http://localhost:65535",  # unreachable — не дойдёт до запроса
            api_key="",
            model="fake",
            timeout_sec=0.01,
        )
        result = rewriter.rewrite("")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "empty_input")

        report = self._profiler.get_profile_report()
        self.assertIn("llm_rewrite", report["methods"])

    # ------------------------------------------------------------------
    # get_diagnostics shape
    # ------------------------------------------------------------------

    def test_get_diagnostics_includes_profiler_section(self):
        """_handle_get_diagnostics → result dict содержит ключ 'profiler' с корректной формой."""
        from backend.service import BackendService
        from backend.state_store import StateStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        store = StateStore(Path(tmp.name) / "data")
        service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

        response = service.handle_request(
            {"id": "test1", "method": "get_diagnostics", "params": {}}
        )
        self.assertTrue(response.get("ok"), msg=f"bad response: {response}")

        result = response["result"]
        self.assertIn("profiler", result)

        profiler_section = result["profiler"]
        self.assertIsInstance(profiler_section, dict)
        # Три ключа верхнего уровня из PerformanceProfiler.get_profile_report()
        self.assertIn("methods", profiler_section)
        self.assertIn("slowest_methods", profiler_section)
        self.assertIn("total_profiled_time_sec", profiler_section)
        self.assertIsInstance(profiler_section["methods"], dict)
        self.assertIsInstance(profiler_section["slowest_methods"], list)

    def test_get_diagnostics_profiler_section_reflects_prior_spans(self):
        """После прохождения span'а 'translate_off' — он должен присутствовать в get_diagnostics['profiler']['methods']."""
        from backend.service import BackendService
        from backend.state_store import StateStore
        from backend.translator import Translator

        # Шаг 1: запускаем реальный translate() чтобы зарегистрировать span
        translator = Translator()
        translator.translate(text="test", mode="off", network_mode="offline_default")

        # Шаг 2: поднимаем BackendService и запрашиваем диагностику
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        store = StateStore(Path(tmp.name) / "data")
        service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )

        response = service.handle_request(
            {"id": "t", "method": "get_diagnostics", "params": {}}
        )
        methods = response["result"]["profiler"]["methods"]
        self.assertIn("translate_off", methods)

        # Проверяем что запись имеет ожидаемые ключи статистики
        entry = methods["translate_off"]
        for key in ("calls", "avg_ms", "p50_ms", "p95_ms", "max_ms"):
            self.assertIn(key, entry)
        self.assertGreaterEqual(entry["calls"], 1)


if __name__ == "__main__":
    unittest.main()
