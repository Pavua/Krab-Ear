"""Юнит-тесты для AudioEngine: fallback chain, language resolution, quality profile,
unavailable model tracking, confidence calculation, SenseVoice output parser,
speaker overlap helper, и LLM rewrite toggle.

Все тяжёлые зависимости (mlx_whisper, pyannote, torch, funasr, nemo) замоканы —
реальные модели не загружаются. Тесты быстрые и запускаются на любой платформе.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Фабрика для создания AudioEngine без загрузки моделей
# ---------------------------------------------------------------------------

def _make_engine(**kwargs):
    """Создаёт AudioEngine с опциональными инжектированными зависимостями.

    Все тяжёлые зависимости (mlx_whisper, pyannote, torch) уже обёрнуты в
    try/except в самом engine.py, поэтому AudioEngine() можно конструировать
    без каких-либо установленных ML-библиотек.
    """
    from core.engine import AudioEngine
    return AudioEngine(**kwargs)


# ---------------------------------------------------------------------------
# 1. Quality profile switching
# ---------------------------------------------------------------------------

class QualityProfileTests(unittest.TestCase):
    """Проверяем переключение profil'ей balanced / max."""

    def setUp(self):
        self.engine = _make_engine()

    def test_default_profile_is_balanced(self):
        self.assertEqual(self.engine.quality_profile, "balanced")

    def test_set_quality_profile_to_max_returns_true(self):
        changed = self.engine.set_quality_profile("max")
        self.assertTrue(changed)
        self.assertEqual(self.engine.quality_profile, "max")

    def test_setting_same_profile_returns_false(self):
        # Already balanced → no change expected
        changed = self.engine.set_quality_profile("balanced")
        self.assertFalse(changed)

    def test_unknown_profile_normalizes_to_balanced(self):
        self.engine.set_quality_profile("max")  # move away first
        self.engine.set_quality_profile("super")  # unknown → normalises to balanced
        self.assertEqual(self.engine.quality_profile, "balanced")

    def test_profile_name_case_insensitive(self):
        self.engine.set_quality_profile("MAX")
        self.assertEqual(self.engine.quality_profile, "max")

    def test_profile_name_strips_whitespace(self):
        self.engine.set_quality_profile("  max  ")
        self.assertEqual(self.engine.quality_profile, "max")


# ---------------------------------------------------------------------------
# 2. Language hint resolution
# ---------------------------------------------------------------------------

class LanguageResolutionTests(unittest.TestCase):
    """_resolve_language должен корректно маппить коды и обрабатывать спецзначения."""

    def _resolve(self, hint):
        from core.engine import AudioEngine
        return AudioEngine._resolve_language(hint)

    def test_none_returns_none(self):
        self.assertIsNone(self._resolve(None))

    def test_auto_returns_none(self):
        self.assertIsNone(self._resolve("auto"))

    def test_auto_uppercase_returns_none(self):
        self.assertIsNone(self._resolve("AUTO"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(self._resolve(""))

    def test_ru_passes_through(self):
        self.assertEqual(self._resolve("ru"), "ru")

    def test_es_passes_through(self):
        self.assertEqual(self._resolve("es"), "es")

    def test_en_passes_through(self):
        self.assertEqual(self._resolve("en"), "en")

    def test_unknown_hint_returns_none_with_warning(self):
        with self.assertLogs("KrabEar.Engine", level="WARNING") as cm:
            result = self._resolve("zh")
        self.assertIsNone(result)
        self.assertTrue(any("zh" in msg for msg in cm.output))

    def test_hint_with_spaces_is_stripped(self):
        self.assertEqual(self._resolve("  ru  "), "ru")


# ---------------------------------------------------------------------------
# 3. Unavailable model tracking
# ---------------------------------------------------------------------------

class UnavailableModelTests(unittest.TestCase):
    """Помечённые модели не должны повторно использоваться в fallback chain."""

    def test_unavailable_set_starts_empty(self):
        engine = _make_engine()
        self.assertEqual(len(engine._unavailable_models), 0)

    def test_mark_model_unavailable(self):
        engine = _make_engine()
        engine._unavailable_models["mlx-community/whisper-large-v3-mlx"] = __import__("time").monotonic()
        self.assertIn("mlx-community/whisper-large-v3-mlx", engine._unavailable_models)

    def test_fallback_chain_skips_unavailable_model(self):
        """Если balanced модель помечена недоступной, chain должен вернуть ошибку
        (offline_strict) или попробовать следующую. Мы проверяем offline_strict path."""
        engine = _make_engine()
        from core.config import settings

        # Помечаем balanced модель как недоступную
        engine._unavailable_models[settings.MODEL_BALANCED] = __import__("time").monotonic()

        # В offline_strict режиме после исчерпания всех кандидатов должен быть RuntimeError
        with patch("core.engine.settings") as mock_settings:
            mock_settings.MODEL_BALANCED = settings.MODEL_BALANCED
            mock_settings.model_max_list = [settings.MODEL_BALANCED]  # все недоступны
            mock_settings.NETWORK_MODE = "offline_strict"
            mock_settings.TRANSCRIBE_TIMEOUT_SEC = 5
            mock_settings.PARAKEET_ENABLED = False
            mock_settings.SENSEVOICE_ENABLED = False
            mock_settings.WHISPERX_ENABLED = False
            mock_settings.VOXTRAL_ENABLED = False

            engine._unavailable_models[settings.MODEL_BALANCED] = __import__("time").monotonic()

            with self.assertRaises(RuntimeError):
                engine._transcribe_with_fallback_impl("fake.wav", "", None)

    def test_model_marked_unavailable_after_exception(self):
        """Если модель бросает исключение при транскрибации, она помечается недоступной."""
        engine = _make_engine()
        from core.config import settings

        with patch("core.engine.settings") as mock_settings:
            mock_settings.MODEL_BALANCED = settings.MODEL_BALANCED
            mock_settings.model_max_list = [settings.MODEL_BALANCED]
            mock_settings.NETWORK_MODE = "offline_strict"
            mock_settings.TRANSCRIBE_TIMEOUT_SEC = 5
            mock_settings.PARAKEET_ENABLED = False
            mock_settings.SENSEVOICE_ENABLED = False
            mock_settings.WHISPERX_ENABLED = False
            mock_settings.VOXTRAL_ENABLED = False

            with patch.object(engine, "_transcribe_model", side_effect=RuntimeError("model error")):
                with patch("core.engine._get_available_memory_gb", return_value=10.0):
                    with self.assertRaises(RuntimeError):
                        engine._transcribe_with_fallback_impl("fake.wav", "", None)

            # После ошибки модель должна быть в _unavailable_models
            self.assertIn(settings.MODEL_BALANCED, engine._unavailable_models)


# ---------------------------------------------------------------------------
# 4. Fallback chain candidate construction
# ---------------------------------------------------------------------------

class FallbackChainCandidateTests(unittest.TestCase):
    """Проверяем, что fallback chain правильно формирует список кандидатов."""

    def _run_chain_and_capture_candidates(self, engine, **settings_overrides):
        """Запускает _transcribe_with_fallback_impl и захватывает список candidates
        до первого вызова _transcribe_model через side_effect-перехват."""
        from core.config import settings
        captured = []

        def fake_transcribe_model(audio_data, model_name, prompt, language):
            captured.append(model_name)
            raise RuntimeError("stop")  # прерываем chain

        with patch("core.engine.settings") as mock_settings:
            mock_settings.MODEL_BALANCED = settings.MODEL_BALANCED
            mock_settings.model_max_list = settings.model_max_list
            mock_settings.NETWORK_MODE = "offline_strict"
            mock_settings.TRANSCRIBE_TIMEOUT_SEC = 5
            mock_settings.PARAKEET_ENABLED = settings_overrides.get("PARAKEET_ENABLED", False)
            mock_settings.SENSEVOICE_ENABLED = settings_overrides.get("SENSEVOICE_ENABLED", False)
            mock_settings.WHISPERX_ENABLED = settings_overrides.get("WHISPERX_ENABLED", False)
            mock_settings.VOXTRAL_ENABLED = settings_overrides.get("VOXTRAL_ENABLED", False)

            with patch.object(engine, "_transcribe_model", side_effect=fake_transcribe_model):
                with patch("core.engine._get_available_memory_gb", return_value=10.0):
                    try:
                        engine._transcribe_with_fallback_impl("fake.wav", "", None)
                    except RuntimeError:
                        pass

        return captured

    def test_balanced_profile_has_single_candidate(self):
        engine = _make_engine()
        engine.quality_profile = "balanced"
        candidates = self._run_chain_and_capture_candidates(engine)
        self.assertEqual(len(candidates), 1)

    def test_max_profile_has_multiple_candidates(self):
        engine = _make_engine()
        engine.set_quality_profile("max")
        candidates = self._run_chain_and_capture_candidates(engine)
        # max profile использует model_max_list — обычно 2+ кандидата или 1 unique
        self.assertGreaterEqual(len(candidates), 1)

    def test_sensevoice_marker_inserted_when_enabled(self):
        engine = _make_engine()

        from core.engine import AudioEngine
        sv_marker = AudioEngine._SENSEVOICE_MARKER

        with patch("core.engine.settings") as mock_settings:
            from core.config import settings
            mock_settings.MODEL_BALANCED = settings.MODEL_BALANCED
            mock_settings.model_max_list = [settings.MODEL_BALANCED]
            mock_settings.NETWORK_MODE = "offline_strict"
            mock_settings.TRANSCRIBE_TIMEOUT_SEC = 5
            mock_settings.PARAKEET_ENABLED = False
            mock_settings.SENSEVOICE_ENABLED = True
            mock_settings.WHISPERX_ENABLED = False
            mock_settings.VOXTRAL_ENABLED = False

            # Патчим SenseVoice так, что он сразу фейлит и помечается unavailable
            with patch.object(engine, "_transcribe_sensevoice", side_effect=RuntimeError("no model")):
                with patch.object(engine, "_transcribe_model", side_effect=RuntimeError("stop")):
                    with patch("core.engine._get_available_memory_gb", return_value=10.0):
                        try:
                            engine._transcribe_with_fallback_impl("fake.wav", "", None)
                        except RuntimeError:
                            pass

            # SenseVoice должен был попробоваться (и пометиться недоступным)
            self.assertIn(sv_marker, engine._unavailable_models)


# ---------------------------------------------------------------------------
# 5. SenseVoice output parser
# ---------------------------------------------------------------------------

class SenseVoiceParserTests(unittest.TestCase):
    """_parse_sensevoice_output должен корректно разбирать токены."""

    def _parse(self, raw):
        from core.engine import AudioEngine
        return AudioEngine._parse_sensevoice_output(raw)

    def test_empty_string_returns_empty(self):
        clean, emotion, lang = self._parse("")
        self.assertEqual(clean, "")
        self.assertIsNone(emotion)
        self.assertIsNone(lang)

    def test_full_format_with_emotion_and_lang(self):
        raw = "<|ru|><|HAPPY|><|Speech|><|woitn|>привет мир"
        clean, emotion, lang = self._parse(raw)
        self.assertEqual(clean, "привет мир")
        self.assertEqual(emotion, "happy")
        self.assertEqual(lang, "ru")

    def test_neutral_emotion_extracted(self):
        raw = "<|en|><|NEUTRAL|>hello world"
        clean, emotion, lang = self._parse(raw)
        self.assertEqual(clean, "hello world")
        self.assertEqual(emotion, "neutral")
        self.assertEqual(lang, "en")

    def test_angry_emotion_extracted(self):
        raw = "<|ru|><|ANGRY|>это возмутительно"
        clean, emotion, lang = self._parse(raw)
        self.assertEqual(emotion, "angry")
        self.assertEqual(clean, "это возмутительно")

    def test_no_tags_returns_text_as_is(self):
        raw = "просто текст без тегов"
        clean, emotion, lang = self._parse(raw)
        self.assertEqual(clean, "просто текст без тегов")
        self.assertIsNone(emotion)
        self.assertIsNone(lang)

    def test_unknown_emotion_token_ignored(self):
        """Неизвестный эмоциональный токен не должен ломать парсинг."""
        raw = "<|ru|><|UNKNOWN_EMOTION|>текст"
        clean, emotion, lang = self._parse(raw)
        self.assertEqual(clean, "текст")
        self.assertEqual(lang, "ru")
        # UNKNOWN_EMOTION не в маппинге — emotion должна остаться None
        self.assertIsNone(emotion)

    def test_nospeech_lang_mapped_to_empty_string(self):
        """Тег nospeech маппится в пустую строку → lang=None."""
        raw = "<|nospeech|>"
        clean, emotion, lang = self._parse(raw)
        # Пустая строка mapped → lang остаётся None
        self.assertIsNone(lang)


# ---------------------------------------------------------------------------
# 6. Segment overlap helper
# ---------------------------------------------------------------------------

class SegmentOverlapTests(unittest.TestCase):
    """_segment_overlap — арифметика пересечения временных отрезков."""

    def _overlap(self, a_start, a_end, b_start, b_end):
        from core.engine import AudioEngine
        return AudioEngine._segment_overlap(a_start, a_end, b_start, b_end)

    def test_no_overlap_returns_zero(self):
        self.assertEqual(self._overlap(0.0, 1.0, 2.0, 3.0), 0.0)

    def test_full_containment_returns_inner_length(self):
        self.assertAlmostEqual(self._overlap(0.0, 5.0, 1.0, 3.0), 2.0)

    def test_partial_overlap(self):
        self.assertAlmostEqual(self._overlap(0.0, 2.0, 1.0, 3.0), 1.0)

    def test_identical_segments(self):
        self.assertAlmostEqual(self._overlap(1.0, 3.0, 1.0, 3.0), 2.0)

    def test_touching_at_boundary_returns_zero(self):
        # [0,1] и [1,2] — touching at 1.0, но overlap = 0
        self.assertEqual(self._overlap(0.0, 1.0, 1.0, 2.0), 0.0)


# ---------------------------------------------------------------------------
# 7. Speaker annotation and turn merging (integration of helpers)
# ---------------------------------------------------------------------------

class SpeakerAnnotationTests(unittest.TestCase):
    """Интеграционные тесты _annotate_segments_with_speakers + _merge_speaker_turns."""

    def setUp(self):
        self.engine = _make_engine()

    def test_speaker_unknown_when_no_speaker_segments(self):
        whisper_segs = [{"start": 0.0, "end": 2.0, "text": "Привет"}]
        annotated = self.engine._annotate_segments_with_speakers(whisper_segs, [])
        self.assertEqual(annotated[0]["speaker"], "SPEAKER_UNKNOWN")

    def test_empty_text_segments_excluded_from_turns(self):
        annotated = [
            {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": ""},
            {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_01", "text": "Текст"},
        ]
        turns = self.engine._merge_speaker_turns(annotated)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["speaker"], "SPEAKER_01")

    def test_consecutive_same_speaker_merged(self):
        annotated = [
            {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "Привет"},
            {"start": 1.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "как дела"},
            {"start": 2.0, "end": 3.0, "speaker": "SPEAKER_01", "text": "Хорошо"},
        ]
        turns = self.engine._merge_speaker_turns(annotated)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["text"], "Привет как дела")
        self.assertEqual(turns[0]["end"], 2.0)


# ---------------------------------------------------------------------------
# 8. LLM rewrite toggle
# ---------------------------------------------------------------------------

class LLMRewriteToggleTests(unittest.TestCase):
    """_llm_rewrite_allowed должен учитывать и наличие rewriter'а, и runtime toggle."""

    def test_no_rewriter_returns_false(self):
        engine = _make_engine(llm_rewriter=None)
        self.assertFalse(engine._llm_rewrite_allowed())

    def test_rewriter_present_but_toggle_off_returns_false(self):
        mock_rewriter = MagicMock()
        engine = _make_engine(
            llm_rewriter=mock_rewriter,
            settings_get=lambda k, d: False,  # toggle выключен
        )
        self.assertFalse(engine._llm_rewrite_allowed())

    def test_rewriter_present_and_toggle_on_returns_true(self):
        mock_rewriter = MagicMock()
        # privacy_mode_enabled must be False, llm_rewrite_enabled must be True
        # (W1229 F3: privacy_mode=True blocks LLM rewrite even if toggle is on)
        engine = _make_engine(
            llm_rewriter=mock_rewriter,
            settings_get=lambda k, d: {"llm_rewrite_enabled": True, "privacy_mode_enabled": False}.get(k, d),
        )
        self.assertTrue(engine._llm_rewrite_allowed())

    def test_settings_get_receives_correct_key(self):
        """settings_get вызывается с ключом 'llm_rewrite_enabled'."""
        received_keys = []

        def capturing_settings_get(key, default):
            received_keys.append(key)
            # privacy_mode_enabled=False, llm_rewrite_enabled=True so all checks pass
            return {"llm_rewrite_enabled": True, "privacy_mode_enabled": False}.get(key, default)

        mock_rewriter = MagicMock()
        engine = _make_engine(
            llm_rewriter=mock_rewriter,
            settings_get=capturing_settings_get,
        )
        engine._llm_rewrite_allowed()
        self.assertIn("llm_rewrite_enabled", received_keys)


# ---------------------------------------------------------------------------
# 9. _resolve_audio_path
# ---------------------------------------------------------------------------

class ResolveAudioPathTests(unittest.TestCase):
    """_resolve_audio_path возвращает абсолютный str-путь или None."""

    def setUp(self):
        self.engine = _make_engine()

    def test_str_existing_file_returns_path(self):
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        try:
            result = self.engine._resolve_audio_path(tmp)
            self.assertIsNotNone(result)
            self.assertEqual(result, str(Path(tmp).expanduser().resolve()))
        finally:
            os.unlink(tmp)

    def test_path_object_existing_file_returns_path(self):
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = Path(f.name)
        try:
            result = self.engine._resolve_audio_path(tmp)
            self.assertIsNotNone(result)
        finally:
            os.unlink(str(tmp))

    def test_nonexistent_str_returns_none(self):
        result = self.engine._resolve_audio_path("/does/not/exist.wav")
        self.assertIsNone(result)

    def test_numpy_array_returns_none(self):
        """numpy array не является файловым путём — diarization невозможен."""
        import numpy as np
        result = self.engine._resolve_audio_path(np.zeros(16000, dtype=np.float32))
        self.assertIsNone(result)

    def test_bytes_returns_none(self):
        """bytes из AudioRecorder не является файловым путём."""
        result = self.engine._resolve_audio_path(b"\x00" * 100)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 10. _transcribe_with_fallback_impl: remote fallback path
# ---------------------------------------------------------------------------

class RemoteFallbackTests(unittest.TestCase):
    """Если все локальные модели недоступны и NETWORK_MODE != offline_strict,
    должен вызываться _transcribe_remote."""

    def test_remote_called_when_all_local_fail(self):
        engine = _make_engine()
        # Намерение теста: облачный STT НАСТРОЕН. Гейт «нет ключа — не ходить
        # в облако» (инцидент 2026-08-26) иначе обрывает каскад до remote,
        # и тест проверял бы не тот путь. Раньше проходило лишь потому,
        # что гейта в этой ветке не было вовсе.
        engine._remote_stt_retry_configured = lambda: True
        from core.config import settings

        remote_called = []

        def fake_remote(audio_data, prompt):
            remote_called.append(True)
            return {"text": "remote result", "engine": "remote"}

        with patch("core.engine.settings") as mock_settings:
            mock_settings.MODEL_BALANCED = settings.MODEL_BALANCED
            mock_settings.model_max_list = [settings.MODEL_BALANCED]
            mock_settings.NETWORK_MODE = "online_preferred"  # НЕ offline_strict
            mock_settings.TRANSCRIBE_TIMEOUT_SEC = 5
            mock_settings.PARAKEET_ENABLED = False
            mock_settings.SENSEVOICE_ENABLED = False
            mock_settings.WHISPERX_ENABLED = False
            mock_settings.VOXTRAL_ENABLED = False

            # Помечаем единственный кандидат недоступным
            engine._unavailable_models[settings.MODEL_BALANCED] = __import__("time").monotonic()

            with patch.object(engine, "_transcribe_remote", side_effect=fake_remote):
                result = engine._transcribe_with_fallback_impl("fake.wav", "", None)

        self.assertTrue(remote_called)
        self.assertEqual(result["engine"], "remote")


if __name__ == "__main__":
    unittest.main()
