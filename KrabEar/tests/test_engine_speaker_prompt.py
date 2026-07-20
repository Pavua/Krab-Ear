"""Тесты speaker-aware initial_prompt для многоспикерных записей.

Проверяет:
- _build_speaker_context_prompt() — локализованные подсказки для ru/es/en
- Интеграцию speaker hint в transcribe() prompt
- Что существующий initial_prompt не перезаписывается, а дополняется
- Флаг STT_SPEAKER_AWARE_PROMPT_ENABLED отключает функциональность
- Ошибка при оценке спикеров → fallback к отсутствию hint (без краша)
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class BuildSpeakerContextPromptTestCase(unittest.TestCase):
    """Unit-тесты статического хелпера _build_speaker_context_prompt."""

    def _build(self, num_speakers, language):
        from core.engine import AudioEngine
        return AudioEngine._build_speaker_context_prompt(num_speakers, language)

    # --- 1 speaker / None → no hint ---

    def test_one_speaker_returns_empty(self):
        """1 спикер → пустая строка, подсказка не нужна."""
        self.assertEqual(self._build(1, "ru"), "")

    def test_none_speakers_returns_empty(self):
        """None спикеров → пустая строка."""
        self.assertEqual(self._build(None, "ru"), "")

    def test_zero_speakers_returns_empty(self):
        """0 спикеров → пустая строка."""
        self.assertEqual(self._build(0, "ru"), "")

    # --- 2 speakers RU ---

    def test_two_speakers_ru_dialogue(self):
        """2 спикера + ru → подсказка с 'диалога двух'."""
        hint = self._build(2, "ru")
        self.assertIn("диалога двух", hint)

    # --- 3+ speakers RU ---

    def test_three_speakers_ru_multi(self):
        """3 спикера + ru → подсказка 'нескольких участников'."""
        hint = self._build(3, "ru")
        self.assertIn("нескольких участников", hint)

    def test_five_speakers_ru_multi(self):
        """5 спикеров → тоже 'нескольких участников'."""
        hint = self._build(5, "ru")
        self.assertIn("нескольких участников", hint)

    # --- ES ---

    def test_two_speakers_es(self):
        """2 спикера + es → испанская подсказка с 'dos'."""
        hint = self._build(2, "es")
        self.assertIn("dos", hint)

    def test_three_speakers_es(self):
        """3 спикера + es → 'varios participantes'."""
        hint = self._build(3, "es")
        self.assertIn("varios participantes", hint)

    # --- EN / fallback ---

    def test_two_speakers_en(self):
        """2 спикера + en → английская подсказка."""
        hint = self._build(2, "en")
        self.assertIn("dialogue", hint.lower())

    def test_three_speakers_en(self):
        """3 спикера + en → 'multi-speaker'."""
        hint = self._build(3, "en")
        self.assertIn("multi-speaker", hint.lower())

    def test_unknown_language_returns_english(self):
        """Неизвестный язык → English hint."""
        hint = self._build(2, "zh")
        self.assertIn("dialogue", hint.lower())

    def test_none_language_returns_english(self):
        """None язык → English hint."""
        hint = self._build(2, None)
        self.assertIn("dialogue", hint.lower())


class SpeakerPromptIntegrationTestCase(unittest.TestCase):
    """Интеграционные тесты: speaker hint в transcribe() prompt."""

    def _fake_whisper_result(self, text: str = "тест"):
        return {
            "text": text,
            "segments": [{"avg_logprob": -0.2}],
            "engine": "fake-whisper",
            "model_used": "fake",
            "language": "ru",
            "audio_duration_sec": 5.0,
        }

    def _make_engine(self):
        from core.engine import AudioEngine
        return AudioEngine(skip_gigaam_warmup=True)

    # --- Existing prompt preserved + hint appended ---

    @patch("core.engine.AudioEngine._estimate_num_speakers", return_value=2)
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_existing_prompt_preserved_and_hint_appended(
        self, mock_fallback, mock_diar, mock_estimate
    ):
        """Существующий initial_prompt не перезаписывается — hint добавляется через \\n."""
        from core.config import settings
        mock_fallback.return_value = self._fake_whisper_result()
        mock_diar.return_value = {
            "enabled": False, "speaker_segments": [],
            "annotated_segments": [], "speaker_turns": [],
        }
        engine = self._make_engine()
        with patch.object(settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", True), \
             patch.object(settings, "STT_DIALOGUE_HINT_THRESHOLD", 2), \
             patch.object(settings, "DIARIZATION_ENABLED", True):
            engine.transcribe("fake.wav", is_preview=False, lang_hint="ru")

        _, kwargs = mock_fallback.call_args
        prompt = kwargs.get("prompt", "")
        # Должен содержать основной TRANSCRIBE_PROMPT
        self.assertIn(settings.TRANSCRIBE_PROMPT, prompt)
        # И speaker hint
        self.assertIn("диалога двух", prompt)

    # --- Disabled flag → no hint ---

    @patch("core.engine.AudioEngine._estimate_num_speakers", return_value=2)
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_disabled_flag_no_hint(self, mock_fallback, mock_diar, mock_estimate):
        """STT_SPEAKER_AWARE_PROMPT_ENABLED=False → hint не добавляется."""
        from core.config import settings
        mock_fallback.return_value = self._fake_whisper_result()
        mock_diar.return_value = {
            "enabled": False, "speaker_segments": [],
            "annotated_segments": [], "speaker_turns": [],
        }
        engine = self._make_engine()
        with patch.object(settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", False), \
             patch.object(settings, "DIARIZATION_ENABLED", True):
            engine.transcribe("fake.wav", is_preview=False, lang_hint="ru")

        _, kwargs = mock_fallback.call_args
        prompt = kwargs.get("prompt", "")
        self.assertNotIn("диалог", prompt)
        self.assertNotIn("собеседник", prompt)
        # Estimate should NOT have been called (early return)
        mock_estimate.assert_not_called()

    # --- 1 speaker → no hint ---

    @patch("core.engine.AudioEngine._estimate_num_speakers", return_value=1)
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_single_speaker_no_hint(self, mock_fallback, mock_diar, mock_estimate):
        """1 спикер → hint не добавляется в prompt."""
        from core.config import settings
        mock_fallback.return_value = self._fake_whisper_result()
        mock_diar.return_value = {
            "enabled": False, "speaker_segments": [],
            "annotated_segments": [], "speaker_turns": [],
        }
        engine = self._make_engine()
        with patch.object(settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", True), \
             patch.object(settings, "STT_DIALOGUE_HINT_THRESHOLD", 2), \
             patch.object(settings, "DIARIZATION_ENABLED", True):
            engine.transcribe("fake.wav", is_preview=False, lang_hint="ru")

        _, kwargs = mock_fallback.call_args
        prompt = kwargs.get("prompt", "")
        self.assertNotIn("диалог", prompt)

    # --- Estimate failure → no hint, no crash ---

    @patch("core.engine.AudioEngine._estimate_num_speakers", side_effect=RuntimeError("pyannote fail"))
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_estimate_failure_fallback_no_crash(self, mock_fallback, mock_diar, mock_estimate):
        """Ошибка _estimate_num_speakers → transcribe не падает, hint не добавляется."""
        from core.config import settings
        mock_fallback.return_value = self._fake_whisper_result()
        mock_diar.return_value = {
            "enabled": False, "speaker_segments": [],
            "annotated_segments": [], "speaker_turns": [],
        }
        engine = self._make_engine()
        with patch.object(settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", True), \
             patch.object(settings, "STT_DIALOGUE_HINT_THRESHOLD", 2), \
             patch.object(settings, "DIARIZATION_ENABLED", True):
            # Should not raise
            result = engine.transcribe("fake.wav", is_preview=False, lang_hint="ru")

        self.assertIn("text", result)
        _, kwargs = mock_fallback.call_args
        prompt = kwargs.get("prompt", "")
        self.assertNotIn("диалог", prompt)

    # --- preview → no hint ---

    @patch("core.engine.AudioEngine._estimate_num_speakers", return_value=3)
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_preview_no_hint(self, mock_fallback, mock_diar, mock_estimate):
        """is_preview=True → prompt пустой, speaker hint не добавляется."""
        from core.config import settings
        mock_fallback.return_value = self._fake_whisper_result()
        mock_diar.return_value = {
            "enabled": False, "speaker_segments": [],
            "annotated_segments": [], "speaker_turns": [],
        }
        engine = self._make_engine()
        with patch.object(settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", True), \
             patch.object(settings, "DIARIZATION_ENABLED", True):
            engine.transcribe("fake.wav", is_preview=True)

        _, kwargs = mock_fallback.call_args
        prompt = kwargs.get("prompt", "")
        self.assertEqual(prompt, "")
        mock_estimate.assert_not_called()

    # --- threshold: 3 speakers with threshold=2 → hint added ---

    @patch("core.engine.AudioEngine._estimate_num_speakers", return_value=3)
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_three_speakers_es_hint_in_prompt(self, mock_fallback, mock_diar, mock_estimate):
        """3 спикера + es → 'varios participantes' в prompt."""
        from core.config import settings
        mock_fallback.return_value = self._fake_whisper_result()
        mock_diar.return_value = {
            "enabled": False, "speaker_segments": [],
            "annotated_segments": [], "speaker_turns": [],
        }
        engine = self._make_engine()
        with patch.object(settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", True), \
             patch.object(settings, "STT_DIALOGUE_HINT_THRESHOLD", 2), \
             patch.object(settings, "DIARIZATION_ENABLED", True):
            engine.transcribe("fake.wav", is_preview=False, lang_hint="es")

        _, kwargs = mock_fallback.call_args
        prompt = kwargs.get("prompt", "")
        self.assertIn("varios participantes", prompt)

    # --- threshold not reached → no hint ---

    @patch("core.engine.AudioEngine._estimate_num_speakers", return_value=2)
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_threshold_not_reached_no_hint(self, mock_fallback, mock_diar, mock_estimate):
        """2 спикера но threshold=3 → hint не добавляется."""
        from core.config import settings
        mock_fallback.return_value = self._fake_whisper_result()
        mock_diar.return_value = {
            "enabled": False, "speaker_segments": [],
            "annotated_segments": [], "speaker_turns": [],
        }
        engine = self._make_engine()
        with patch.object(settings, "STT_SPEAKER_AWARE_PROMPT_ENABLED", True), \
             patch.object(settings, "STT_DIALOGUE_HINT_THRESHOLD", 3), \
             patch.object(settings, "DIARIZATION_ENABLED", True):
            engine.transcribe("fake.wav", is_preview=False, lang_hint="ru")

        _, kwargs = mock_fallback.call_args
        prompt = kwargs.get("prompt", "")
        self.assertNotIn("диалог", prompt)


class EstimateNumSpeakersCacheTestCase(unittest.TestCase):
    """Тесты кеширования результата _estimate_num_speakers."""

    def test_cache_returns_cached_value(self):
        """Кеш возвращает ранее сохранённое значение без вызова pipeline."""
        from core.engine import AudioEngine
        engine = AudioEngine(skip_gigaam_warmup=True)
        cache: dict = {"_estimated_num_speakers": 3}
        # Даже если бы pipeline вызвался — он бы упал (нет реального аудио).
        # С кешем — должен вернуть 3 без попытки запустить pyannote.
        result = engine._estimate_num_speakers("nonexistent.wav", cache=cache)
        self.assertEqual(result, 3)

    def test_cache_written_on_failure(self):
        """При ошибке pipeline результат None записывается в кеш."""
        from core.engine import AudioEngine
        engine = AudioEngine(skip_gigaam_warmup=True)
        cache: dict = {}
        # Патчим _resolve_audio_path чтобы вернуть путь, и _load_diarization_pipeline чтобы упасть.
        with patch.object(engine, "_resolve_audio_path", return_value="/fake/audio.wav"), \
             patch.object(engine, "_load_diarization_pipeline", side_effect=RuntimeError("no model")):
            result = engine._estimate_num_speakers("/fake/audio.wav", cache=cache)
        self.assertIsNone(result)
        self.assertIn("_estimated_num_speakers", cache)
        self.assertIsNone(cache["_estimated_num_speakers"])

    def test_pipeline_runs_under_shared_diarization_lock(self):
        """Оценка спикеров не пересекается с meeting-диаризацией на MPS."""
        from core.engine import AudioEngine

        engine = AudioEngine(skip_gigaam_warmup=True)
        engine._diarization_run_lock = threading.Lock()
        lock_states: list[bool] = []
        load_lock_states: list[bool] = []
        annotation = SimpleNamespace(
            itertracks=lambda yield_label: [(None, None, "SPEAKER_00")],
        )

        def pipeline(_audio_path):
            lock_states.append(engine._diarization_run_lock.locked())
            return annotation

        def load_pipeline():
            load_lock_states.append(engine._diarization_run_lock.locked())
            return pipeline

        with patch.object(engine, "_resolve_audio_path", return_value="/fake/audio.wav"), \
             patch.object(engine, "_load_diarization_pipeline", side_effect=load_pipeline), \
             patch.object(
                 engine,
                 "_prepare_audio_for_diarization",
                 return_value=("/fake/audio.wav", False),
             ):
            result = engine._estimate_num_speakers("/fake/audio.wav")

        self.assertEqual(result, 1)
        self.assertEqual(lock_states, [True])
        self.assertEqual(load_lock_states, [True])


if __name__ == "__main__":
    unittest.main()
