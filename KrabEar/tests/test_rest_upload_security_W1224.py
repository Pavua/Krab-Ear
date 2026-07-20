"""REST upload security tests — W1213 findings F1+F2+F3+F4 (W1224).

Covers:
  F1 — magic byte validation rejects disguised ZIP / accepts valid WAV
  F2 — decoder DoS: soundfile.info rejects audio >1 hour; transcribe timeout → 504
  F3 — privacy_mode_enabled skips history persistence via REST
  F4 — Unicode filename extension preserved through secure_filename

Run:
    PYTHONPATH=KrabEar python -m unittest \
        KrabEar/tests/test_rest_upload_security_W1224.py -v
"""
from __future__ import annotations

import io
import json
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Guard: skip if REST dependencies unavailable.
# ---------------------------------------------------------------------------
_REST_AVAILABLE = False
_rest_mod = None

try:
    import flask  # noqa: F401

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"
    _mock_engine.normalize_audio = MagicMock()

    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []
    _mock_store.is_idempotent.return_value = False
    _mock_store.add_history_item.return_value = MagicMock(id="hist-W1224-001")
    _mock_store.load_settings.return_value = {}  # W1707: prevent truthy MagicMock → privacy_mode 403

    _mock_transcriber = MagicMock()
    _mock_transcriber.transcribe.return_value = {
        "text": "test transcription",
        "raw_text": "test transcription",
        "confidence": 0.95,
        "duration_ms": 300,
        "engine": "mlx-whisper",
        "model": "whisper-small",
        "language": "en",
        "segments": [],
        "diarization": {},
    }

    _mock_metrics = MagicMock()
    _mock_metrics.get_summary.return_value = {
        "total_requests": 1,
        "error_rate": 0.0,
        "error_count": 0,
        "request_count": 1,
        "status": "ok",
        "stt_metrics": {
            "latency_ms": {"p50": 100, "p95": 500, "p99": 900, "avg": 150},
            "confidence": {"avg": 0.95},
        },
        "window_size": 1,
    }

    if "backend.rest_server" not in sys.modules:
        with patch("core.engine.AudioEngine", return_value=_mock_engine), \
                patch("backend.state_store.StateStore", return_value=_mock_store), \
                patch("backend.transcriber.Transcriber", return_value=_mock_transcriber), \
                patch("backend.metrics_collector.metrics", _mock_metrics):
            import backend.rest_server as _rest_mod  # type: ignore
    else:
        import backend.rest_server as _rest_mod  # type: ignore
        _rest_mod.engine = _mock_engine

    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    pass


def _client():
    _rest_mod.app.config["TESTING"] = True
    return _rest_mod.app.test_client()


# ---------------------------------------------------------------------------
# Minimal valid WAV bytes (44-byte header, 0 PCM samples)
# ---------------------------------------------------------------------------
def _make_wav_bytes() -> bytes:
    """Return a minimal but structurally valid 44-byte WAV header."""
    data_size = 0
    chunk_size = 36 + data_size
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", chunk_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<I", 16))        # subchunk1 size
    buf.write(struct.pack("<H", 1))         # PCM
    buf.write(struct.pack("<H", 1))         # channels
    buf.write(struct.pack("<I", 16000))     # sample rate
    buf.write(struct.pack("<I", 32000))     # byte rate
    buf.write(struct.pack("<H", 2))         # block align
    buf.write(struct.pack("<H", 16))        # bits per sample
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Base: patches module-level singletons + disables rate limiter.
# ---------------------------------------------------------------------------
class _Base(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        self.engine.quality_profile = "balanced"
        self.engine.normalize_audio = MagicMock()

        self.store = MagicMock()
        self.store.load_vocabulary.return_value = []
        self.store.is_idempotent.return_value = False
        self.store.add_history_item.return_value = MagicMock(id="hist-base-W1224")
        self.store.load_settings.return_value = {}  # W1707: prevent truthy MagicMock → privacy_mode 403

        self.transcriber = MagicMock()
        self.transcriber.transcribe.return_value = {
            "text": "hello",
            "raw_text": "hello",
            "confidence": 0.9,
            "duration_ms": 300,
            "engine": "mlx-whisper",
            "model": "whisper-small",
            "language": "en",
            "segments": [],
            "diarization": {},
        }

        self.metrics = MagicMock()
        self.metrics.get_summary.return_value = {
            "total_requests": 1,
            "error_rate": 0.0,
            "error_count": 0,
            "request_count": 1,
            "status": "ok",
            "stt_metrics": {
                "latency_ms": {"p50": 100, "p95": 500, "p99": 900, "avg": 150},
                "confidence": {"avg": 0.9},
            },
            "window_size": 1,
        }

        # Реальный callback использует os._exit(70), поэтому все REST-тесты
        # подменяют только терминальную границу процесса. Сам response-close
        # контракт и очистка временного файла продолжают выполняться реально.
        self.real_process_exit = _rest_mod._exit_poisoned_rest_process
        self.process_exit = MagicMock(name="process_exit")

        self._patches = [
            patch.object(_rest_mod, "engine", self.engine),
            patch.object(_rest_mod, "store", self.store),
            patch.object(_rest_mod, "transcriber", self.transcriber),
            patch.object(_rest_mod, "metrics", self.metrics),
            patch.object(_rest_mod, "_exit_poisoned_rest_process", self.process_exit),
        ]
        for p in self._patches:
            p.start()

        self._orig_limiter = _rest_mod.limiter.enabled
        _rest_mod.limiter.enabled = False

        self.client = _client()

    def tearDown(self):
        _rest_mod.limiter.enabled = self._orig_limiter
        for p in self._patches:
            p.stop()


# ===========================================================================
# F1 — Magic byte validation
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestMagicByteValidationRejectsDisguisedZip(_Base):
    """F1: A ZIP file renamed to exploit.wav must be rejected (400)."""

    def test_magic_byte_validation_rejects_disguised_zip(self):
        """Crafted .wav with PK ZIP header → 400 before decoder is reached."""
        zip_magic = b"PK\x03\x04" + b"\x00" * 100  # ZIP local file header
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"file": (io.BytesIO(zip_magic), "exploit.wav")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("error", body)
        # Transcriber must NOT have been called
        self.transcriber.transcribe.assert_not_called()


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestMagicByteValidationAcceptsWav(_Base):
    """F1: A genuine WAV file must pass magic-byte check and proceed."""

    def test_magic_byte_validation_accepts_wav(self):
        """Valid WAV bytes → 200 (transcription succeeds)."""
        wav_data = _make_wav_bytes()
        # soundfile.info will be called; mock it to return short duration
        mock_info = MagicMock()
        mock_info.duration = 5.0  # 5 seconds — well under limit
        with patch("soundfile.info", return_value=mock_info):
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(wav_data), "voice.wav")},
                content_type="multipart/form-data",
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("status"), "ok")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestUploadTempDirectoryRecovery(_Base):
    """Каталог временных загрузок восстанавливается перед сохранением файла."""

    def test_valid_upload_recreates_deleted_temp_directory(self):
        """Удалённый после импорта TEMP_DIR не превращает валидную загрузку в 500."""
        wav_data = _make_wav_bytes()
        mock_info = MagicMock()
        mock_info.duration = 5.0

        with tempfile.TemporaryDirectory(prefix="krab-rest-upload-") as root:
            upload_dir = Path(root) / "temp_uploads"
            upload_dir.mkdir(parents=True)
            shutil.rmtree(upload_dir)
            self.assertFalse(upload_dir.exists())

            with patch.object(_rest_mod, "TEMP_DIR", upload_dir), \
                    patch("soundfile.info", return_value=mock_info):
                resp = self.client.post(
                    "/v1/stt/transcribe",
                    data={"file": (io.BytesIO(wav_data), "voice.wav")},
                    content_type="multipart/form-data",
                )

            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json().get("text"), "hello")
            self.assertTrue(upload_dir.is_dir())
            self.assertEqual(list(upload_dir.iterdir()), [])
            self.transcriber.transcribe.assert_called_once()


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestMagicByteValidationRejectsRandomBytes(_Base):
    """F1: Arbitrary random bytes named audio.flac must be rejected."""

    def test_magic_byte_validation_rejects_random_bytes_as_flac(self):
        garbage = b"\x00\x01\x02\x03" * 32
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"file": (io.BytesIO(garbage), "audio.flac")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)
        self.transcriber.transcribe.assert_not_called()


# ===========================================================================
# F2 — Decoder DoS: duration check + transcription timeout
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestDecoderRejectsAudioOver1Hour(_Base):
    """F2: soundfile.info reports duration > 3600s → 400 before transcription."""

    def test_decoder_rejects_audio_over_1_hour(self):
        """soundfile.info returns 7200s → request rejected with 400."""
        wav_data = _make_wav_bytes()
        mock_info = MagicMock()
        mock_info.duration = 7200.0  # 2 hours
        with patch("soundfile.info", return_value=mock_info):
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(wav_data), "longfile.wav")},
                content_type="multipart/form-data",
            )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("error", body)
        self.assertIn("long", body["error"].lower())
        self.transcriber.transcribe.assert_not_called()

    def test_decoder_accepts_audio_under_1_hour(self):
        """soundfile.info returns 3599s → request proceeds normally."""
        wav_data = _make_wav_bytes()
        mock_info = MagicMock()
        mock_info.duration = 3599.0
        with patch("soundfile.info", return_value=mock_info):
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(wav_data), "ok.wav")},
                content_type="multipart/form-data",
            )
        self.assertEqual(resp.status_code, 200)


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestTranscribeTimeoutFutureStates(_Base):
    """Не-running Future не вызывает hard-exit здорового REST-процесса."""

    def test_pending_future_returns_504_without_process_exit(self):
        """Успешный cancel pending Future даёт обычный 504 и cleanup."""
        import concurrent.futures as _cf

        wav_data = _make_wav_bytes()
        mock_info = MagicMock()
        mock_info.duration = 30.0
        pending_future = _cf.Future()
        shutdown_calls = []

        # Executor намеренно не запускает задачу: cancel() обязан вернуть True.
        class _FakeExecutor:
            def submit(self, fn, *args, **kwargs):
                return pending_future

            def shutdown(self, wait=True, cancel_futures=False):
                shutdown_calls.append((wait, cancel_futures))

        with tempfile.TemporaryDirectory(prefix="krab-rest-pending-") as root:
            upload_dir = Path(root) / "temp_uploads"
            with patch.object(_rest_mod, "TEMP_DIR", upload_dir), \
                    patch("soundfile.info", return_value=mock_info), \
                    patch("concurrent.futures.ThreadPoolExecutor", return_value=_FakeExecutor()), \
                    patch.object(
                        _cf.Future,
                        "result",
                        side_effect=_cf.TimeoutError("timed out"),
                    ):
                resp = self.client.post(
                    "/v1/stt/transcribe",
                    data={"file": (io.BytesIO(wav_data), "pending.wav")},
                    content_type="multipart/form-data",
                )

            self.assertEqual(resp.status_code, 504)
            self.assertEqual(resp.get_json(), {"error": "Transcription timeout"})
            self.assertTrue(pending_future.cancelled())
            self.assertEqual(shutdown_calls, [(True, False)])
            self.assertEqual(list(upload_dir.iterdir()), [])
            self.process_exit.assert_not_called()
            resp.close()
            self.process_exit.assert_not_called()

    def test_unknown_non_running_future_returns_504_without_exit(self):
        """Нестандартное неопределённое состояние даёт fail-safe 504."""
        import concurrent.futures as _cf

        wav_data = _make_wav_bytes()
        mock_info = MagicMock(duration=30.0)
        state_calls = []
        shutdown_calls = []

        class _UnknownFuture:
            def result(self, timeout=None):
                state_calls.append(("result", timeout))
                raise _cf.TimeoutError("timed out")

            def done(self):
                state_calls.append(("done", None))
                return False

            def cancel(self):
                state_calls.append(("cancel", None))
                return False

            def running(self):
                state_calls.append(("running", None))
                return False

        class _FakeExecutor:
            def submit(self, fn, *args, **kwargs):
                return _UnknownFuture()

            def shutdown(self, wait=True, cancel_futures=False):
                shutdown_calls.append((wait, cancel_futures))

        with tempfile.TemporaryDirectory(prefix="krab-rest-unknown-future-") as root:
            upload_dir = Path(root) / "temp_uploads"
            with patch.object(_rest_mod, "TEMP_DIR", upload_dir), \
                    patch("soundfile.info", return_value=mock_info), \
                    patch("concurrent.futures.ThreadPoolExecutor", return_value=_FakeExecutor()):
                resp = self.client.post(
                    "/v1/stt/transcribe",
                    data={"file": (io.BytesIO(wav_data), "unknown.wav")},
                    content_type="multipart/form-data",
                )

            self.assertEqual(resp.status_code, 504)
            self.assertEqual(resp.get_json(), {"error": "Transcription timeout"})
            self.assertEqual(
                [name for name, _value in state_calls],
                ["result", "done", "cancel", "done", "running"],
            )
            self.assertEqual(shutdown_calls, [(False, True)])
            self.assertEqual(list(upload_dir.iterdir()), [])
            self.process_exit.assert_not_called()
            resp.close()
            self.process_exit.assert_not_called()


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestGunicornTimeoutBudget(unittest.TestCase):
    """Gunicorn не должен обрывать REST раньше собственного watchdog."""

    def test_gunicorn_timeout_exceeds_watchdog_and_response_close_grace(self):
        """Worker budget покрывает watchdog, graceful shutdown и close grace."""
        import runpy

        config = runpy.run_path(str(PROJECT_ROOT / "gunicorn_config.py"))
        minimum_response_close_grace_sec = 30

        self.assertGreaterEqual(
            config["timeout"],
            (
                _rest_mod._TRANSCRIBE_TIMEOUT_SEC
                + config["graceful_timeout"]
                + minimum_response_close_grace_sec
            ),
            (
                "gunicorn timeout обязан пережить REST watchdog и дать "
                "Response.call_on_close выполнить fail-fast"
            )
        )


# ===========================================================================
# W1755 — Wall-clock timeout: 504 must arrive BEFORE the hung worker finishes
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestTranscribeTimeoutDoesNotBlockOnHungWorker(_Base):
    """Регрессия: 504 приходит быстро, а hard-exit ждёт закрытия ответа.

    Блокирующий decoder дольше таймаута держит executor worker. Обработчик не
    ждёт этот поток, но и не завершает процесс до того, как WSGI закрыл уже
    сформированный 504-ответ.
    """

    TIMEOUT_SEC = 0.3    # патчим константу на короткое значение
    LONG_SLEEP = 3.0     # воркер зависает на столько секунд
    EPSILON = 1.5        # допуск: ответ должен прийти за timeout + epsilon

    def test_504_arrives_before_hung_worker_finishes(self):
        """504 приходит за timeout+epsilon, не после полного LONG_SLEEP."""
        import threading
        import time as _time

        wav_data = _make_wav_bytes()
        mock_info = MagicMock()
        mock_info.duration = 5.0

        # Вечно-блокирующий transcribe — имитирует зависший MLX decoder.
        _never = threading.Event()
        self.addCleanup(_never.set)

        def _hung_transcribe(*_a, **_kw):
            # Ждём LONG_SLEEP секунд (или до timeout теста, что наступит раньше)
            _never.wait(timeout=self.LONG_SLEEP)
            return {
                "text": "это никогда не вернётся вовремя",
                "confidence": 0.5,
                "duration_ms": 100,
                "engine": "mlx-whisper",
                "model": "whisper-small",
                "language": "ru",
                "segments": [],
                "diarization": {},
            }

        self.transcriber.transcribe.side_effect = _hung_transcribe

        resp = None
        response_closed = False
        try:
            t0 = _time.monotonic()
            with patch("soundfile.info", return_value=mock_info), \
                    patch.object(_rest_mod, "_TRANSCRIBE_TIMEOUT_SEC", self.TIMEOUT_SEC):
                resp = self.client.post(
                    "/v1/stt/transcribe",
                    data={"file": (io.BytesIO(wav_data), "hung.wav")},
                    content_type="multipart/form-data",
                )
            elapsed = _time.monotonic() - t0

            # Убеждаемся что получили 504 с правильным телом
            self.assertEqual(resp.status_code, 504,
                             f"Expected 504, got {resp.status_code} in {elapsed:.3f}s")
            body = resp.get_json()
            self.assertIn("timeout", body.get("error", "").lower(),
                          f"Unexpected body: {body}")

            # КЛЮЧЕВАЯ проверка: ответ пришёл ДО того как воркер завершился.
            # Без исправления elapsed ≈ LONG_SLEEP (shutdown(wait=True) блокирует).
            # С исправлением elapsed ≈ TIMEOUT_SEC (быстрый возврат).
            max_allowed = self.TIMEOUT_SEC + self.EPSILON
            self.assertLess(
                elapsed,
                max_allowed,
                f"Request took {elapsed:.3f}s — handler BLOCKED on hung worker "
                f"(expected < {max_allowed:.3f}s). "
                f"W1755: shutdown(wait=False, cancel_futures=True) not enforced.",
            )

            # Тело доступно клиенту до терминального callback процесса.
            self.process_exit.assert_not_called()
            resp.close()
            response_closed = True
            self.process_exit.assert_called_once_with(
                _rest_mod._TRANSCRIBE_TIMEOUT_EXIT_CODE,
            )
        finally:
            # Пока patch exit-boundary ещё активен, закрываем ответ даже при
            # раннем assertion failure; затем отпускаем реальный worker.
            if resp is not None and not response_closed:
                resp.close()
            _never.set()


# ===========================================================================
# P1 — зависший executor не должен переживать закрытый 504-ответ
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestTranscribeTimeoutProcessRecovery(_Base):
    """Проверяет fail-fast, порядок очистки и обычные ветки REST upload."""

    TIMEOUT_SEC = 0.05

    def test_process_boundary_uses_non_finalizing_exit_code_70(self):
        """Терминальная граница вызывает os._exit(70), а не sys.exit."""
        with patch.object(_rest_mod.os, "_exit") as raw_exit:
            self.real_process_exit(
                _rest_mod._TRANSCRIBE_TIMEOUT_EXIT_CODE,
            )

        raw_exit.assert_called_once_with(70)

    def test_blocked_transcriber_defers_cleanup_and_exit_until_response_close(self):
        """Upload жив до выдачи 504; close удаляет его и вызывает exit(70)."""
        import threading
        import time as _time

        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def _blocked_transcribe(*_args, **_kwargs):
            started.set()
            try:
                release.wait(timeout=5.0)
                return {"text": "поздний результат"}
            finally:
                finished.set()

        self.transcriber.transcribe.side_effect = _blocked_transcribe
        wav_data = _make_wav_bytes()
        mock_info = MagicMock(duration=5.0)

        with tempfile.TemporaryDirectory(prefix="krab-rest-timeout-") as root:
            upload_dir = Path(root) / "temp_uploads"
            upload_dir.mkdir()
            unrelated = upload_dir / "не-чужой-файл.keep"
            unrelated.write_bytes(b"keep")
            response = None
            response_closed = False
            try:
                started_at = _time.monotonic()
                with patch.object(_rest_mod, "TEMP_DIR", upload_dir), \
                        patch.object(_rest_mod, "_TRANSCRIBE_TIMEOUT_SEC", self.TIMEOUT_SEC), \
                        patch("soundfile.info", return_value=mock_info):
                    response = self.client.post(
                        "/v1/stt/transcribe",
                        data={"file": (io.BytesIO(wav_data), "blocked.wav")},
                        content_type="multipart/form-data",
                    )
                elapsed = _time.monotonic() - started_at

                self.assertTrue(started.wait(timeout=1.0))
                self.assertEqual(response.status_code, 504)
                self.assertEqual(response.get_json(), {"error": "Transcription timeout"})
                self.assertLess(elapsed, 1.0)
                self.process_exit.assert_not_called()

                pending_uploads = [path for path in upload_dir.iterdir() if path != unrelated]
                self.assertEqual(len(pending_uploads), 1)
                self.assertTrue(pending_uploads[0].name.endswith("_blocked.wav"))

                response.close()
                response_closed = True
                self.process_exit.assert_called_once_with(70)
                self.assertFalse(pending_uploads[0].exists())
                self.assertTrue(unrelated.exists())
            finally:
                if response is not None and not response_closed:
                    response.close()
                release.set()
                self.assertTrue(finished.wait(timeout=2.0))

    def test_success_cleans_temp_without_process_exit(self):
        """Успешная транскрибация очищает upload и не ставит hard-exit."""
        wav_data = _make_wav_bytes()
        mock_info = MagicMock(duration=5.0)

        with tempfile.TemporaryDirectory(prefix="krab-rest-success-") as root:
            upload_dir = Path(root) / "temp_uploads"
            with patch.object(_rest_mod, "TEMP_DIR", upload_dir), \
                    patch("soundfile.info", return_value=mock_info):
                response = self.client.post(
                    "/v1/stt/transcribe",
                    data={"file": (io.BytesIO(wav_data), "success.wav")},
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(list(upload_dir.iterdir()), [])
            response.close()
            self.process_exit.assert_not_called()

    def test_transcriber_error_cleans_temp_without_process_exit(self):
        """Завершившаяся ошибка даёт 500 без рестарта и без остатка upload."""
        self.transcriber.transcribe.side_effect = RuntimeError("decoder failed")
        wav_data = _make_wav_bytes()
        mock_info = MagicMock(duration=5.0)

        with tempfile.TemporaryDirectory(prefix="krab-rest-error-") as root:
            upload_dir = Path(root) / "temp_uploads"
            with patch.object(_rest_mod, "TEMP_DIR", upload_dir), \
                    patch("soundfile.info", return_value=mock_info):
                response = self.client.post(
                    "/v1/stt/transcribe",
                    data={"file": (io.BytesIO(wav_data), "error.wav")},
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 500)
            self.assertEqual(list(upload_dir.iterdir()), [])
            response.close()
            self.process_exit.assert_not_called()

    def test_completed_timeout_exception_does_not_poison_process(self):
        """TimeoutError из decoder-а не равен wall-clock таймауту Future."""
        import concurrent.futures as _cf

        self.transcriber.transcribe.side_effect = _cf.TimeoutError("decoder timeout")
        wav_data = _make_wav_bytes()
        mock_info = MagicMock(duration=5.0)

        with tempfile.TemporaryDirectory(prefix="krab-rest-decoder-timeout-") as root:
            upload_dir = Path(root) / "temp_uploads"
            with patch.object(_rest_mod, "TEMP_DIR", upload_dir), \
                    patch("soundfile.info", return_value=mock_info):
                response = self.client.post(
                    "/v1/stt/transcribe",
                    data={"file": (io.BytesIO(wav_data), "decoder-timeout.wav")},
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 500)
            self.assertEqual(list(upload_dir.iterdir()), [])
            response.close()
            self.process_exit.assert_not_called()


# ===========================================================================
# F3 — Privacy mode skips history persistence
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestPrivacyModeSkipsHistoryPersistViaRest(_Base):
    """F3: When privacy_mode_enabled=True, store.add_history_item() is NOT called."""

    def test_privacy_mode_skips_history_persist_via_rest(self):
        """privacy_mode_enabled=True → returns 403 {"ok": false, "skipped": "privacy_mode"}.

        W1212: privacy_mode=True blocks the entire transcription endpoint with 403,
        not just history persistence (more conservative security posture).
        """
        wav_data = _make_wav_bytes()
        mock_info = MagicMock()
        mock_info.duration = 5.0

        with patch("soundfile.info", return_value=mock_info), \
                patch.object(_rest_mod, "_load_settings_field", return_value=True):
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(wav_data), "private.wav")},
                content_type="multipart/form-data",
            )

        # W1212: privacy_mode returns 403, not 200
        self.assertEqual(resp.status_code, 403)
        body = resp.get_json()
        self.assertIn("skipped", body)
        self.assertEqual(body.get("skipped"), "privacy_mode")
        # History must NOT have been persisted
        self.store.add_history_item.assert_not_called()

    def test_privacy_mode_disabled_persists_history(self):
        """privacy_mode_enabled=False → store.add_history_item() IS called."""
        wav_data = _make_wav_bytes()
        mock_info = MagicMock()
        mock_info.duration = 5.0

        with patch("soundfile.info", return_value=mock_info), \
                patch.object(_rest_mod, "_load_settings_field", return_value=False):
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(wav_data), "public.wav")},
                content_type="multipart/form-data",
            )

        self.assertEqual(resp.status_code, 200)
        self.store.add_history_item.assert_called_once()


# ===========================================================================
# F4 — Unicode filename extension preserved
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestUnicodeFilenameExtensionPreserved(_Base):
    """F4: Cyrillic/Unicode filenames must not lose their extension."""

    def test_unicode_filename_extension_preserved(self):
        """'тест.wav' must be accepted (extension '.wav' is preserved)."""
        wav_data = _make_wav_bytes()
        mock_info = MagicMock()
        mock_info.duration = 5.0

        with patch("soundfile.info", return_value=mock_info):
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(wav_data), "тест.wav")},
                content_type="multipart/form-data",
            )
        # Should not be rejected with "Unsupported file type: " for empty ext
        self.assertEqual(resp.status_code, 200)

    def test_unicode_filename_without_audio_ext_rejected(self):
        """'данные.pdf' must still be rejected — wrong extension."""
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"file": (io.BytesIO(b"%PDF-1.4"), "данные.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIn("error", body)
        self.assertIn(".pdf", body["error"])

    def test_ascii_filename_extension_still_works(self):
        """Regular ASCII 'audio.mp3' extension check still functions."""
        mp3_header = b"ID3" + b"\x03\x00\x00" + b"\x00" * 30
        mock_info = MagicMock()
        mock_info.duration = 10.0

        with patch("soundfile.info", return_value=mock_info):
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(mp3_header), "podcast.mp3")},
                content_type="multipart/form-data",
            )
        # ID3 magic is accepted by _validate_audio_magic_bytes
        self.assertEqual(resp.status_code, 200)


# ===========================================================================
# Unit tests for _validate_audio_magic_bytes
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestValidateAudioMagicBytesUnit(unittest.TestCase):
    """Unit tests for the _validate_audio_magic_bytes() helper."""

    def _fn(self, data: bytes) -> bool:
        return _rest_mod._validate_audio_magic_bytes(data)

    def test_wav_magic_accepted(self):
        self.assertTrue(self._fn(b"RIFF\x24\x00\x00\x00WAVE"))

    def test_flac_magic_accepted(self):
        self.assertTrue(self._fn(b"fLaC" + b"\x00" * 12))

    def test_ogg_magic_accepted(self):
        self.assertTrue(self._fn(b"OggS" + b"\x00" * 12))

    def test_webm_magic_accepted(self):
        self.assertTrue(self._fn(b"\x1A\x45\xDF\xA3" + b"\x00" * 12))

    def test_mp3_id3_accepted(self):
        self.assertTrue(self._fn(b"ID3" + b"\x03\x00\x00" + b"\x00" * 10))

    def test_mp3_sync_word_ffb_accepted(self):
        self.assertTrue(self._fn(b"\xFF\xFB" + b"\x00" * 14))

    def test_mp3_sync_word_fff3_accepted(self):
        self.assertTrue(self._fn(b"\xFF\xF3" + b"\x00" * 14))

    def test_m4a_ftyp_accepted(self):
        self.assertTrue(self._fn(b"\x00\x00\x00\x20ftyp" + b"M4A " + b"\x00" * 6))

    def test_zip_rejected(self):
        self.assertFalse(self._fn(b"PK\x03\x04" + b"\x00" * 12))

    def test_pdf_rejected(self):
        self.assertFalse(self._fn(b"%PDF-1.4" + b"\x00" * 8))

    def test_empty_rejected(self):
        self.assertFalse(self._fn(b""))

    def test_too_short_rejected(self):
        self.assertFalse(self._fn(b"\xff"))

    def test_random_garbage_rejected(self):
        self.assertFalse(self._fn(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f"))

    def test_wav_without_wave_tag_rejected(self):
        # "RIFF" + size + "AVI " — not WAVE
        self.assertFalse(self._fn(b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 4))


if __name__ == "__main__":
    unittest.main()
