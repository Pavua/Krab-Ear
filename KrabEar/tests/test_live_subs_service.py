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

    tr_result = MagicMock()
    tr_result.translated_text = translated
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
    """Тесты flush-логики."""

    def test_flush_on_3s_boundary(self) -> None:
        """Чанк ≥3 с → возвращает результат с text."""
        svc = _make_service(stt_text="world")
        result = svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "world")

    def test_flush_on_is_final_true(self) -> None:
        """is_final=True → немедленный flush даже при малом буфере."""
        svc = _make_service(stt_text="final text")
        result = svc.ingest(_pcm_chunk(0.5), 16000, "ru", True)
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "final text")

    def test_flush_resets_buffer(self) -> None:
        """После flush буфер обнуляется."""
        svc = _make_service()
        svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
        self.assertAlmostEqual(svc.buffer_duration_sec(16000), 0.0, places=2)

    def test_flush_returns_translation(self) -> None:
        """Flush включает перевод при указанном target_lang."""
        svc = _make_service(stt_text="hello", translated="привет")
        result = svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
        self.assertIsNotNone(result)
        self.assertEqual(result["translation"], "привет")

    def test_flush_no_translation_when_target_off(self) -> None:
        """target_lang='off' → translation=None, translator не вызывается."""
        svc = _make_service()
        result = svc.ingest(_pcm_chunk(3.0), 16000, "off", False)
        self.assertIsNotNone(result)
        self.assertIsNone(result["translation"])
        svc._translator.translate.assert_not_called()


class TestLiveSubsEventBus(unittest.TestCase):
    """Тесты эмита EventBus при flush."""

    def test_event_emitted_on_flush(self) -> None:
        """При flush emit_typed вызывается ровно один раз."""
        svc = _make_service(stt_text="bus test")
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
            mock_bus.emit_typed.assert_called_once()

    def test_event_type_is_live_subs_result(self) -> None:
        """Тип события — LIVE_SUBS_RESULT."""
        from contracts.registry import EventType
        svc = _make_service()
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_chunk(3.0), 16000, "ru", False)
            call_args = mock_bus.emit_typed.call_args
            self.assertEqual(call_args[0][0], EventType.LIVE_SUBS_RESULT)


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

    def test_handle_ingest_flushed(self) -> None:
        """handle_ingest: чанк ≥3 с → status=flushed, содержит text."""
        svc = _make_service(stt_text="ipc test")
        params = {
            "audio_chunk": _b64_chunk(3.0),
            "target_lang": "ru",
            "sample_rate": 16000,
            "is_final": False,
        }
        result = svc.handle_ingest(params)
        self.assertEqual(result["status"], "flushed")
        self.assertEqual(result["text"], "ipc test")

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
