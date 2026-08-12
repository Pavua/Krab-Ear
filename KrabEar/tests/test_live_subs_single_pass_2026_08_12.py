"""LiveSubsService: single_pass=True реально передаётся в Transcriber (2026-08-12).

Живой инцидент: окно live-субтитров длиной 2.5с прошло полную цепочку STT,
спроектированную для диктовки (GigaAM → confidence-retry на whisper-large-v3 →
whisper-large-v3-turbo) — 9.49с на окно, которое приходит каждые ~3с.
`_process_window` обязан передавать `single_pass=True` в `Transcriber.transcribe`
(класс «декоративная проводка» — компонент существует, но прод его не зовёт,
см. CLAUDE.md). Пустой результат первого движка при single_pass=True не должен
доехать до эмита события (окно не показывает субтитр, если в нём нет речи).

Спека: docs/superpowers/specs/2026-08-12-live-subs-single-pass-design.md

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_live_subs_single_pass_2026_08_12.py -v
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
    """Фабрика LiveSubsService с мок-зависимостями (тот же паттерн, что
    test_live_subs_service.py::_make_service)."""
    transcriber = MagicMock()
    transcriber.transcribe.return_value = {"text": stt_text, "language": "en"}

    tr_result = TranslationResult(
        text=translated, status="ok", source_lang="en", target_lang="ru",
        mode="ru", engine="stub",
    )
    translator = MagicMock()
    translator.translate.return_value = tr_result

    return LiveSubsService(transcriber=transcriber, translator=translator)


# ── DoD: source-контракт — _process_window реально передаёт single_pass=True ──

class ProcessWindowPassesSinglePassTest(unittest.TestCase):
    """«Декоративная проводка» класс: проверяем ФАКТИЧЕСКИЙ вызов transcriber.transcribe
    с single_pass=True, а не просто существование параметра где-то в коде."""

    def test_non_final_flush_passes_single_pass_true(self) -> None:
        svc = _make_service(stt_text="world")
        svc.ingest(_pcm_bytes(3.0), 16000, "ru", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0))

        svc._transcriber.transcribe.assert_called_once()
        _, kwargs = svc._transcriber.transcribe.call_args
        self.assertTrue(
            kwargs.get("single_pass"),
            "LiveSubsService._process_window обязан передавать single_pass=True",
        )
        svc.close()

    def test_final_flush_passes_single_pass_true(self) -> None:
        """is_final=True идёт синхронным путём — тот же контракт обязан выполняться."""
        svc = _make_service(stt_text="final text")
        svc.ingest(_pcm_bytes(0.5), 16000, "ru", True)

        _, kwargs = svc._transcriber.transcribe.call_args
        self.assertTrue(kwargs.get("single_pass"))
        svc.close()


# ── DoD: пустой результат первого движка → путь не падает, событие не эмитится ─

class EmptyResultDoesNotEmitTest(unittest.TestCase):
    """Пустой текст (single_pass=True привёл к пустому результату первого движка
    в GigaAM-пример из живого лога) → окно не должно показывать субтитр."""

    def test_empty_text_does_not_call_emit_typed(self) -> None:
        svc = _make_service(stt_text="")
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_bytes(3.0), 16000, "ru", False)
            self.assertTrue(svc.wait_until_idle(timeout=2.0))
            mock_bus.emit_typed.assert_not_called()
        svc.close()

    def test_empty_text_does_not_call_translate(self) -> None:
        """Пустой текст — переводить нечего, translator не должен вызываться."""
        svc = _make_service(stt_text="")
        svc.ingest(_pcm_bytes(3.0), 16000, "ru", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0))

        svc._translator.translate.assert_not_called()
        svc.close()

    def test_empty_text_path_does_not_crash_and_returns_well_formed_dict(self) -> None:
        svc = _make_service(stt_text="")
        svc.ingest(_pcm_bytes(3.0), 16000, "ru", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0))

        result = svc._completed_result
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "")
        self.assertIsNone(result["translation"])
        svc.close()

    def test_empty_text_final_flush_returns_well_formed_dict(self) -> None:
        """is_final=True — синхронный путь, тот же контракт: не падает, не эмитит."""
        svc = _make_service(stt_text="")
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            result = svc.ingest(_pcm_bytes(0.5), 16000, "ru", True)
            mock_bus.emit_typed.assert_not_called()
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "")
        svc.close()


# ── Регрессия: непустой текст по-прежнему эмитит событие как раньше ───────────

class NonEmptyResultStillEmitsTest(unittest.TestCase):
    """single_pass=True не должен затрагивать обычный (непустой) путь."""

    def test_non_empty_text_still_emits_event(self) -> None:
        svc = _make_service(stt_text="bus test")
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_bytes(3.0), 16000, "ru", False)
            self.assertTrue(svc.wait_until_idle(timeout=2.0))
            mock_bus.emit_typed.assert_called_once()
        svc.close()


if __name__ == "__main__":
    unittest.main()
