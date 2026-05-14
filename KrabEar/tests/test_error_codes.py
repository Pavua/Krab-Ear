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
        }
        self.assertEqual(set(ERROR_REGISTRY.keys()), expected)
