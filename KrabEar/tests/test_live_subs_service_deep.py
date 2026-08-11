"""Wave 182 — deep edge-case тесты LiveSubsService.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_live_subs_service_deep.py -v
"""

from __future__ import annotations

import base64
import sys
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

# ── path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np

from backend.live_subs_service import LiveSubsService, _FLUSH_THRESHOLD_SEC
from backend.translator import TranslationResult


# ── helpers ───────────────────────────────────────────────────────────────────

def _pcm_bytes(duration_sec: float, sample_rate: int = 16000, value: int = 0) -> bytes:
    """Генерирует PCM int16 байты заданной длительности и значения."""
    n = int(duration_sec * sample_rate)
    return np.full(n, value, dtype=np.int16).tobytes()


def _b64(duration_sec: float, sample_rate: int = 16000, value: int = 0) -> str:
    return base64.b64encode(_pcm_bytes(duration_sec, sample_rate, value)).decode()


def _make_service(
    stt_text: str = "hello",
    translated: str = "привет",
    translate_raises: bool = False,
) -> LiveSubsService:
    transcriber = MagicMock()
    transcriber.transcribe.return_value = {"text": stt_text, "language": "en"}

    translator = MagicMock()
    if translate_raises:
        translator.translate.side_effect = RuntimeError("translation failed")
    else:
        tr_result = TranslationResult(
            text=translated,
            status="ok",
            source_lang="en",
            target_lang="ru",
            mode="ru",
            engine="stub",
        )
        translator.translate.return_value = tr_result

    return LiveSubsService(transcriber=transcriber, translator=translator)


# ── тесты ─────────────────────────────────────────────────────────────────────

class TestIngestBase64Decoding(unittest.TestCase):
    """test_ingest_base64_pcm_decoded_correctly."""

    def test_ingest_base64_pcm_decoded_correctly(self) -> None:
        """PCM int16 байты корректно декодируются в float32 [-1, 1]."""
        # Создаём сигнал: max positive int16 = 32767 → float32 ≈ 1.0
        n = 16000  # 1 секунда
        pcm = np.full(n, 32767, dtype=np.int16)
        raw_bytes = pcm.tobytes()

        result = LiveSubsService._decode_audio(raw_bytes, sample_rate=16000)

        self.assertEqual(result.dtype, np.float32)
        self.assertEqual(len(result), n)
        self.assertAlmostEqual(float(result[0]), 32767 / 32768.0, places=4)
        # Все значения в диапазоне [-1, 1]
        self.assertTrue(np.all(result >= -1.0))
        self.assertTrue(np.all(result <= 1.0))


class TestInvalidBase64(unittest.TestCase):
    """test_invalid_base64_handled_gracefully."""

    def test_invalid_base64_handled_gracefully(self) -> None:
        """Невалидный base64 в handle_ingest выбрасывает ValueError."""
        svc = _make_service()
        params = {
            "audio_chunk": "!!!not-base64!!!",
            "target_lang": "ru",
            "sample_rate": 16000,
            "is_final": False,
        }
        with self.assertRaises(ValueError) as ctx:
            svc.handle_ingest(params)
        self.assertIn("invalid base64", str(ctx.exception))

    def test_invalid_base64_message_contains_detail(self) -> None:
        """ValueError содержит информативное сообщение об ошибке."""
        svc = _make_service()
        # Строка с некорректным padding (нечётная длина без '=') → binascii.Error
        params = {"audio_chunk": "!!!not-valid-base64!!!!!", "target_lang": "ru", "sample_rate": 16000, "is_final": False}
        with self.assertRaises(ValueError) as ctx:
            svc.handle_ingest(params)
        self.assertIn("audio_chunk", str(ctx.exception))


class TestAccumulateBelowThreshold(unittest.TestCase):
    """test_accumulate_below_3s_no_flush."""

    def test_accumulate_below_3s_no_flush(self) -> None:
        """Несколько чанков суммарно <3 с — flush не происходит."""
        svc = _make_service()
        durations = [0.5, 0.8, 0.9]  # итого 2.2 с < 3 с
        for d in durations:
            result = svc.ingest(_pcm_bytes(d), 16000, "ru", False)
            self.assertIsNone(result, f"Неожиданный flush при накоплении {d}с")

    def test_buffer_grows_correctly(self) -> None:
        """buffer_duration_sec корректно отражает накопленные данные."""
        svc = _make_service()
        svc.ingest(_pcm_bytes(0.5), 16000, "ru", False)
        svc.ingest(_pcm_bytes(1.0), 16000, "ru", False)
        svc.ingest(_pcm_bytes(0.7), 16000, "ru", False)
        self.assertAlmostEqual(svc.buffer_duration_sec(16000), 2.2, places=1)

    def test_transcriber_not_called_without_flush(self) -> None:
        """STT не вызывается до flush."""
        svc = _make_service()
        svc.ingest(_pcm_bytes(1.0), 16000, "ru", False)
        svc._transcriber.transcribe.assert_not_called()


class TestAccumulateAboveThreshold(unittest.TestCase):
    """test_accumulate_above_3s_triggers_flush.

    F3 (2026-08-12): non-final threshold-flush больше не выполняет STT
    синхронно — ingest() возвращается немедленно (None), а результат
    появляется в white-box _completed_result после wait_until_idle().
    """

    def test_accumulate_above_3s_triggers_flush(self) -> None:
        """Накопление ≥3 с автоматически запускает (асинхронный) flush."""
        svc = _make_service(stt_text="auto flush")
        result = svc.ingest(_pcm_bytes(_FLUSH_THRESHOLD_SEC), 16000, "ru", False)
        self.assertIsNone(result, "non-final flush асинхронный — ingest() не блокирует")
        self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
        self.assertIsNotNone(svc._completed_result)
        self.assertEqual(svc._completed_result["text"], "auto flush")
        svc.close()

    def test_flush_resets_buffer_to_zero(self) -> None:
        """После flush буфер обнуляется (синхронно, независимо от асинхронности STT)."""
        svc = _make_service()
        svc.ingest(_pcm_bytes(_FLUSH_THRESHOLD_SEC), 16000, "ru", False)
        self.assertAlmostEqual(svc.buffer_duration_sec(16000), 0.0, places=5)
        svc.close()

    def test_multiple_chunks_trigger_flush_at_boundary(self) -> None:
        """Два чанка по 1.6 с → второй пересекает границу и запускает (асинхронный) flush."""
        svc = _make_service(stt_text="crossed boundary")
        r1 = svc.ingest(_pcm_bytes(1.6), 16000, "ru", False)
        self.assertIsNone(r1)
        r2 = svc.ingest(_pcm_bytes(1.6), 16000, "ru", False)
        self.assertIsNone(r2, "non-final flush асинхронный — ingest() не блокирует")
        self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
        self.assertIsNotNone(svc._completed_result)
        self.assertEqual(svc._completed_result["text"], "crossed boundary")
        svc.close()


class TestIsFinalFlag(unittest.TestCase):
    """test_is_final_flag_triggers_immediate_flush."""

    def test_is_final_flag_triggers_immediate_flush(self) -> None:
        """is_final=True вызывает flush даже при малом буфере (<3 с)."""
        svc = _make_service(stt_text="final chunk")
        result = svc.ingest(_pcm_bytes(0.1), 16000, "ru", True)
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "final chunk")

    def test_is_final_zero_length_chunk(self) -> None:
        """is_final=True с нулевым буфером → возвращает пустой текст."""
        svc = _make_service(stt_text="")
        result = svc.ingest(b"", 16000, "ru", True)
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "")

    def test_is_final_resets_buffer(self) -> None:
        """После is_final flush буфер обнуляется."""
        svc = _make_service()
        svc.ingest(_pcm_bytes(0.5), 16000, "ru", False)
        svc.ingest(_pcm_bytes(0.3), 16000, "ru", True)
        self.assertAlmostEqual(svc.buffer_duration_sec(16000), 0.0, places=5)


class TestEmptyChunk(unittest.TestCase):
    """test_empty_chunk_handled."""

    def test_empty_chunk_handled(self) -> None:
        """Пустые байты (len=0) не вызывают ошибку, не влияют на буфер."""
        svc = _make_service()
        result = svc.ingest(b"", 16000, "ru", False)
        self.assertIsNone(result)
        self.assertAlmostEqual(svc.buffer_duration_sec(16000), 0.0, places=5)

    def test_empty_bytes_decode_to_zero_array(self) -> None:
        """_decode_audio(b'') → пустой float32 массив."""
        arr = LiveSubsService._decode_audio(b"", sample_rate=16000)
        self.assertEqual(len(arr), 0)
        self.assertEqual(arr.dtype, np.float32)

    def test_single_byte_trimmed_to_zero(self) -> None:
        """Нечётное число байт обрезается: 1 байт → 0 сэмплов."""
        arr = LiveSubsService._decode_audio(b"\x01", sample_rate=16000)
        self.assertEqual(len(arr), 0)


class TestConcurrentIngest(unittest.TestCase):
    """test_concurrent_ingest_thread_safe."""

    def test_concurrent_ingest_thread_safe(self) -> None:
        """Конкурентный вызов ingest из нескольких потоков не падает с исключением."""
        svc = _make_service(stt_text="concurrent")
        errors: list[Exception] = []

        def worker(chunk_sec: float) -> None:
            try:
                svc.ingest(_pcm_bytes(chunk_sec), 16000, "ru", False)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(0.3,)) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Exceptions в потоках: {errors}")

    def test_concurrent_handle_ingest_no_crash(self) -> None:
        """handle_ingest из 5 потоков одновременно — без падений."""
        svc = _make_service()
        errors: list[Exception] = []
        params = {
            "audio_chunk": _b64(0.4),
            "target_lang": "ru",
            "sample_rate": 16000,
            "is_final": False,
        }

        def worker() -> None:
            try:
                svc.handle_ingest(dict(params))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Exceptions в потоках: {errors}")


class TestEmitsLiveSubsResult(unittest.TestCase):
    """test_emits_live_subs_result_via_eventbus."""

    def test_emits_live_subs_result_via_eventbus(self) -> None:
        """При flush emit_typed вызывается с EventType.LIVE_SUBS_RESULT.

        F3 (2026-08-12): emit_typed вызывается из фонового воркера — ждём
        wait_until_idle() ВНУТРИ блока patch, иначе ассерт может выполниться
        раньше воркера (гонка).
        """
        from contracts.registry import EventType
        from contracts.live_subs_events import LiveSubsResult

        svc = _make_service(stt_text="event test")
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_bytes(_FLUSH_THRESHOLD_SEC), 16000, "ru", False)
            self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
            mock_bus.emit_typed.assert_called_once()
            event_type_arg, payload_arg = mock_bus.emit_typed.call_args[0]
            self.assertEqual(event_type_arg, EventType.LIVE_SUBS_RESULT)
            self.assertIsInstance(payload_arg, LiveSubsResult)
        svc.close()

    def test_emits_correct_text_in_payload(self) -> None:
        """Payload содержит корректный text из STT."""
        svc = _make_service(stt_text="payload check")
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_bytes(_FLUSH_THRESHOLD_SEC), 16000, "ru", False)
            self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
            _, payload = mock_bus.emit_typed.call_args[0]
            self.assertEqual(payload.text, "payload check")
        svc.close()

    def test_no_emit_without_flush(self) -> None:
        """emit_typed не вызывается, пока буфер не достиг порога."""
        svc = _make_service()
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_bytes(1.0), 16000, "ru", False)
            mock_bus.emit_typed.assert_not_called()


class TestTranslationFailureContinues(unittest.TestCase):
    """test_translation_failure_continues_with_stt_only."""

    def test_translation_failure_continues_with_stt_only(self) -> None:
        """Ошибка перевода не прерывает flush: text возвращается, translation=None."""
        svc = _make_service(stt_text="stt only", translate_raises=True)
        svc.ingest(_pcm_bytes(_FLUSH_THRESHOLD_SEC), 16000, "ru", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
        result = svc._completed_result
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "stt only")
        self.assertIsNone(result["translation"])
        svc.close()

    def test_translation_failure_event_still_emitted(self) -> None:
        """Даже при ошибке перевода событие EventBus эмитируется."""
        svc = _make_service(stt_text="emit anyway", translate_raises=True)
        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.ingest(_pcm_bytes(_FLUSH_THRESHOLD_SEC), 16000, "ru", False)
            self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
            mock_bus.emit_typed.assert_called_once()
        svc.close()

    def test_translation_not_called_when_target_is_none(self) -> None:
        """target_lang='none' → translator.translate не вызывается."""
        svc = _make_service()
        svc.ingest(_pcm_bytes(_FLUSH_THRESHOLD_SEC), 16000, "none", False)
        svc._translator.translate.assert_not_called()

    def test_translation_not_called_when_text_empty(self) -> None:
        """Пустой STT результат → translate не вызывается."""
        svc = _make_service(stt_text="")
        svc.ingest(_pcm_bytes(_FLUSH_THRESHOLD_SEC), 16000, "ru", False)
        svc._translator.translate.assert_not_called()


class TestUnicodeTextInSubtitle(unittest.TestCase):
    """test_unicode_text_in_subtitle."""

    def test_unicode_text_in_subtitle(self) -> None:
        """Unicode (кириллица, иероглифы, emoji) корректно возвращается."""
        unicode_texts = [
            "Привет, мир! 🌍",
            "你好世界",
            "Héllo wörld — café",
            "Тест ендо строки",
        ]
        for text in unicode_texts:
            with self.subTest(text=text):
                svc = _make_service(stt_text=text)
                result = svc.ingest(_pcm_bytes(_FLUSH_THRESHOLD_SEC), 16000, "ru", True)
                self.assertIsNotNone(result)
                self.assertEqual(result["text"], text)

    def test_unicode_translation_returned(self) -> None:
        """Unicode перевод корректно передаётся в результате."""
        svc = _make_service(stt_text="hello", translated="Привет, мир! 🌏")
        svc.ingest(_pcm_bytes(_FLUSH_THRESHOLD_SEC), 16000, "ru", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
        result = svc._completed_result
        self.assertIsNotNone(result)
        self.assertEqual(result["translation"], "Привет, мир! 🌏")
        svc.close()


class TestBufferGrowsCorrectly(unittest.TestCase):
    """test_buffer_grows_correctly (дополнительные случаи)."""

    def test_buffer_grows_correctly_multiple_appends(self) -> None:
        """Буфер точно отражает суммарную длительность после N appends."""
        svc = _make_service()
        chunks = [0.1, 0.2, 0.3, 0.4, 0.5]
        expected_total = sum(chunks)
        for d in chunks:
            result = svc.ingest(_pcm_bytes(d), 16000, "ru", False)
            self.assertIsNone(result, f"Неожиданный flush при d={d}")
        self.assertAlmostEqual(svc.buffer_duration_sec(16000), expected_total, places=1)

    def test_buffer_sample_count_matches_duration(self) -> None:
        """Количество накопленных сэмплов соответствует длительности."""
        svc = _make_service()
        sr = 16000
        svc.ingest(_pcm_bytes(1.0, sr), sr, "ru", False)
        svc.ingest(_pcm_bytes(1.5, sr), sr, "ru", False)
        expected_samples = int(2.5 * sr)
        self.assertEqual(svc._buffer_samples, expected_samples)

    def test_buffer_empty_after_stop(self) -> None:
        """После stop() буфер и счётчик обнуляются."""
        svc = _make_service()
        for _ in range(3):
            svc.ingest(_pcm_bytes(0.5), 16000, "ru", False)
        svc.stop()
        self.assertEqual(svc._buffer_samples, 0)
        self.assertEqual(svc._buffer, [])


class TestChunkFormatValidation(unittest.TestCase):
    """test_chunk_format_validation."""

    def test_chunk_format_validation_odd_bytes_trimmed(self) -> None:
        """Нечётное число байт → последний байт отрезается, не ValueError."""
        odd_bytes = b"\x00\x01\x02"  # 3 байта → 1 сэмпл int16 + 1 обрезанный
        arr = LiveSubsService._decode_audio(odd_bytes, 16000)
        self.assertEqual(len(arr), 1)  # 2 байта = 1 int16

    def test_chunk_format_exactly_two_bytes(self) -> None:
        """Ровно 2 байта → 1 float32 сэмпл."""
        arr = LiveSubsService._decode_audio(b"\x00\x40", 16000)
        self.assertEqual(len(arr), 1)
        self.assertEqual(arr.dtype, np.float32)

    def test_chunk_format_large_buffer(self) -> None:
        """Большой буфер (10 с) корректно декодируется без ошибок."""
        big_bytes = _pcm_bytes(10.0)
        arr = LiveSubsService._decode_audio(big_bytes, 16000)
        self.assertEqual(len(arr), 10 * 16000)
        self.assertEqual(arr.dtype, np.float32)

    def test_handle_ingest_defaults_to_16000_sample_rate(self) -> None:
        """Отсутствие sample_rate в params → используется 16000 по умолчанию."""
        svc = _make_service()
        params = {
            "audio_chunk": _b64(1.0),
            "target_lang": "ru",
            "is_final": False,
            # sample_rate намеренно пропущен
        }
        result = svc.handle_ingest(params)
        self.assertEqual(result["status"], "accepted")
        self.assertAlmostEqual(result["buffer_duration_sec"], 1.0, places=1)

    def test_handle_ingest_is_final_default_false(self) -> None:
        """Отсутствие is_final в params → по умолчанию False, flush не происходит."""
        svc = _make_service()
        params = {
            "audio_chunk": _b64(0.5),
            "target_lang": "ru",
            "sample_rate": 16000,
            # is_final намеренно пропущен
        }
        result = svc.handle_ingest(params)
        self.assertEqual(result["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
