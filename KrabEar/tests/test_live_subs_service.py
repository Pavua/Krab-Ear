"""Unit тесты для LiveSubsService (Sprint 2B).

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_live_subs_service.py -v
"""

from __future__ import annotations

import base64
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# ── path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np

from backend.live_subs_service import LiveSubsService
from backend.translator import TranslationResult


# ── helpers ───────────────────────────────────────────────────────────────────

def _pcm_chunk(duration_sec: float, sample_rate: int = 16000) -> bytes:
    """Генерирует PCM int16 нули заданной длительности."""
    n = int(duration_sec * sample_rate)
    return (np.zeros(n, dtype=np.int16)).tobytes()


def _b64_chunk(duration_sec: float, sample_rate: int = 16000) -> str:
    return base64.b64encode(_pcm_chunk(duration_sec, sample_rate)).decode()


def _make_service(stt_text: str = "hello", translated: str = "привет") -> LiveSubsService:
    """Фабрика LiveSubsService с мок-зависимостями."""
    transcriber = MagicMock()
    transcriber.transcribe.return_value = {"text": stt_text, "language": "en"}

    tr_result = TranslationResult(
        text=translated,
        status="ok",
        source_lang="en",
        target_lang="ru",
        mode="ru",
        engine="stub",
    )
    translator = MagicMock()
    translator.translate.return_value = tr_result

    return LiveSubsService(transcriber=transcriber, translator=translator)


# ── тесты ─────────────────────────────────────────────────────────────────────

class TestLiveSubsBuffer(unittest.TestCase):
    """Тесты буферизации без flush."""

    def test_ingest_short_chunk_no_flush(self) -> None:
        """Чанк <3 с → статус accepted, flush не происходит."""
        svc = _make_service()
        result = svc.ingest(
            audio_bytes=_pcm_chunk(1.0),
            sample_rate=16000,
            target_lang="ru",
            is_final=False,
        )
        self.assertIsNone(result)

    def test_buffer_duration_accumulates(self) -> None:
        """Буфер накапливает длительность."""
        svc = _make_service()
        svc.ingest(_pcm_chunk(1.0), 16000, "ru", False)
        svc.ingest(_pcm_chunk(1.0), 16000, "ru", False)
        self.assertAlmostEqual(svc.buffer_duration_sec(16000), 2.0, places=1)

    def test_two_chunks_below_threshold_no_flush(self) -> None:
        """Два чанка по 1 с (итого 2 с) не вызывают flush."""
        svc = _make_service()
        r1 = svc.ingest(_pcm_chunk(1.0), 16000, "ru", False)
        r2 = svc.ingest(_pcm_chunk(1.0), 16000, "ru", False)
        self.assertIsNone(r1)
        self.assertIsNone(r2)


class TestLiveSubsFlush(unittest.TestCase):
    """Тесты flush-логики.

    F3 (2026-08-12, backpressure): non-final threshold-flush больше не
    выполняет STT синхронно в вызывающем треде — ingest() кладёт снапшот в
    фоновый воркер и возвращается немедленно (None). Тесты, проверяющие
    СОДЕРЖИМОЕ результата non-final flush, ждут воркер детерминированно через
    wait_until_idle() и читают white-box _completed_result (тот же стиль
    прямого доступа к внутренностям, что уже используется в этом файле для
    _buffer/_lock/_translator). is_final=True остаётся синхронным (см.
    test_flush_on_is_final_true) — контракт не менялся.
    """

    def test_flush_on_3s_boundary(self) -> None:
        """Чанк ≥3 с → ingest() возвращается немедленно (None), воркер флашит асинхронно."""
        svc = _make_service(stt_text="world")
        result = svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
        self.assertIsNone(result, "non-final flush теперь асинхронный — ingest() не блокирует")
        self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
        self.assertIsNotNone(svc._completed_result)
        self.assertEqual(svc._completed_result["text"], "world")
        svc.close()

    def test_flush_on_is_final_true(self) -> None:
        """is_final=True → немедленный СИНХРОННЫЙ flush даже при малом буфере (контракт сохранён)."""
        svc = _make_service(stt_text="final text")
        result = svc.ingest(_pcm_chunk(0.5), 16000, "ru", True)
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "final text")
        svc.close()

    def test_flush_resets_buffer(self) -> None:
        """После flush буфер обнуляется — синхронно, независимо от асинхронности STT."""
        svc = _make_service()
        svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
        self.assertAlmostEqual(svc.buffer_duration_sec(16000), 0.0, places=2)
        svc.close()

    def test_flush_returns_translation(self) -> None:
        """Flush включает перевод при указанном target_lang (проверено после wait_until_idle)."""
        svc = _make_service(stt_text="hello", translated="привет")
        svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0))
        self.assertIsNotNone(svc._completed_result)
        self.assertEqual(svc._completed_result["translation"], "привет")
        svc.close()

    def test_flush_no_translation_when_target_off(self) -> None:
        """target_lang='off' → translation=None, translator не вызывается."""
        svc = _make_service()
        svc.ingest(_pcm_chunk(3.0), 16000, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0))
        self.assertIsNotNone(svc._completed_result)
        self.assertIsNone(svc._completed_result["translation"])
        svc._translator.translate.assert_not_called()
        svc.close()


class TestLiveSubsEventBus(unittest.TestCase):
    """Тесты эмита EventBus при flush.

    F3: emit_typed теперь вызывается из фонового воркера, а не синхронно
    внутри ingest() — ждём wait_until_idle() ДО проверки мока, иначе
    ассерт мог бы выполниться раньше, чем воркер успел обработать окно.
    """

    def test_event_emitted_on_flush(self) -> None:
        """При flush emit_typed вызывается ровно один раз (после того, как воркер догнал очередь)."""
        svc = _make_service(stt_text="bus test")
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
            self.assertTrue(svc.wait_until_idle(timeout=2.0))
            mock_bus.emit_typed.assert_called_once()
        svc.close()

    def test_event_type_is_live_subs_result(self) -> None:
        """Тип события — LIVE_SUBS_RESULT."""
        from contracts.registry import EventType
        svc = _make_service()
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
            self.assertTrue(svc.wait_until_idle(timeout=2.0))
            call_args = mock_bus.emit_typed.call_args
            self.assertEqual(call_args[0][0], EventType.LIVE_SUBS_RESULT)
        svc.close()


class TestLiveSubsStop(unittest.TestCase):
    """Тесты handle_stop."""

    def test_stop_resets_buffer(self) -> None:
        """stop() очищает буфер."""
        svc = _make_service()
        svc.ingest(_pcm_chunk(1.5), 16000, "ru", False)
        svc.stop()
        self.assertEqual(svc.buffer_duration_sec(16000), 0.0)

    def test_stop_returns_stopped_status(self) -> None:
        """stop() возвращает {'status': 'stopped', 'flushed': True/False}."""
        svc = _make_service()
        svc.ingest(_pcm_chunk(1.0), 16000, "ru", False)
        result = svc.stop()
        self.assertEqual(result["status"], "stopped")
        self.assertIn("flushed", result)

    def test_stop_on_empty_buffer(self) -> None:
        """stop() без данных в буфере — flushed=False."""
        svc = _make_service()
        result = svc.stop()
        self.assertFalse(result["flushed"])


class TestLiveSubsIPCHandlers(unittest.TestCase):
    """Тесты IPC-обёрток."""

    def test_handle_ingest_accepted(self) -> None:
        """handle_ingest: короткий чанк → status=accepted."""
        svc = _make_service()
        params = {
            "audio_chunk": _b64_chunk(1.0),
            "target_lang": "ru",
            "sample_rate": 16000,
            "is_final": False,
        }
        result = svc.handle_ingest(params)
        self.assertEqual(result["status"], "accepted")
        self.assertIn("buffer_duration_sec", result)

    def test_handle_ingest_non_final_threshold_is_queued_not_flushed(self) -> None:
        """F3: non-final чанк ≥3 с → status=accepted немедленно (flush уходит в фон).

        Ранее (до F3) handle_ingest выполнял STT синхронно в этом же IPC-треде
        и возвращал status=flushed с текстом сразу. Теперь STT никогда не
        блокирует IPC-хендлер — воркер обработает окно асинхронно, а текст
        уходит наружу только через EventBus (live_subs.result).
        """
        svc = _make_service(stt_text="ipc test")
        params = {
            "audio_chunk": _b64_chunk(3.0),
            "target_lang": "ru",
            "sample_rate": 16000,
            "is_final": False,
        }
        result = svc.handle_ingest(params)
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
        self.assertIsNotNone(svc._completed_result)
        self.assertEqual(svc._completed_result["text"], "ipc test")
        svc.close()

    def test_handle_ingest_is_final_flushed(self) -> None:
        """handle_ingest: is_final=True → status=flushed синхронно, содержит text (контракт сохранён)."""
        svc = _make_service(stt_text="ipc final test")
        params = {
            "audio_chunk": _b64_chunk(0.5),
            "target_lang": "ru",
            "sample_rate": 16000,
            "is_final": True,
        }
        result = svc.handle_ingest(params)
        self.assertEqual(result["status"], "flushed")
        self.assertEqual(result["text"], "ipc final test")
        svc.close()

    def test_handle_ingest_invalid_base64(self) -> None:
        """handle_ingest: невалидный base64 → ValueError."""
        svc = _make_service()
        params = {
            "audio_chunk": "not-valid-base64!!!",
            "target_lang": "ru",
            "sample_rate": 16000,
            "is_final": False,
        }
        with self.assertRaises(ValueError):
            svc.handle_ingest(params)

    def test_handle_stop(self) -> None:
        """handle_stop возвращает ожидаемую структуру."""
        svc = _make_service()
        result = svc.handle_stop({})
        self.assertEqual(result["status"], "stopped")


class TestLiveSubsTranslationAttribute(unittest.TestCase):
    """Regression W1740 — TranslationResult.text was accessed as .translated_text."""

    def test_translation_flows_through_to_result(self) -> None:
        """Flush with target_lang set must deliver translated text, NOT None.

        Before the fix: tr.translated_text raised AttributeError on the real
        TranslationResult dataclass (field is .text, not .translated_text).
        The except clause swallowed it → translation=None silently every time.

        F3 (2026-08-12): non-final flush больше не синхронный — ждём
        wait_until_idle() и читаем white-box _completed_result вместо
        прямого return-значения ingest().
        """
        svc = _make_service(stt_text="hello world", translated="привет мир")
        svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
        result = svc._completed_result
        self.assertIsNotNone(result, "flush должен вернуть результат")
        self.assertEqual(
            result["translation"],
            "привет мир",
            "translation должен содержать переведённый текст, а не None",
        )
        svc.close()

    def test_translation_is_not_none_on_valid_target_lang(self) -> None:
        """translation в ответе никогда не None при валидном target_lang и тексте."""
        svc = _make_service(stt_text="test sentence", translated="тестовое предложение")
        svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0))
        result = svc._completed_result
        self.assertIsNotNone(result)
        self.assertIsNotNone(
            result["translation"],
            "translation не должен быть None когда STT вернул текст и target_lang задан",
        )
        svc.close()

    def test_real_translation_result_attribute_accessible(self) -> None:
        """TranslationResult.text существует и доступен (контракт dataclass)."""
        tr = TranslationResult(
            text="translated value",
            status="ok",
            source_lang="en",
            target_lang="ru",
            mode="ru",
            engine="stub",
        )
        # .text должен работать
        self.assertEqual(tr.text, "translated value")
        # .translated_text НЕ существует в slots=True dataclass
        with self.assertRaises(AttributeError):
            _ = tr.translated_text  # type: ignore[attr-defined]

    def test_network_mode_offline_default_passed_to_translator(self) -> None:
        """translator.translate должен получать network_mode='offline_default', не 'offline'."""
        svc = _make_service(stt_text="check mode", translated="проверка режима")
        svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
        call_kwargs = svc._translator.translate.call_args
        self.assertIsNotNone(call_kwargs)
        network_mode_passed = call_kwargs.kwargs.get(
            "network_mode", call_kwargs.args[2] if len(call_kwargs.args) > 2 else None
        )
        self.assertEqual(
            network_mode_passed,
            "offline_default",
            "network_mode должен быть 'offline_default', не 'offline'",
        )
        svc.close()


class TestLiveSubsIsolation(unittest.TestCase):
    """Тесты изоляции состояния между экземплярами."""

    def test_multiple_instances_isolated(self) -> None:
        """Два экземпляра сервиса не делят буфер."""
        svc1 = _make_service(stt_text="one")
        svc2 = _make_service(stt_text="two")
        svc1.ingest(_pcm_chunk(1.5), 16000, "ru", False)
        # svc2 буфер должен оставаться пустым
        self.assertEqual(svc2.buffer_duration_sec(16000), 0.0)
        # svc1 буфер должен иметь данные
        self.assertGreater(svc1.buffer_duration_sec(16000), 0.0)


if __name__ == "__main__":
    unittest.main()
