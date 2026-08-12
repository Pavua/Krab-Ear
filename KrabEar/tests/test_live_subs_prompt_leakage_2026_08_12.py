"""G1/G2 (2026-08-12) — live-субтитры: утечка TRANSCRIBE_PROMPT в текст +
зацикленное окно, показанное пользователю поверх чужого видео.

Живой инцидент: владелец включил live-субтитры на русском YouTube-интервью —
на экране появилось «Сохраняй смысл 0 тяги, мастеряй смысл 0 тяги». Источник —
`core/config.py` TRANSCRIBE_PROMPT (инструкция для Whisper), переданный как
`initial_prompt`: Whisper трактует его как префикс предыдущего текста и на
коротких/плотных окнах начинает его выговаривать и зацикливается.

G1 — `Transcriber.transcribe(context_free=True)` → `AudioEngine.transcribe`
     формирует пустой initial_prompt (ни инструкции, ни истории, ни hotwords);
     LiveSubsService реально передаёт флаг.
G2 — зацикленное окно (is_likely_repetition_loop==True) не эмитится в
     LiveSubsService: дропается, инкрементит dropped_windows (F3), логируется
     БЕЗ утечки самого текста. Путь диктовки (AudioEngine напрямую) не тронут —
     raw_text по-прежнему возвращается неизменённым (engine.py: «не врём про
     input»).

Спека: docs/superpowers/specs/2026-08-12-live-subs-prompt-leakage-design.md

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_live_subs_prompt_leakage_2026_08_12.py -v
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _pcm_bytes(duration_sec: float, sample_rate: int = 16000) -> bytes:
    n = int(duration_sec * sample_rate)
    return np.zeros(n, dtype=np.int16).tobytes()


def _make_service(stt_text: str = "hello", translated: str = "привет") -> LiveSubsService:
    """LiveSubsService с фейковыми (MagicMock) зависимостями — без реального MLX."""
    transcriber = MagicMock()
    transcriber.transcribe.return_value = {"text": stt_text, "language": "ru"}

    tr_result = TranslationResult(
        text=translated, status="ok", source_lang="ru", target_lang="ru",
        mode="off", engine="stub",
    )
    translator = MagicMock()
    translator.translate.return_value = tr_result

    return LiveSubsService(transcriber=transcriber, translator=translator)


def _make_whisper_result(text: str) -> dict:
    return {
        "text": text,
        "segments": [{"avg_logprob": -0.2}],
        "engine": "fake-whisper",
        "model_used": "fake",
        "language": "ru",
    }


# Bigram "сохраняй смысл" x6 — тот же паттерн, что в живом инциденте
# (heuristic 1 из is_likely_repetition_loop: ≥5 identical adjacent bigrams).
LOOP_TEXT = ("сохраняй смысл " * 6).strip()


# ── G1: AudioEngine.transcribe(context_free=True) → пустой initial_prompt ─────

class EngineContextFreePromptTests(unittest.TestCase):
    """AudioEngine.transcribe() при context_free=True отдаёт Whisper'у пустой prompt."""

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_context_free_passes_empty_prompt(self, mock_fallback, mock_diar):
        """context_free=True → _transcribe_with_fallback получает prompt=''."""
        from core.engine import AudioEngine
        mock_fallback.return_value = _make_whisper_result("test")
        mock_diar.return_value = None
        engine = AudioEngine(skip_gigaam_warmup=True)
        engine.transcribe("fake.wav", is_preview=False, context_free=True)
        _, kwargs = mock_fallback.call_args
        self.assertEqual(kwargs.get("prompt"), "")

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_context_free_false_keeps_full_prompt_bytewise(self, mock_fallback, mock_diar):
        """context_free=False (дефолт) — поведение байт-в-байт прежнее: полный
        TRANSCRIBE_PROMPT + тематика + extra_vocabulary, как и до фикса."""
        from core.engine import AudioEngine
        from core.config import settings
        mock_fallback.return_value = _make_whisper_result("test")
        mock_diar.return_value = None
        engine = AudioEngine(skip_gigaam_warmup=True)
        engine.transcribe(
            "fake.wav", is_preview=False, domain="casual", extra_vocabulary=["Mercadona"],
        )
        _, kwargs = mock_fallback.call_args
        prompt = kwargs.get("prompt", "")
        self.assertIn(settings.TRANSCRIBE_PROMPT, prompt)
        self.assertIn("Тематика:", prompt)
        self.assertIn("Mercadona", prompt)

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_context_free_does_not_imply_preview(self, mock_fallback, mock_diar):
        """context_free — ТОЛЬКО пустой промпт; в отличие от is_preview, диаризация
        остаётся включённой (is_preview остаётся False внутри _maybe_run_diarization).
        Навешивать на is_preview значило бы получить три незапрошенных изменения —
        именно это спека прямо запрещает (§3 G1)."""
        from core.engine import AudioEngine
        mock_fallback.return_value = _make_whisper_result("обычный текст")
        mock_diar.return_value = None
        engine = AudioEngine(skip_gigaam_warmup=True)
        engine.transcribe("fake.wav", is_preview=False, context_free=True)
        _, diar_kwargs = mock_diar.call_args
        self.assertFalse(diar_kwargs.get("is_preview"))

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_context_free_does_not_disable_loop_detector(self, mock_fallback, mock_diar):
        """context_free=True не гейтит repetition-loop детектор в engine.py —
        тот срабатывает как обычно (тот же путь, что и без context_free)."""
        from core.engine import AudioEngine
        mock_fallback.return_value = _make_whisper_result(LOOP_TEXT)
        mock_diar.return_value = None
        engine = AudioEngine(skip_gigaam_warmup=True)
        with self.assertLogs("KrabEar.Engine", level="WARNING") as cm:
            engine.transcribe("fake.wav", is_preview=False, context_free=True)
        loop_logs = [line for line in cm.output if "repetition loop detected" in line]
        self.assertEqual(len(loop_logs), 1)


# ── G1: LiveSubsService реально передаёт context_free (декоративная проводка) ─

class LiveSubsPassesContextFreeTests(unittest.TestCase):
    """Регресс на декоративную проводку: LiveSubsService обязана вызывать
    transcriber.transcribe(..., context_free=True), а не просто иметь параметр."""

    def test_process_window_calls_transcribe_with_context_free_true(self) -> None:
        svc = _make_service(stt_text="hello")
        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=3.0))

        svc._transcriber.transcribe.assert_called_once()
        _, kwargs = svc._transcriber.transcribe.call_args
        self.assertIs(kwargs.get("context_free"), True)
        svc.close()


# ── G2: зацикленное окно дропается в live subs ─────────────────────────────────

class LiveSubsRepetitionLoopDropTests(unittest.TestCase):
    """DoD: is_likely_repetition_loop==True → окно НЕ эмитится, dropped_windows
    инкрементится, лог пишется — без утечки самого текста в лог."""

    def test_loop_window_not_emitted(self) -> None:
        svc = _make_service(stt_text=LOOP_TEXT)
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
            self.assertTrue(svc.wait_until_idle(timeout=3.0))
        mock_bus.emit_typed.assert_not_called()
        svc.close()

    def test_loop_window_increments_dropped_counter(self) -> None:
        svc = _make_service(stt_text=LOOP_TEXT)
        self.assertEqual(svc.dropped_windows, 0)

        svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=3.0))

        self.assertEqual(svc.dropped_windows, 1, "loop-дроп обязан использовать ТОТ ЖЕ счётчик, что и F3 backpressure")
        svc.close()

    def test_loop_window_logs_warning_without_leaking_text(self) -> None:
        svc = _make_service(stt_text=LOOP_TEXT)
        with self.assertLogs("KrabEar.Backend.LiveSubsService", level="WARNING") as cm:
            svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
            self.assertTrue(svc.wait_until_idle(timeout=3.0))

        loop_logs = [line for line in cm.output if "зациклен" in line.lower()]
        self.assertEqual(len(loop_logs), 1, f"ожидался ровно один лог дропа, получено: {cm.output}")
        # W1770 MED (PII): сам зациклённый текст не должен попасть в лог.
        joined = "\n".join(cm.output).lower()
        self.assertNotIn("сохраняй", joined)
        self.assertNotIn("смысл", joined)
        svc.close()

    def test_normal_window_still_emitted(self) -> None:
        """Регресс-гвард: обычный (не зацикленный) текст по-прежнему эмитится."""
        svc = _make_service(stt_text="Обычный текст без повторов.")
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_bytes(3.0), 16000, "off", False)
            self.assertTrue(svc.wait_until_idle(timeout=3.0))
        mock_bus.emit_typed.assert_called_once()
        self.assertEqual(svc.dropped_windows, 0)
        svc.close()

    def test_loop_window_result_has_empty_text(self) -> None:
        """Синхронный is_final путь тоже не должен вернуть мусорный текст вызывающему."""
        svc = _make_service(stt_text=LOOP_TEXT)
        result = svc.ingest(_pcm_bytes(3.0), 16000, "off", True)
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "")
        self.assertIsNone(result["translation"])
        svc.close()


# ── G2: путь диктовки (AudioEngine напрямую) НЕ затронут ──────────────────────

class DictationPathUnaffectedByLoopDropTests(unittest.TestCase):
    """G2-гейт живёт ТОЛЬКО в LiveSubsService. AudioEngine (дорога диктовки) по-
    прежнему возвращает зацикленный текст НЕИЗМЕНЁННЫМ — engine.py:~1138,
    «не врём про input»: пользователь видит реальный вывод Whisper и решает,
    перезаписать ли фразу."""

    @patch("core.engine.AudioEngine._maybe_run_diarization")
    @patch("core.engine.AudioEngine._transcribe_with_fallback")
    def test_dictation_returns_loop_text_unmodified(self, mock_fallback, mock_diar):
        from core.engine import AudioEngine
        mock_fallback.return_value = _make_whisper_result(LOOP_TEXT)
        mock_diar.return_value = None
        engine = AudioEngine(skip_gigaam_warmup=True)

        result = engine.transcribe("fake.wav", is_preview=False)

        self.assertEqual(result["raw_text"], LOOP_TEXT)


if __name__ == "__main__":
    unittest.main()
