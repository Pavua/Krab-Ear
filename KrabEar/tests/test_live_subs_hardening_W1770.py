"""W1770 — hardening тесты LiveSubsService.

Покрывают 1 HIGH + 3 MED фикса:
  (1) HIGH — unbounded buffer: огромный sample_rate не должен раздувать буфер
      без границ; _MAX_BUFFER_SAMPLES форсирует flush. + handle_ingest клампит
      sample_rate в безопасный диапазон [_MIN_SAMPLE_RATE, _MAX_SAMPLE_RATE].
  (2) MED — PII в логах: INFO-строка flush НЕ содержит текста транскрипта/перевода.
  (3) MED — MLX под service-RLock: тяжёлый STT выполняется ВНЕ self._lock.
  (4) reset() — публичная очистка буфера под локом (для privacy-purge wiring).

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_live_subs_hardening_W1770.py \
        -p no:xdist -q --tb=short -p no:cacheprovider
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

# ── path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np  # noqa: E402

from backend.live_subs_service import (  # noqa: E402
    LiveSubsService,
    _MAX_BUFFER_SAMPLES,
    _MAX_SAMPLE_RATE,
    _MIN_SAMPLE_RATE,
)
from backend.translator import TranslationResult  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _pcm_bytes(n_samples: int) -> bytes:
    """n_samples нулей int16 → сырые PCM-байты."""
    return np.zeros(n_samples, dtype=np.int16).tobytes()


def _b64_samples(n_samples: int) -> str:
    return base64.b64encode(_pcm_bytes(n_samples)).decode()


def _make_service(stt_text: str = "hello", translated: str = "привет") -> LiveSubsService:
    """LiveSubsService с мок-зависимостями (без реального MLX)."""
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


# ── (1) HIGH: unbounded buffer / OOM ──────────────────────────────────────────

class TestBufferCapForcesFlush(unittest.TestCase):
    """HIGH: огромный sample_rate не должен раздувать буфер без границ."""

    def test_giant_sample_rate_does_not_grow_buffer_past_cap(self) -> None:
        """sample_rate=1e9, is_final=False, повторно → буфер не превышает потолок.

        FAIL-BEFORE: без _MAX_BUFFER_SAMPLES buffer_sec≈0 навсегда, flush никогда не
        срабатывает, _buffer_samples растёт линейно с числом чанков → OOM.
        PASS-AFTER: потолок форсирует flush, _buffer_samples сбрасывается.
        """
        svc = _make_service(stt_text="")  # пустой STT → flush без побочных эффектов
        giant_sr = 1_000_000_000
        # Чанк ~ половина потолка, чтобы второй ingest гарантированно пересёк границу.
        chunk_samples = _MAX_BUFFER_SAMPLES // 2 + 1

        max_seen = 0
        for _ in range(6):
            svc.ingest(
                audio_bytes=_pcm_bytes(chunk_samples),
                sample_rate=giant_sr,
                target_lang="off",
                is_final=False,
            )
            with svc._lock:
                max_seen = max(max_seen, svc._buffer_samples)

        # Буфер НИКОГДА не должен превысить потолок более чем на один чанк.
        self.assertLessEqual(
            max_seen,
            _MAX_BUFFER_SAMPLES + chunk_samples,
            "Буфер вырос выше потолка — OOM-защита не сработала (W1770 HIGH)",
        )

    def test_buffer_cap_triggers_flush_via_transcriber(self) -> None:
        """При достижении потолка STT (transcribe) вызывается — значит был flush.

        F3 (2026-08-12): flush теперь асинхронный (фоновый воркер) — ждём
        wait_until_idle() перед проверкой мока, иначе ассерт мог бы
        выполниться раньше, чем воркер успел вызвать transcribe().
        """
        svc = _make_service(stt_text="")
        giant_sr = 1_000_000_000
        chunk_samples = _MAX_BUFFER_SAMPLES + 1  # одиночный чанк сразу пересекает потолок

        svc.ingest(_pcm_bytes(chunk_samples), giant_sr, "off", False)
        self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")

        svc._transcriber.transcribe.assert_called()
        # После форсированного flush буфер обнулён (синхронно, внутри ingest()).
        with svc._lock:
            self.assertEqual(svc._buffer_samples, 0)
        svc.close()

    def test_repeated_giant_sr_ingest_buffer_bounded(self) -> None:
        """Многократный ingest с гигантским sample_rate держит буфер ограниченным."""
        svc = _make_service(stt_text="")
        giant_sr = 2_000_000_000
        chunk_samples = 200_000

        for _ in range(400):  # 400 * 200k = 80M samples при отсутствии cap
            svc.ingest(_pcm_bytes(chunk_samples), giant_sr, "off", False)

        with svc._lock:
            self.assertLess(
                svc._buffer_samples,
                _MAX_BUFFER_SAMPLES + chunk_samples,
                "Буфер должен оставаться ограниченным при потоке гигантских sample_rate",
            )


class TestSampleRateClamp(unittest.TestCase):
    """HIGH: handle_ingest клампит/валидирует sample_rate из IPC."""

    def test_out_of_range_high_sample_rate_clamped(self) -> None:
        """sample_rate выше потолка → клампится до _MAX_SAMPLE_RATE (с warning)."""
        self.assertEqual(
            LiveSubsService._sanitize_sample_rate(1_000_000_000),
            _MAX_SAMPLE_RATE,
        )

    def test_out_of_range_low_sample_rate_clamped(self) -> None:
        """sample_rate ниже минимума (включая 0 и отрицательные) → _MIN_SAMPLE_RATE."""
        self.assertEqual(LiveSubsService._sanitize_sample_rate(0), _MIN_SAMPLE_RATE)
        self.assertEqual(LiveSubsService._sanitize_sample_rate(-48000), _MIN_SAMPLE_RATE)
        self.assertEqual(LiveSubsService._sanitize_sample_rate(100), _MIN_SAMPLE_RATE)

    def test_in_range_sample_rate_unchanged(self) -> None:
        """Нормальные значения проходят без изменений."""
        for sr in (8000, 16000, 44100, 48000, 192000):
            self.assertEqual(LiveSubsService._sanitize_sample_rate(sr), sr)

    def test_non_numeric_sample_rate_defaults_to_16000(self) -> None:
        """Нечисловой sample_rate → дефолт 16000 (а не исключение)."""
        self.assertEqual(LiveSubsService._sanitize_sample_rate("not-a-number"), 16000)
        self.assertEqual(LiveSubsService._sanitize_sample_rate(None), 16000)

    def test_handle_ingest_clamps_giant_sample_rate(self) -> None:
        """handle_ingest с гигантским sample_rate не раздувает буфер (клампинг + cap).

        FAIL-BEFORE: sample_rate=int(1e9) принимался как есть → buffer_sec≈0 → нет flush.
        PASS-AFTER: клампится до 192000 → нормальный flush-gate, плюс абсолютный потолок.

        F3 (2026-08-12): non-final flush асинхронный — handle_ingest возвращает
        status=accepted немедленно (буфер уже сброшен синхронно), а фактический
        flush (STT-вызов) проверяем через wait_until_idle() + white-box
        _completed_result, чтобы убедиться, что клампинг действительно дал flush,
        а не просто "тихо принял" без последующей обработки.
        """
        svc = _make_service(stt_text="")
        # Чанк меньше потолка, но при клампнутом sample_rate=192000 буфер_sec велик.
        params = {
            "audio_chunk": _b64_samples(_MAX_SAMPLE_RATE * 4),  # ~4 c при 192 kHz
            "sample_rate": 1_000_000_000,
            "target_lang": "off",
            "is_final": False,
        }
        result = svc.handle_ingest(params)
        # При клампнутом SR=192000 и ~4 c аудио буфер уже сброшен (>3 c порог).
        self.assertEqual(result.get("status"), "accepted",
                         f"Клампнутый sample_rate должен дать корректный (асинхронный) flush: {result}")
        with svc._lock:
            self.assertEqual(svc._buffer_samples, 0)
        self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
        svc._transcriber.transcribe.assert_called()
        svc.close()

    def test_handle_ingest_rejects_negative_sample_rate_no_crash(self) -> None:
        """handle_ingest с отрицательным sample_rate не падает (клампится до min)."""
        svc = _make_service()
        params = {
            "audio_chunk": _b64_samples(800),
            "sample_rate": -5,
            "target_lang": "off",
            "is_final": False,
        }
        result = svc.handle_ingest(params)  # не должно бросить
        self.assertIn("status", result)


# ── (2) MED: no PII in logs ───────────────────────────────────────────────────

class TestNoTranscriptInLogs(unittest.TestCase):
    """MED: INFO-лог flush НЕ содержит текста транскрипта/перевода."""

    def test_flush_info_log_contains_no_transcript_text(self) -> None:
        """Секретный текст транскрипта не должен попасть ни в одно лог-сообщение.

        FAIL-BEFORE: logger.info(... preview=%r ..., text[:30]) логировал PII.
        PASS-AFTER: логируются только метаданные (text_len/lang/translation_len).
        """
        secret_text = "SECRET_TRANSCRIPT_PuZ7q"
        secret_translation = "SEKRETNYY_PEREVOD_QwE9"
        svc = _make_service(stt_text=secret_text, translated=secret_translation)

        # F3 (2026-08-12): flush и его логирование теперь происходят в фоновом
        # воркере — wait_until_idle() ВНУТРИ assertLogs гарантирует, что лог
        # успел записаться до выхода из блока (без ожидания это гонка).
        with self.assertLogs("KrabEar.Backend.LiveSubsService", level="INFO") as cm:
            svc.ingest(_pcm_bytes(16000 * 3), 16000, "ru", False)  # 3 c → flush
            self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")

        svc.close()
        joined = "\n".join(cm.output)
        self.assertNotIn(secret_text, joined,
                         "Текст транскрипта НЕ должен присутствовать в логах (PII)")
        self.assertNotIn(secret_text[:30], joined,
                         "Даже усечённый превью транскрипта НЕ должен попадать в лог")
        self.assertNotIn(secret_translation, joined,
                         "Текст перевода НЕ должен присутствовать в логах (PII)")
        # Метаданные должны присутствовать (логирование не сломано полностью).
        self.assertTrue(
            any("flush OK" in line for line in cm.output),
            "Должна остаться метаданная INFO-строка 'flush OK'",
        )

    def test_flush_record_has_metadata_fields_not_text(self) -> None:
        """LogRecord несёт text_len/lang/translation_len, но не сам текст."""
        secret_text = "ANOTHER_SECRET_aBcD42"
        svc = _make_service(stt_text=secret_text, translated="привет")

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        lg = logging.getLogger("KrabEar.Backend.LiveSubsService")
        handler = _Capture()
        prev_level = lg.level
        lg.addHandler(handler)
        lg.setLevel(logging.INFO)
        try:
            svc.ingest(_pcm_bytes(16000 * 3), 16000, "ru", False)
            # F3: flush асинхронный — ждём воркер ДО снятия хендлера, иначе
            # лог-запись может появиться уже после removeHandler() (гонка).
            self.assertTrue(svc.wait_until_idle(timeout=2.0), "воркер не догнал очередь")
        finally:
            lg.removeHandler(handler)
            lg.setLevel(prev_level)
            svc.close()

        ok_records = [r for r in records if "flush OK" in r.getMessage()]
        self.assertTrue(ok_records, "Ожидалась запись 'flush OK'")
        rec = ok_records[0]
        # Метаданные присутствуют как structured-атрибуты.
        self.assertEqual(getattr(rec, "text_len", None), len(secret_text))
        self.assertEqual(getattr(rec, "lang", None), "en")
        # Текст транскрипта не должен быть ни в одном строковом атрибуте записи.
        for attr_val in vars(rec).values():
            if isinstance(attr_val, str):
                self.assertNotIn(secret_text, attr_val)


# ── (3) MED: MLX/STT runs outside the service RLock ───────────────────────────

class TestSTTRunsOutsideServiceLock(unittest.TestCase):
    """MED: тяжёлый STT не должен держать self._lock (head-of-line blocking)."""

    def test_transcribe_invoked_without_holding_service_lock(self) -> None:
        """Во время transcribe() service-лок должен быть СВОБОДЕН.

        FAIL-BEFORE: STT вызывался под `with self._lock` → acquire(blocking=False)
        из другого «потока» провалился бы (лок занят).
        PASS-AFTER: snapshot+reset под локом, STT — вне него → лок свободен.

        Проверяем из колбэка transcribe: пробуем взять лок неблокирующе ИЗ ДРУГОГО
        потока (RLock реентрантен в текущем потоке, поэтому нужен отдельный поток).

        F3 (2026-08-12): STT теперь вызывается в ФОНОВОМ воркере, а не в
        треде, вызвавшем ingest() — используем threading.Event, чтобы
        детерминированно дождаться, пока side_effect реально выполнится,
        вместо предположения, что он успел отработать синхронно до возврата
        из ingest() (ingest() теперь возвращается немедленно).
        """
        svc = _make_service(stt_text="x")

        lock_was_free: dict[str, bool] = {}
        side_effect_done = threading.Event()

        def _probe_lock_from_other_thread() -> None:
            acquired = svc._lock.acquire(blocking=False)
            lock_was_free["free"] = acquired
            if acquired:
                svc._lock.release()

        def _transcribe_side_effect(audio, **kwargs):  # noqa: ANN001
            t = threading.Thread(target=_probe_lock_from_other_thread)
            t.start()
            t.join(timeout=2.0)
            side_effect_done.set()
            return {"text": "x", "language": "en"}

        svc._transcriber.transcribe.side_effect = _transcribe_side_effect

        svc.ingest(_pcm_bytes(16000 * 3), 16000, "off", False)  # 3 c → flush (асинхронно)
        self.assertTrue(side_effect_done.wait(timeout=3.0), "фоновый воркер не вызвал transcribe вовремя")

        self.assertIn("free", lock_was_free, "transcribe side-effect не выполнился")
        self.assertTrue(
            lock_was_free["free"],
            "service-лок был ЗАНЯТ во время STT — STT выполняется под self._lock (W1770 MED)",
        )
        svc.close()

    def test_concurrent_ingest_during_slow_stt_not_blocked(self) -> None:
        """Пока медленный STT идёт в одном потоке, ingest в другом не блокируется.

        Если STT держит self._lock, второй ingest повис бы на acquire до конца STT.
        Проверяем, что второй ingest успевает добавить чанк, пока STT «спит».
        """
        svc = _make_service(stt_text="")

        stt_started = threading.Event()
        stt_release = threading.Event()
        second_ingest_done = threading.Event()

        def _slow_transcribe(audio, **kwargs):  # noqa: ANN001
            stt_started.set()
            # Имитируем многосекундный STT.
            stt_release.wait(timeout=3.0)
            return {"text": "", "language": "en"}

        svc._transcriber.transcribe.side_effect = _slow_transcribe

        def _flushing_ingest() -> None:
            svc.ingest(_pcm_bytes(16000 * 3), 16000, "off", False)  # триггерит flush+slow STT

        def _second_ingest() -> None:
            stt_started.wait(timeout=2.0)
            # Этот ingest должен пройти, пока STT «висит» в первом потоке.
            svc.ingest(_pcm_bytes(8000), 16000, "off", False)
            second_ingest_done.set()

        t1 = threading.Thread(target=_flushing_ingest)
        t2 = threading.Thread(target=_second_ingest)
        t1.start()
        t2.start()

        # Второй ingest должен завершиться, НЕ дожидаясь окончания STT.
        progressed = second_ingest_done.wait(timeout=2.0)
        stt_release.set()  # отпускаем STT
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        self.assertTrue(
            progressed,
            "Второй ingest заблокировался на время STT — STT держит service-лок (W1770 MED)",
        )


# ── (4) reset() public method ─────────────────────────────────────────────────

class TestPublicReset(unittest.TestCase):
    """reset() очищает буфер под локом, без STT/эмиссии (для privacy-purge)."""

    def test_reset_empties_buffer(self) -> None:
        """reset() обнуляет _buffer и _buffer_samples."""
        svc = _make_service()
        svc.ingest(_pcm_bytes(16000), 16000, "ru", False)  # 1 c, без flush
        self.assertGreater(svc._buffer_samples, 0)

        svc.reset()

        with svc._lock:
            self.assertEqual(svc._buffer, [])
            self.assertEqual(svc._buffer_samples, 0)
        self.assertEqual(svc.buffer_duration_sec(16000), 0.0)

    def test_reset_does_not_transcribe_or_emit(self) -> None:
        """reset() не вызывает STT и не эмитит событий (в отличие от stop())."""
        svc = _make_service()
        svc.ingest(_pcm_bytes(16000), 16000, "ru", False)

        with patch("backend.live_subs_service.event_bus") as mock_bus:
            svc.reset()

        mock_bus.emit_typed.assert_not_called()
        svc._transcriber.transcribe.assert_not_called()

    def test_reset_on_empty_buffer_is_noop(self) -> None:
        """reset() на пустом буфере не падает."""
        svc = _make_service()
        svc.reset()  # не должно бросить
        self.assertEqual(svc._buffer_samples, 0)

    def test_reset_is_thread_safe(self) -> None:
        """Конкурентный reset() + ingest не падают и оставляют согласованное состояние."""
        svc = _make_service(stt_text="")
        errors: list[Exception] = []

        def _worker_ingest() -> None:
            try:
                for _ in range(20):
                    svc.ingest(_pcm_bytes(800), 16000, "off", False)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def _worker_reset() -> None:
            try:
                for _ in range(20):
                    svc.reset()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker_ingest) for _ in range(4)]
        threads += [threading.Thread(target=_worker_reset) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Исключения в потоках: {errors}")
        with svc._lock:
            self.assertEqual(svc._buffer_samples, sum(len(a) for a in svc._buffer))


if __name__ == "__main__":
    unittest.main()
