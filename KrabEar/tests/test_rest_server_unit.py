"""Dedicated unit tests for KrabEar/backend/rest_server.py.

Covers gaps NOT addressed by the existing REST test suite:
  - POST /v1/stt/transcribe success path (mock transcriber)
  - POST /v1/stt/transcribe backend exception → 500 JSON
  - POST /v1/stt/transcribe idempotent duplicate → 200 skipped
  - POST /v1/stt/transcribe empty filename → 400
  - POST /v1/stt/transcribe vocabulary hint param
  - GET /health/dashboard → 200 HTML
  - _format_uptime() helper
  - _status_dot_color() helper
  - _parse_cors_origins() helper
  - _build_prometheus_text() with nested stt_metrics structure
  - MAX_CONTENT_LENGTH config (500 MB)
  - MAX_VOCABULARY_SIZE enforcement (501 words → 400/422)
  - MAX_WORD_LENGTH truncation (words >100 chars clipped)
  - metrics.record() called after successful transcription

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_server_unit.py -v
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Guard: skip if Flask / REST-server dependencies are missing.
# Patch heavy objects before module-level AudioEngine() is instantiated.
# ---------------------------------------------------------------------------
_REST_AVAILABLE = False
_rest_mod = None

try:
    import flask  # noqa: F401

    _import_engine = MagicMock()
    _import_engine.quality_profile = "balanced"

    _import_store = MagicMock()
    _import_store.load_vocabulary.return_value = []
    _import_store.is_idempotent.return_value = False
    _import_store.add_history_item.return_value = MagicMock(id="hist-abc-123")
    _import_store.load_settings.return_value = {}  # wave1212

    _import_transcriber = MagicMock()
    _import_transcriber.transcribe.return_value = {
        "text": "Привет мир",
        "raw_text": "Привет мир",
        "confidence": 0.92,
        "duration_ms": 1234,
        "engine": "mlx-whisper",
        "model": "whisper-small",
        "language": "ru",
        "segments": [],
        "diarization": {},
    }

    _import_metrics = MagicMock()
    _import_metrics.get_summary.return_value = {
        "total_requests": 5,
        "error_rate": 0.0,
        "error_count": 0,
        "status": "ok",
        "stt_metrics": {
            "latency_ms": {"p50": 250, "p95": 900, "p99": 1800, "avg": 310},
            "confidence": {"avg": 0.87},
        },
        "window_size": 5,
    }

    with patch("core.engine.AudioEngine", return_value=_import_engine), \
            patch("backend.state_store.StateStore", return_value=_import_store), \
            patch("backend.transcriber.Transcriber", return_value=_import_transcriber), \
            patch("backend.metrics_collector.metrics", _import_metrics):
        import backend.rest_server as _rest_mod

    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    pass


def _get_helpers():
    """Return helper functions from rest_server module (safe even in full suite)."""
    if _rest_mod is None:
        return None, None, None, None
    return (
        _rest_mod._format_uptime,
        _rest_mod._status_dot_color,
        _rest_mod._parse_cors_origins,
        _rest_mod._build_prometheus_text,
    )


def _make_client():
    app = _rest_mod.app
    app.config["TESTING"] = True
    return app.test_client()


class _TranscribeBase(unittest.TestCase):
    """Base for transcribe tests: patches rest_server module-level objects per-test."""

    # Default transcriber return value
    _TRANSCRIBE_RESULT = {
        "text": "Привет мир",
        "raw_text": "Привет мир",
        "confidence": 0.92,
        "duration_ms": 1234,
        "engine": "mlx-whisper",
        "model": "whisper-small",
        "language": "ru",
        "segments": [],
        "diarization": {},
    }

    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.load_vocabulary.return_value = []
        self.mock_store.is_idempotent.return_value = False
        self.mock_store.add_history_item.return_value = MagicMock(id="hist-abc-123")
        self.mock_store.load_settings.return_value = {}  # wave1212

        self.mock_transcriber = MagicMock()
        self.mock_transcriber.transcribe.return_value = dict(self._TRANSCRIBE_RESULT)

        self.mock_metrics = MagicMock()
        self.mock_metrics.get_summary.return_value = {}

        self.mock_engine = MagicMock()
        self.mock_engine.quality_profile = "balanced"
        self.mock_engine.normalize_audio = MagicMock()

        # Patch the module-level objects in rest_server
        self._patcher_store = patch.object(_rest_mod, "store", self.mock_store)
        self._patcher_transcriber = patch.object(_rest_mod, "transcriber", self.mock_transcriber)
        self._patcher_metrics = patch.object(_rest_mod, "metrics", self.mock_metrics)
        self._patcher_engine = patch.object(_rest_mod, "engine", self.mock_engine)
        self._patcher_store.start()
        self._patcher_transcriber.start()
        self._patcher_metrics.start()
        self._patcher_engine.start()

        # Disable rate limiting to avoid 429 from cross-test counter accumulation
        self._orig_limiter_enabled = _rest_mod.limiter.enabled
        _rest_mod.limiter.enabled = False

        self.client = _make_client()

    def tearDown(self):
        _rest_mod.limiter.enabled = self._orig_limiter_enabled
        self._patcher_store.stop()
        self._patcher_transcriber.stop()
        self._patcher_metrics.stop()
        self._patcher_engine.stop()


# ---------------------------------------------------------------------------
# 1. POST /v1/stt/transcribe — success path
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeSuccessTest(_TranscribeBase):
    """POST /v1/stt/transcribe with valid audio file — success path."""

    def test_transcribe_wav_returns_200(self):
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)

    def test_transcribe_result_has_text_field(self):
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        result = resp.get_json()
        self.assertIn("text", result)
        self.assertEqual(result["text"], "Привет мир")

    def test_transcribe_result_has_confidence_field(self):
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        result = resp.get_json()
        self.assertIn("confidence", result)
        self.assertIsInstance(result["confidence"], float)

    def test_transcribe_result_has_history_id(self):
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        result = resp.get_json()
        self.assertIn("history_id", result)
        self.assertEqual(result["history_id"], "hist-abc-123")

    def test_transcribe_result_status_ok(self):
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        result = resp.get_json()
        self.assertEqual(result["status"], "ok")

    def test_transcribe_result_has_engine_field(self):
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        result = resp.get_json()
        self.assertIn("engine", result)

    def test_transcribe_mp3_extension_accepted(self):
        # W1224: _validate_audio_magic_bytes checks first 16 bytes — use real MP3 magic (ID3)
        data = {"file": (io.BytesIO(b"ID3\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"), "recording.mp3")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)

    def test_transcribe_calls_metrics_record(self):
        self.mock_metrics.record.reset_mock()
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.mock_metrics.record.assert_called_once()

    def test_transcribe_with_vocabulary_hint(self):
        """vocabulary form field should be passed through to transcriber."""
        self.mock_transcriber.transcribe.reset_mock()
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "vocabulary": "антигравитация,краб",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        self.mock_transcriber.transcribe.assert_called_once()
        call_kwargs = self.mock_transcriber.transcribe.call_args[1]
        extra_vocab = call_kwargs.get("extra_vocabulary", [])
        self.assertTrue(
            "антигравитация" in extra_vocab or "краб" in extra_vocab,
            f"Expected hint words in extra_vocabulary, got: {extra_vocab}",
        )


# ---------------------------------------------------------------------------
# 1b. POST /v1/stt/transcribe — persist_history flag (2026-07-08)
#
# Companion to a Voice Gateway PR: VG's "Разговор с AI" conversation flow sends
# persist_history=false on each turn so ephemeral conversational utterances
# are transcribed and returned but never written to history.ndjson. Default
# (field omitted) is unchanged — existing callers keep saving to history.
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribePersistHistoryFlagTest(_TranscribeBase):
    """POST /v1/stt/transcribe with persist_history form field."""

    def test_persist_history_false_skips_history_save(self):
        """persist_history=false → transcript returned but NOT saved to history."""
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "persist_history": "false",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        result = resp.get_json()
        # The transcription itself still happens and is still returned.
        self.assertEqual(result["text"], "Привет мир")
        self.assertEqual(result["history_id"], "")
        self.mock_store.add_history_item.assert_not_called()

    def test_persist_history_false_case_insensitive_variants(self):
        """"0"/"no"/"FALSE" all count as false (case-insensitive)."""
        for raw in ("0", "no", "FALSE", "False", "NO"):
            with self.subTest(raw=raw):
                self.mock_store.add_history_item.reset_mock()
                data = {
                    "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
                    "persist_history": raw,
                }
                resp = self.client.post(
                    "/v1/stt/transcribe",
                    data=data,
                    content_type="multipart/form-data",
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.get_json()["history_id"], "")
                self.mock_store.add_history_item.assert_not_called()

    def test_persist_history_explicit_true_saves_history(self):
        """persist_history=true (explicit, case-insensitive) behaves like default."""
        for raw in ("true", "1", "yes", "TRUE", "Yes"):
            with self.subTest(raw=raw):
                self.mock_store.add_history_item.reset_mock()
                self.mock_store.add_history_item.return_value = MagicMock(id="hist-abc-123")
                data = {
                    "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
                    "persist_history": raw,
                }
                resp = self.client.post(
                    "/v1/stt/transcribe",
                    data=data,
                    content_type="multipart/form-data",
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.get_json()["history_id"], "hist-abc-123")
                self.mock_store.add_history_item.assert_called_once()

    def test_persist_history_omitted_defaults_to_true(self):
        """Regression: no persist_history field → unchanged pre-existing behaviour (saves)."""
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        result = resp.get_json()
        self.assertEqual(result["history_id"], "hist-abc-123")
        self.mock_store.add_history_item.assert_called_once()
        call_kwargs = self.mock_store.add_history_item.call_args[1]
        self.assertEqual(call_kwargs.get("text"), "Привет мир")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribePrivacyModeWinsOverPersistHistoryTest(_TranscribeBase):
    """privacy_mode_enabled must ALWAYS win over persist_history (CLAUDE.md rule)."""

    def setUp(self):
        super().setUp()
        self.mock_store.load_settings.return_value = {"privacy_mode_enabled": True}

    def test_privacy_mode_blocks_request_even_with_persist_history_true(self):
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "persist_history": "true",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        # @_privacy_gate short-circuits before the handler body ever runs.
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json().get("skipped"), "privacy_mode")
        self.mock_store.add_history_item.assert_not_called()


# ---------------------------------------------------------------------------
# 2. POST /v1/stt/transcribe — idempotent duplicate
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeIdempotentTest(_TranscribeBase):
    """POST /v1/stt/transcribe with duplicate chat_id+message_id → 200 skipped."""

    def setUp(self):
        super().setUp()
        self.mock_store.is_idempotent.return_value = True

    def test_duplicate_returns_200(self):
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "chat_id": "chat-1",
            "message_id": "msg-1",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)

    def test_duplicate_returns_skipped_reason(self):
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "chat_id": "chat-1",
            "message_id": "msg-1",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        result = resp.get_json()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "duplicate")


# ---------------------------------------------------------------------------
# 3. POST /v1/stt/transcribe — backend exception → 500
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeExceptionTest(_TranscribeBase):
    """Backend exception during transcription should return 500 JSON error."""

    def test_transcriber_exception_returns_500(self):
        self.mock_transcriber.transcribe.side_effect = RuntimeError("Model crashed")
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 500)

    def test_transcriber_exception_returns_json_error(self):
        self.mock_transcriber.transcribe.side_effect = ValueError("Bad audio")
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        result = resp.get_json()
        self.assertIn("error", result)

    def test_transcriber_exception_records_error_metric(self):
        self.mock_metrics.record.reset_mock()
        self.mock_transcriber.transcribe.side_effect = Exception("oops")
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        # metrics.record should be called with is_error=True
        self.mock_metrics.record.assert_called_once_with(0, 0, is_error=True)


# ---------------------------------------------------------------------------
# 4. POST /v1/stt/transcribe — empty filename
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeEmptyFilenameTest(_TranscribeBase):
    """Empty filename in upload should return 400."""

    def test_empty_filename_returns_400(self):
        data = {"file": (io.BytesIO(b"data"), "")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_empty_filename_returns_json_error(self):
        data = {"file": (io.BytesIO(b"data"), "")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        result = resp.get_json()
        self.assertIn("error", result)


# ---------------------------------------------------------------------------
# 5. GET /health/dashboard — HTML page
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class HealthDashboardTest(unittest.TestCase):
    """GET /health/dashboard should return 200 HTML page."""

    def setUp(self):
        self.client = _make_client()

    def test_dashboard_returns_200(self):
        resp = self.client.get("/health/dashboard")
        self.assertEqual(resp.status_code, 200)

    def test_dashboard_content_type_html(self):
        resp = self.client.get("/health/dashboard")
        self.assertIn("text/html", resp.content_type)

    def test_dashboard_body_contains_krab_ear(self):
        resp = self.client.get("/health/dashboard")
        body = resp.data.decode("utf-8")
        self.assertIn("Krab Ear", body)

    def test_dashboard_body_is_complete_html(self):
        resp = self.client.get("/health/dashboard")
        body = resp.data.decode("utf-8")
        self.assertIn("<!DOCTYPE html>", body)
        self.assertIn("</html>", body)


# ---------------------------------------------------------------------------
# 6. _format_uptime() helper
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class FormatUptimeTest(unittest.TestCase):
    """Unit tests for _format_uptime() helper function."""

    def setUp(self):
        _format_uptime, _, _, _ = _get_helpers()
        self._fn = _format_uptime

    def test_zero_seconds(self):
        self.assertEqual(self._fn(0), "00s")

    def test_59_seconds(self):
        self.assertEqual(self._fn(59), "59s")

    def test_60_seconds(self):
        self.assertEqual(self._fn(60), "1m 00s")

    def test_one_hour(self):
        self.assertEqual(self._fn(3600), "1h 0m 00s")

    def test_one_day(self):
        self.assertEqual(self._fn(86400), "1d 0h 0m 00s")

    def test_complex_duration(self):
        seconds = 2 * 86400 + 3 * 3600 + 14 * 60 + 5
        result = self._fn(seconds)
        self.assertIn("2d", result)
        self.assertIn("3h", result)
        self.assertIn("14m", result)
        self.assertIn("05s", result)

    def test_returns_string(self):
        self.assertIsInstance(self._fn(100), str)


# ---------------------------------------------------------------------------
# 7. _status_dot_color() helper
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class StatusDotColorTest(unittest.TestCase):
    """Unit tests for _status_dot_color() helper function."""

    def setUp(self):
        _, _status_dot_color, _, _ = _get_helpers()
        self._fn = _status_dot_color

    def test_ok_returns_green(self):
        self.assertEqual(self._fn("ok"), "#4ade80")

    def test_healthy_returns_green(self):
        self.assertEqual(self._fn("healthy"), "#4ade80")

    def test_closed_returns_green(self):
        self.assertEqual(self._fn("closed"), "#4ade80")

    def test_warning_returns_yellow(self):
        self.assertEqual(self._fn("warning"), "#fbbf24")

    def test_degraded_returns_yellow(self):
        self.assertEqual(self._fn("degraded"), "#fbbf24")

    def test_waiting_data_returns_yellow(self):
        self.assertEqual(self._fn("waiting_data"), "#fbbf24")

    def test_unknown_returns_red(self):
        self.assertEqual(self._fn("unknown"), "#f87171")

    def test_error_returns_red(self):
        self.assertEqual(self._fn("error"), "#f87171")


# ---------------------------------------------------------------------------
# 8. _parse_cors_origins() helper
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class ParseCorsOriginsTest(unittest.TestCase):
    """Unit tests for _parse_cors_origins() function in rest_server."""

    def setUp(self):
        _, _, _parse_cors_origins, _ = _get_helpers()
        self._fn = _parse_cors_origins

    def test_wildcard_returns_string(self):
        self.assertEqual(self._fn("*"), "*")

    def test_wildcard_with_spaces_returns_string(self):
        self.assertEqual(self._fn("  *  "), "*")

    def test_single_origin_returns_list(self):
        self.assertEqual(self._fn("http://localhost:3000"), ["http://localhost:3000"])

    def test_multiple_origins_returns_list(self):
        self.assertEqual(
            self._fn("http://app1.local,http://app2.local"),
            ["http://app1.local", "http://app2.local"],
        )

    def test_strips_spaces_from_origins(self):
        self.assertEqual(
            self._fn("  http://a.com ,  http://b.com  "),
            ["http://a.com", "http://b.com"],
        )

    def test_empty_string_returns_empty_list(self):
        self.assertEqual(self._fn(""), [])


# ---------------------------------------------------------------------------
# 9. _build_prometheus_text() with nested stt_metrics
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class PrometheusTextWithMetricsTest(unittest.TestCase):
    """_build_prometheus_text() produces correct output with nested stt_metrics."""

    def setUp(self):
        _, _, _, _build_prometheus_text = _get_helpers()
        self._fn = _build_prometheus_text

    def _summary(self):
        return {
            "total_requests": 10,
            "error_rate": 0.1,
            "stt_metrics": {
                "latency_ms": {"p50": 300, "p95": 800, "p99": 1500, "avg": 350},
                "confidence": {"avg": 0.89},
            },
            "window_size": 10,
        }

    def test_confidence_avg_in_output(self):
        body = self._fn(self._summary())
        self.assertIn("krab_ear_confidence_avg", body)
        self.assertIn("0.8900", body)

    def test_transcriptions_total_count(self):
        body = self._fn(self._summary())
        self.assertIn("krab_ear_transcriptions_total 10", body)

    def test_latency_histogram_buckets_present(self):
        body = self._fn(self._summary())
        self.assertIn("krab_ear_stt_latency_seconds_bucket", body)

    def test_inf_bucket_count_matches_window(self):
        body = self._fn(self._summary())
        self.assertIn('krab_ear_stt_latency_seconds_bucket{le="+Inf"} 10', body)

    def test_empty_summary_doesnt_crash(self):
        """Should not raise even if stt_metrics is absent."""
        body = self._fn({})
        self.assertIsInstance(body, str)
        self.assertGreater(len(body), 0)


# ---------------------------------------------------------------------------
# 10. MAX_CONTENT_LENGTH configuration
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class MaxContentLengthTest(unittest.TestCase):
    """Flask MAX_CONTENT_LENGTH must be set to 500 MB."""

    def test_max_content_length_is_500mb(self):
        expected = 500 * 1024 * 1024
        self.assertEqual(_rest_mod.app.config.get("MAX_CONTENT_LENGTH"), expected)


# ---------------------------------------------------------------------------
# 11. Vocabulary MAX_VOCABULARY_SIZE enforcement
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class VocabularySizeEnforcementTest(unittest.TestCase):
    """POST /v1/vocabulary must reject payloads that would exceed 500-word limit."""

    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.load_vocabulary.return_value = []
        self._patcher = patch.object(_rest_mod, "store", self.mock_store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_501_words_returns_400_or_422(self):
        too_many = [f"word{i}" for i in range(501)]
        resp = self.client.post("/v1/vocabulary", json={"words": too_many})
        self.assertIn(resp.status_code, (400, 422))

    def test_500_words_returns_200(self):
        exactly_500 = [f"word{i}" for i in range(500)]
        resp = self.client.post("/v1/vocabulary", json={"words": exactly_500})
        self.assertEqual(resp.status_code, 200)

    def test_existing_vocab_plus_new_exceeds_limit_returns_error(self):
        """400/422 when store already has 498 words and we add 5 new ones (503 total)."""
        self.mock_store.load_vocabulary.return_value = [f"existing{i}" for i in range(498)]
        resp = self.client.post("/v1/vocabulary", json={"words": [f"new{i}" for i in range(5)]})
        self.assertIn(resp.status_code, (400, 422))


# ---------------------------------------------------------------------------
# 12. Word length truncation (MAX_WORD_LENGTH = 100)
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class VocabularyWordLengthTest(unittest.TestCase):
    """Words longer than MAX_WORD_LENGTH (100) are silently truncated to 100 chars."""

    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.load_vocabulary.return_value = []
        self._patcher = patch.object(_rest_mod, "store", self.mock_store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_long_word_is_truncated_in_save(self):
        """A 200-char word must be truncated to 100 before being saved."""
        long_word = "а" * 200  # 200 Russian 'а' characters
        resp = self.client.post("/v1/vocabulary", json={"words": [long_word]})
        self.assertEqual(resp.status_code, 200)
        # Verify save_vocabulary was called with a word of at most 100 chars
        call_args = self.mock_store.save_vocabulary.call_args
        saved_words = call_args[0][0]
        for w in saved_words:
            self.assertLessEqual(len(w), 100, f"Word too long after truncation: {len(w)} chars")


if __name__ == "__main__":
    unittest.main()
