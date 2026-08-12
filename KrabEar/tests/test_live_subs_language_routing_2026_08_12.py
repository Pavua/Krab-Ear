"""Live-субтитры: явный язык → GigaAM для русского, Whisper для остального.

Спека: docs/superpowers/specs/2026-08-12-live-subs-language-routing-design.md

Проблема: `_process_window` не передавал `lang_hint` в `transcribe()` — язык
резолвился в статический `settings.TRANSCRIBE_LANGUAGE`, а не в явную,
пользовательски настраиваемую настройку live-субтитров. Решение — настройка
`live_subs_language` (дефолт "ru"): конкретный код передаётся как `lang_hint`
напрямую (GigaAM для "ru" структурно не может утечь промптом — не принимает
`initial_prompt` вовсе), `"auto"` сохраняет прежнее поведение (`lang_hint`
не передаётся).

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_live_subs_language_routing_2026_08_12.py -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# ── path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np  # noqa: E402

from backend.live_subs_service import LiveSubsService  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402
from core.config import DEFAULT_SETTINGS  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _pcm_bytes(duration_sec: float, sample_rate: int = 16000) -> bytes:
    n = int(duration_sec * sample_rate)
    return np.zeros(n, dtype=np.int16).tobytes()


def _make_whisper_result(text: str) -> dict:
    """Минимальный dict, который `_transcribe_with_fallback_impl` принимает как
    успешный whisper-результат (см. test_live_subs_prompt_leakage_2026_08_12.py)."""
    return {
        "text": text,
        "segments": [{"avg_logprob": -0.2}],
        "engine": "fake-whisper",
        "model_used": "fake",
        "language": "ru",
    }


def _make_service_with_lang(
    live_subs_language: str, stt_text: str = "hello"
) -> LiveSubsService:
    """LiveSubsService с мок-Transcriber (без реального MLX) и заданной
    настройкой live_subs_language, прочитанной через settings_get — тот же
    способ, каким сервис уже читает privacy_mode_enabled."""
    transcriber = MagicMock()
    transcriber.transcribe.return_value = {"text": stt_text, "language": "ru"}

    tr_result = TranslationResult(
        text="привет", status="ok", source_lang="ru", target_lang="ru",
        mode="off", engine="stub",
    )
    translator = MagicMock()
    translator.translate.return_value = tr_result

    settings_get = lambda k, d: live_subs_language if k == "live_subs_language" else d  # noqa: E731

    return LiveSubsService(transcriber=transcriber, translator=translator, settings_get=settings_get)


# ── DoD: DEFAULT_SETTINGS содержит live_subs_language="ru" ────────────────────

class DefaultSettingsTests(unittest.TestCase):
    def test_default_is_ru(self) -> None:
        self.assertEqual(DEFAULT_SETTINGS.get("live_subs_language"), "ru")


# ── DoD: _process_window передаёт lang_hint для конкретного языка ─────────────

class ProcessWindowPassesLangHintTests(unittest.TestCase):
    """`_process_window` обязана прокидывать `lang_hint` в `transcriber.transcribe()`
    при конкретном языке настройки live_subs_language."""

    def test_ru_passes_lang_hint_ru(self) -> None:
        svc = _make_service_with_lang("ru")
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=3.0))

        svc._transcriber.transcribe.assert_called_once()
        _, kwargs = svc._transcriber.transcribe.call_args
        self.assertEqual(kwargs.get("lang_hint"), "ru")
        svc.close()

    def test_es_passes_lang_hint_es(self) -> None:
        svc = _make_service_with_lang("es")
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=3.0))

        _, kwargs = svc._transcriber.transcribe.call_args
        self.assertEqual(kwargs.get("lang_hint"), "es")
        svc.close()

    def test_en_passes_lang_hint_en(self) -> None:
        svc = _make_service_with_lang("en")
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=3.0))

        _, kwargs = svc._transcriber.transcribe.call_args
        self.assertEqual(kwargs.get("lang_hint"), "en")
        svc.close()


class ProcessWindowAutoDoesNotPassLangHintTests(unittest.TestCase):
    """`"auto"` — прежнее поведение: `lang_hint` не передаётся (остаётся None)."""

    def test_auto_passes_no_lang_hint(self) -> None:
        svc = _make_service_with_lang("auto")
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=3.0))

        _, kwargs = svc._transcriber.transcribe.call_args
        self.assertIsNone(kwargs.get("lang_hint"))
        svc.close()

    def test_missing_setting_defaults_to_ru_not_auto(self) -> None:
        """settings_get без явного ключа (fallback на дефолт "ru" из вызывающего
        кода) — регресс-гвард: _process_window обязан передавать default="ru"
        в settings_get, а не молча трактовать отсутствие ключа как "auto"."""
        transcriber = MagicMock()
        transcriber.transcribe.return_value = {"text": "hello", "language": "ru"}
        translator = MagicMock()
        translator.translate.return_value = TranslationResult(
            text="привет", status="ok", source_lang="ru", target_lang="ru",
            mode="off", engine="stub",
        )
        # settings_get, который не знает про live_subs_language вообще —
        # имитирует старый settings.json без этого ключа.
        settings_get = lambda k, d: d  # noqa: E731
        svc = LiveSubsService(transcriber=transcriber, translator=translator, settings_get=settings_get)

        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=3.0))

        _, kwargs = svc._transcriber.transcribe.call_args
        self.assertEqual(kwargs.get("lang_hint"), "ru")
        svc.close()


# ── DoD: при "ru" автоопределение языка (AudioLanguageID) НЕ вызывается ───────
# Доказываем через spy на РЕАЛЬНОМ пути transcribe (AudioEngine +
# Transcriber), а не мок-Transcriber верхнего уровня (тот бы дал только
# "результат похож" — сам факт вызова AudioLanguageID.detect() живёт на
# несколько слоёв ниже LiveSubsService).

class RuLangHintRoutesToGigaamTests(unittest.TestCase):
    """Доказываем, что явный `lang_hint="ru"` (из настройки live_subs_language) —
    и НИЧЕГО, кроме него — заставляет реальную цепочку `_transcribe_with_fallback_impl`
    выбрать GigaAM первым кандидатом, а `AudioLanguageID.detect()` не вызывается.

    `TRANSCRIBE_LANGUAGE` временно патчится на "es" (заведомо НЕ "ru"): если бы
    тест был "результатом похож" (GigaAM запустился бы просто потому, что
    статический дефолт TRANSCRIBE_LANGUAGE и так "ru"), патч на "es" сломал бы
    это совпадение — тест проходит именно благодаря явному lang_hint, дошедшему
    от `_process_window` через `Transcriber.transcribe` до `_effective_lang`.
    """

    @patch("core.audio_lang_id.AudioLanguageID.detect")
    @patch("core.engine.AudioEngine._maybe_run_diarization")
    def test_live_subs_language_ru_selects_gigaam_over_whisper(
        self, mock_diar, mock_lid_detect
    ) -> None:
        from core.engine import AudioEngine
        from core.config import settings as core_settings
        from backend.transcriber import Transcriber

        mock_diar.return_value = None

        # skip_gigaam_warmup НЕ передаём: это REST-engine контракт, который
        # запрещает GigaAM целиком (см. Wave 525) — здесь GigaAM должен быть
        # реально доступен движку, только адаптер и сам вызов замоканы.
        with patch("core.engine.threading.Thread.start", autospec=True):
            engine = AudioEngine()

        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = MagicMock()  # адаптер "жив"
        engine._router = fake_router
        engine._transcribe_gigaam = MagicMock(return_value={
            "text": "привет мир", "language": "ru", "confidence": 0.92, "engine": "gigaam-rnnt",
        })

        transcriber = Transcriber(engine=engine)
        settings_get = lambda k, d: "ru" if k == "live_subs_language" else d  # noqa: E731
        svc = LiveSubsService(
            transcriber=transcriber, translator=MagicMock(), settings_get=settings_get
        )

        with patch.object(core_settings, "STT_GIGAAM_ENABLED", True), \
                patch.object(core_settings, "TRANSCRIBE_LANGUAGE", "es"):
            svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
            self.assertTrue(svc.wait_until_idle(timeout=3.0))

        engine._transcribe_gigaam.assert_called_once()
        mock_lid_detect.assert_not_called()
        svc.close()


# ── DoD: GigaAM недоступен → деградация на Whisper без исключения ────────────

class GigaamUnavailableDegradesToWhisperTests(unittest.TestCase):
    """При live_subs_language="ru" и структурно недоступном GigaAM в этом
    инстансе engine (skip_gigaam_warmup=True — тот же REST-engine контракт,
    что запрещает GigaAM целиком, Wave 525) путь не падает: явный lang_hint
    не мешает деградации на whisper-кандидата, исключение не долетает до
    LiveSubsService."""

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_gigaam_unavailable_falls_back_without_exception(
        self, mock_fallback, mock_diar
    ) -> None:
        from core.engine import AudioEngine
        from backend.transcriber import Transcriber

        mock_fallback.return_value = _make_whisper_result("привет мир")
        mock_diar.return_value = None

        engine = AudioEngine(skip_gigaam_warmup=True)
        transcriber = Transcriber(engine=engine)
        settings_get = lambda k, d: "ru" if k == "live_subs_language" else d  # noqa: E731

        svc = LiveSubsService(
            transcriber=transcriber, translator=MagicMock(), settings_get=settings_get
        )

        result = svc.ingest(_pcm_bytes(3.0), 16000, "off", True)  # is_final → синхронно

        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "привет мир")
        svc.close()


if __name__ == "__main__":
    unittest.main()
