"""Юнит-тесты для однопроходного режима STT (live subs, 2026-08-12).

Живой инцидент: окно live-субтитров длиной 2.5с прошло GigaAM (пусто) →
whisper-large-v3 (confidence 0.61 < 0.65 порог, retry) → whisper-large-v3-turbo
(retry) = 9.49с на окно, которое приходит каждые ~3с. `single_pass=True`
отключает ДВА прохода, спроектированных для диктовки: (1) confidence-driven
multi-pass retry (`_maybe_multipass_retry`) и (2) request-local fallback на
Whisper после пустого успешного результата GigaAM.

Спека: docs/superpowers/specs/2026-08-12-live-subs-single-pass-design.md
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
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    """Создаёт AudioEngine без фонового GigaAM-warmup треда/subprocess'а.

    `skip_gigaam_warmup=True` — не подходит: он также отключает сам GigaAM в
    chain-building (`_skip_gigaam`, Wave 525 REST-engine guard), а нашим
    тестам нужен реальный GigaAM-кандидат в chain. Вместо этого — тот же
    приём, что и test_engine_gigaam_integration.py::_make_audio_engine_without_warmup:
    патчим Thread.start на время конструктора, чтобы фоновый warmup (который
    в некоторых окружениях реально спавнит core/workers/gigaam_worker.py)
    никогда не стартовал и не утекал за пределы теста.
    """
    from core.engine import AudioEngine
    with patch("core.engine.threading.Thread.start", autospec=True):
        return AudioEngine()


def _base_transcribe_settings(mock_cfg: MagicMock) -> None:
    """Минимальный набор настроек для безопасного полного прогона
    engine.transcribe() с байтовым (не ndarray) аудио — тот же рецепт, что
    уже используется в test_engine_multipass.py (MultipassDisabledTest /
    PreviewNoRetryTest): байты обходят все numpy/file-специфичные ветки
    препроцессинга (denoise/VAD/gain/smart-silence/streaming-chunking).
    """
    mock_cfg.STT_MIN_CONFIDENCE_THRESHOLD = 0.65
    mock_cfg.STT_MAX_RETRIES = 2
    mock_cfg.TRANSCRIBE_LANGUAGE = "ru"
    mock_cfg.DIARIZATION_ENABLED = False
    mock_cfg.SMART_SILENCE_SKIP_ENABLED = False
    mock_cfg.MAX_AUDIO_MB = 1000
    mock_cfg.SENSEVOICE_EMOTION_TO_HISTORY = False
    # Явно отключаем pipeline_v2 обеими проверяемыми в коде ветками —
    # иначе MagicMock-автоатрибут PIPELINE_V2_ENABLED окажется truthy и
    # not-None, код полезет за реальным transcribe_v2 (мягко ловится except,
    # но лишний реальный импорт/попытка на каждый тест не нужны).
    mock_cfg.PIPELINE_V2_ENABLED = None
    mock_cfg.PIPELINE_V2 = False
    mock_cfg.TRANSCRIBE_PROMPT = "prompt"
    mock_cfg.LLM_ENABLED = False
    mock_cfg.STT_SPEAKER_AWARE_PROMPT_ENABLED = False
    mock_cfg.NETWORK_MODE = "offline_strict"
    mock_cfg.TRANSCRIBE_TIMEOUT_SEC = 30
    # GigaAM chain-building (candidates list) — только GigaAM включён,
    # остальные адаптеры выключены, чтобы chain был предсказуемым:
    # [GIGAAM_MARKER, current_model].
    mock_cfg.STT_GIGAAM_ENABLED = True
    mock_cfg.STT_GIGAAM_MODE = "rnnt"
    mock_cfg.STT_USE_RU_FINETUNE = False
    mock_cfg.PARAKEET_ENABLED = False
    mock_cfg.SENSEVOICE_ENABLED = False
    mock_cfg.WHISPERX_ENABLED = False
    mock_cfg.VOXTRAL_ENABLED = False
    mock_cfg.model_max_list = ["max-model"]


def _wire_gigaam(engine, empty: bool = True, confidence: float = 0.9) -> MagicMock:
    """Подключает фейковый GigaAM-адаптер, добавляемый первым в chain."""
    engine._router = MagicMock()
    engine._router.get_gigaam_adapter.return_value = MagicMock()
    gigaam_mock = MagicMock(return_value={
        "text": "" if empty else "тест",
        "confidence": confidence,
        "engine": "gigaam-rnnt",
        "language": "ru",
    })
    engine._transcribe_gigaam = gigaam_mock
    return gigaam_mock


# ---------------------------------------------------------------------------
# 1. _transcribe_with_fallback(_impl) level — request-local fallback gate
# ---------------------------------------------------------------------------

class SinglePassGigaamEmptyFallbackTest(unittest.TestCase):
    """single_pass=True → пустой успешный GigaAM НЕ продолжает chain на Whisper."""

    def test_single_pass_true_returns_empty_gigaam_result_without_whisper(self):
        engine = _make_engine()
        gigaam_mock = _wire_gigaam(engine, empty=True)

        with patch.object(engine, "_transcribe_model") as mock_tm, \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_GIGAAM_ENABLED = True
            mock_cfg.STT_USE_RU_FINETUNE = False
            mock_cfg.PARAKEET_ENABLED = False
            mock_cfg.SENSEVOICE_ENABLED = False
            mock_cfg.WHISPERX_ENABLED = False
            mock_cfg.VOXTRAL_ENABLED = False
            mock_cfg.STT_GIGAAM_MODE = "rnnt"

            result = engine._transcribe_with_fallback(
                b"\x00" * 100, prompt="", language="ru", single_pass=True,
            )

        gigaam_mock.assert_called_once()
        mock_tm.assert_not_called()
        self.assertEqual(result["text"], "")
        # Пустой успешный ответ single_pass — НЕ ошибка GigaAM, blacklist не трогаем.
        self.assertNotIn(engine._GIGAAM_MARKER, engine._unavailable_models)

    def test_single_pass_false_still_falls_back_to_whisper(self):
        """Регрессия: старое поведение (по умолчанию) не изменилось."""
        engine = _make_engine()
        gigaam_mock = _wire_gigaam(engine, empty=True)
        whisper_result = {"text": "whisper после тишины", "language": "ru", "segments": []}

        with patch.object(engine, "_transcribe_model", return_value=whisper_result) as mock_tm, \
             patch("core.engine._get_available_memory_gb", return_value=999.0), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_GIGAAM_ENABLED = True
            mock_cfg.STT_USE_RU_FINETUNE = False
            mock_cfg.PARAKEET_ENABLED = False
            mock_cfg.SENSEVOICE_ENABLED = False
            mock_cfg.WHISPERX_ENABLED = False
            mock_cfg.VOXTRAL_ENABLED = False
            mock_cfg.STT_GIGAAM_MODE = "rnnt"
            mock_cfg.MODEL_BALANCED = engine.current_model

            result = engine._transcribe_with_fallback(
                b"\x00" * 100, prompt="", language="ru",
            )  # single_pass не передан → False по умолчанию

        gigaam_mock.assert_called_once()
        mock_tm.assert_called_once()
        self.assertEqual(result["text"], "whisper после тишины")
        self.assertNotIn(engine._GIGAAM_MARKER, engine._unavailable_models)


# ---------------------------------------------------------------------------
# 2. transcribe() level — confidence-driven multi-pass retry gate
# ---------------------------------------------------------------------------

class SinglePassSkipsMultipassRetryTest(unittest.TestCase):
    """single_pass=True → _maybe_multipass_retry не вызывается даже при низкой уверенности."""

    def test_single_pass_true_skips_multipass(self):
        engine = _make_engine()

        with patch.object(engine, "_maybe_multipass_retry") as mock_mp, \
             patch.object(engine, "_transcribe_with_fallback") as mock_fb, \
             patch.object(engine, "_maybe_run_diarization", return_value=None), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            _base_transcribe_settings(mock_cfg)
            mock_fb.return_value = {
                "text": "низкая уверенность", "segments": [], "model_used": "balanced-model",
                "confidence": 0.10,
            }

            engine.transcribe(b"\x00" * 100, is_preview=False, single_pass=True)

        mock_mp.assert_not_called()
        # single_pass прокинут в fallback-цепочку.
        self.assertTrue(mock_fb.call_args.kwargs.get("single_pass"))

    def test_single_pass_false_still_retries(self):
        """Регрессия: по умолчанию (single_pass=False) ретрай выполняется, если включён."""
        engine = _make_engine()

        with patch.object(engine, "_maybe_multipass_retry") as mock_mp, \
             patch.object(engine, "_transcribe_with_fallback") as mock_fb, \
             patch.object(engine, "_maybe_run_diarization", return_value=None), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            _base_transcribe_settings(mock_cfg)
            _low_conf_result = {
                "text": "низкая уверенность", "segments": [], "model_used": "balanced-model",
                "confidence": 0.10,
            }
            mock_fb.return_value = _low_conf_result
            mock_mp.return_value = _low_conf_result

            engine.transcribe(b"\x00" * 100, is_preview=False, single_pass=False)

        mock_mp.assert_called_once()
        self.assertFalse(mock_fb.call_args.kwargs.get("single_pass"))


# ---------------------------------------------------------------------------
# 3. Полный прогон transcribe() — ФАКТ количества вызовов движка (не время)
# ---------------------------------------------------------------------------

class SinglePassExactEngineCallCountTest(unittest.TestCase):
    """Воспроизводит живой лог: GigaAM пусто → single_pass не идёт дальше.

    single_pass=True: ровно ОДИН вызов движка (GigaAM), whisper не вызывается
    ни разу — ни как request-local fallback, ни как multipass retry.
    single_pass=False: несколько вызовов (GigaAM + whisper + retry) — старое
    поведение сохранено.
    """

    def test_single_pass_true_exactly_one_engine_call(self):
        engine = _make_engine()
        gigaam_mock = _wire_gigaam(engine, empty=True)

        with patch.object(engine, "_transcribe_model") as mock_tm, \
             patch.object(engine, "_maybe_run_diarization", return_value=None), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            _base_transcribe_settings(mock_cfg)

            result = engine.transcribe(b"\x00" * 100, is_preview=False, single_pass=True)

        total_engine_calls = gigaam_mock.call_count + mock_tm.call_count
        self.assertEqual(total_engine_calls, 1, "single_pass=True должен звать движок ровно один раз")
        gigaam_mock.assert_called_once()
        mock_tm.assert_not_called()
        self.assertEqual(result.get("text"), "")

    def test_single_pass_false_makes_multiple_engine_calls(self):
        """Регрессия: без single_pass живой сценарий делает несколько проходов."""
        engine = _make_engine()
        gigaam_mock = _wire_gigaam(engine, empty=True)
        import math
        _low_conf_segments = [{"avg_logprob": math.log(0.3), "text": "текст"}]
        whisper_low_conf = {
            "text": "текст", "language": "ru", "segments": _low_conf_segments,
        }

        with patch.object(engine, "_transcribe_model", return_value=whisper_low_conf) as mock_tm, \
             patch.object(engine, "_maybe_run_diarization", return_value=None), \
             patch("core.engine._get_available_memory_gb", return_value=999.0), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            _base_transcribe_settings(mock_cfg)
            mock_cfg.MODEL_BALANCED = engine.current_model

            engine.transcribe(b"\x00" * 100, is_preview=False, single_pass=False)

        total_engine_calls = gigaam_mock.call_count + mock_tm.call_count
        self.assertGreater(
            total_engine_calls, 1,
            "single_pass=False должен сохранить старое многопроходное поведение",
        )
        gigaam_mock.assert_called_once()
        # balanced whisper (request-local fallback) + хотя бы один multipass retry.
        self.assertGreaterEqual(mock_tm.call_count, 2)


# ---------------------------------------------------------------------------
# 4. Пустой результат первого движка при single_pass=True — путь не падает
# ---------------------------------------------------------------------------

class SinglePassEmptyResultDoesNotCrashTest(unittest.TestCase):
    """Пустой результат первого движка при single_pass=True не бросает исключений."""

    def test_empty_first_engine_result_returns_well_formed_dict(self):
        engine = _make_engine()
        _wire_gigaam(engine, empty=True)

        with patch.object(engine, "_transcribe_model") as mock_tm, \
             patch.object(engine, "_maybe_run_diarization", return_value=None), \
             patch("core.engine.settings") as mock_cfg:
            mock_cfg.STT_MULTIPASS_ENABLED = True
            _base_transcribe_settings(mock_cfg)

            result = engine.transcribe(b"\x00" * 100, is_preview=False, single_pass=True)

        mock_tm.assert_not_called()
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("text"), "")


if __name__ == "__main__":
    unittest.main()
