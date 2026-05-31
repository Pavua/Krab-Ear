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
import struct
import sys
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

        self._patches = [
            patch.object(_rest_mod, "engine", self.engine),
            patch.object(_rest_mod, "store", self.store),
            patch.object(_rest_mod, "transcriber", self.transcriber),
            patch.object(_rest_mod, "metrics", self.metrics),
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
class TestTranscribeTimeoutKillsLongRunning(_Base):
    """F2: transcriber.transcribe() that never returns → 504 after timeout."""

    def test_transcribe_timeout_kills_long_running(self):
        """Future.result() raises TimeoutError → handler returns 504."""
        import concurrent.futures as _cf

        wav_data = _make_wav_bytes()
        mock_info = MagicMock()
        mock_info.duration = 30.0

        # Replace the ThreadPoolExecutor so future.result() raises TimeoutError
        class _FakeExecutor:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def submit(self, fn, *args, **kwargs):
                fut = _cf.Future()
                # Don't set result — leave future pending
                return fut

        with patch("soundfile.info", return_value=mock_info), \
                patch("concurrent.futures.ThreadPoolExecutor", return_value=_FakeExecutor()), \
                patch.object(
                    _cf.Future,
                    "result",
                    side_effect=_cf.TimeoutError("timed out"),
                ):
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(wav_data), "slow.wav")},
                content_type="multipart/form-data",
            )
        self.assertEqual(resp.status_code, 504)
        body = resp.get_json()
        self.assertIn("timeout", body.get("error", "").lower())


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
