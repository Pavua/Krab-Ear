import unittest

from backend.error_codes import ERROR_REGISTRY


class ErrorRegistryShapeTests(unittest.TestCase):
    REQUIRED_KEYS = {
        "user_msg_ru", "actionable", "action_id",
        "action_label", "severity", "dedupe_seconds",
    }
    VALID_SEVERITIES = {"info", "warn", "error", "critical"}

    def test_all_entries_have_required_keys(self):
        for code, entry in ERROR_REGISTRY.items():
            with self.subTest(code=code):
                missing = self.REQUIRED_KEYS - set(entry.keys())
                self.assertFalse(missing, f"{code} missing keys: {missing}")

    def test_severities_valid(self):
        for code, entry in ERROR_REGISTRY.items():
            with self.subTest(code=code):
                self.assertIn(entry["severity"], self.VALID_SEVERITIES)

    def test_actionable_implies_action_id(self):
        for code, entry in ERROR_REGISTRY.items():
            with self.subTest(code=code):
                if entry["actionable"]:
                    self.assertIsNotNone(entry["action_id"], f"{code} actionable but no action_id")
                    self.assertTrue(entry["action_label"], f"{code} actionable but empty action_label")

    def test_dedupe_seconds_positive(self):
        for code, entry in ERROR_REGISTRY.items():
            with self.subTest(code=code):
                self.assertGreater(entry["dedupe_seconds"], 0)

    def test_expected_codes_present(self):
        expected = {
            "paste.ax_denied", "paste.app_unsupported",
            "rewriter.timeout", "rewriter.connection_error",
            "rewriter.circuit_open", "rewriter.unavailable",
            # Added 2026-05-04 — gemma-4 production failure modes (HTTP 200
            # but content empty / tool_calls leak / parse error)
            "rewriter.tool_calls_emitted",
            "rewriter.empty_response",
            "rewriter.parse_error",
            "rewriter.model_evicted",
            "stt.load_fail", "stt.empty_text",
            # Added 2026-05-04 Phase C.4 — Whisper repetition-loop hallucination
            "stt.repetition_loop",
            "diarization.no_token", "diarization.pipeline_fail",
            "translation.timeout",
            "mlx.oom",
            "history.write_fail",
            "vocabulary.load_fail",
            "hotkey.conflict",
            # Added 2026-05-04 Phase C.2 — IPC reconnect telemetry
            "ipc.reconnect",
            # rewriter provider/channel failure modes
            "rewriter.fallback_used",
            "rewriter.channel_error",
            "rewriter.unauthorized",
            # Added 2026-05-12 Wave 50 — routine review findings:
            # warmup chronic / MLX timeout / GigaAM padding / pyannote gated
            # / two-binary drift
            "rewriter.warmup_failed",
            "stt.mlx_timeout",
            "stt.padding_mismatch",
            "diarization.vad_gated",
            "agent.binary_drift",
            # Added Wave 60 — production call-site findings:
            # warmup timeout / disk space / buffer overflow / OOM eviction / GigaAM worker timeout
            "rewriter.warmup_timeout",
            "disk.low_space",
            "audio.buffer_overflow",
            "stt.oom_model_evicted",
            "stt.gigaam_worker_timeout",
            # Added Wave 61 — 3 final missing codes:
            # VGW reconnect / diarization skipped / LM Studio HTTP 500 HTML
            "vgw.reconnect",
            "stt.diarization_skipped",
            "rewriter.lm_studio_500",
            # Added Wave 64 — 5 new codes from backend log analysis 2026-05-14/16:
            # ffmpeg missing / Metal assertion / semaphore leak / empty audio / malloc env leak
            "stt.gigaam.ffmpeg_missing",
            "mlx.metal_assertion_failure",
            "mlx.semaphore_leak",
            "stt.empty_audio_warning",
            "system.malloc_env_leak",
            # Added Wave 77 — 3 production-critical codes from Wave 151 log audit:
            # gigaam worker crashed (×3829) / IPC rate limit (×2779) / critical STT error (×68)
            "stt.gigaam_worker_crashed",
            "ipc.rate_limit_exceeded",
            "stt.critical_recognition_error",
            # Added Wave 78 (Wave 205) — 5 production-discovered codes from Wave 202 audit:
            # gigaam HF cache miss / rewriter model unloaded / output ratio fallback
            # / MLX watchdog hang / audio device poll flood
            "stt.gigaam_hf_cache_miss",
            "rewriter.model_unloaded",
            "rewriter.output_ratio_fallback",
            "stt.mlx_watchdog_hang",
            "ipc.audio_device_poll_flood",
            # Added Wave 306 — LM Studio Metal GPU stream context lost:
            # 12 production hits 2026-05-18, misclassified as rewriter.timeout.
            # Now retried once (2s sleep) before circuit failure; dedicated code.
            "rewriter.lm_studio_stream_gpu_lost",
            # Added W860 F1 — dedicated disk.critical code (previously missing from
            # registry; _push_disk_critical_error fell back to empty user_msg_ru).
            "disk.critical",
            # Added W905 F2 — startup.stt_model_cache_miss: STT model not yet in
            # HF cache at startup; pushed by startup diagnostics / engine init path.
            "startup.stt_model_cache_miss",
        }
        self.assertEqual(set(ERROR_REGISTRY.keys()), expected)
