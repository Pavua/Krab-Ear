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
            # Also W1232 fix — Wave 82 codes claimed shipped but never added.
            "disk.critical",
            "system.proc_cmdline_permission",
            # Added W905 F2 — startup.stt_model_cache_miss: STT model not yet in
            # HF cache at startup; pushed by startup diagnostics / engine init path.
            "startup.stt_model_cache_miss",
            # Added W1231 F2 HIGH — two live callsites in llm_rewriter.py were
            # pushing codes not present in the registry, degrading to empty
            # metadata + stale user message.
            "rewriter.mlx_token_bug",
            "rewriter.gpu_stream_error",
            # Added W1614 F1 HIGH — stt.transcribe_failed was in the registry
            # (added in W78) but was missing from this expected set (test drift).
            "stt.transcribe_failed",
            # Added W1614 F1 HIGH — audio.max_duration_reached pushed by
            # recorder.py:257 but was absent from ERROR_REGISTRY causing silent
            # empty-user_msg_ru toast.
            "audio.max_duration_reached",
            # Added W1759 merge-train — history.purge_incomplete: partial purge
            # operation left orphan records; dedicated code for observability.
            "history.purge_incomplete",
            # crypto-audit (2026-06-20) — encryption enabled but encrypt_line failed →
            # record written in plaintext; loud event so the silent downgrade is visible.
            "history.encrypt_fail",
            # Added 2026-07-12 — AudioSelfHealer escalation: PortAudio stack wedged
            # (all-zero frames) and a soft reinit didn't fix it.
            "audio.stack_wedged",
            # Added 2026-07-15 — WakeWordWatchdog escalation: independent
            # wake-word _listen_loop wedged (thread alive, no frames, no
            # exception) and a soft reinit didn't fix it either.
            "audio.wakeword_wedged",
            # Added R1 (2026-07-24) — recording_rescue.run_rescue_scan() пушит
            # при находке .part-файла на старте после некорректного
            # завершения backend (SIGKILL/crash/OOM); уведомляет, что аудио
            # восстановлено (или лежит WAV-ом при privacy_mode).
            "audio.recording_rescued",
            # Added R2 (2026-07-25) — положительный конфликт owner при stop:
            # shadow логирует и продолжает, enforce отклоняет физический stop.
            "recording.owner_mismatch",
            # M2 (2026-07-29) — in-process REST-сервер не смог занять порт
            # (EADDRINUSE, обычно легаси launchd-агент ai.krab.ear.rest ещё жив).
            "rest.port_conflict",
        }
        self.assertEqual(set(ERROR_REGISTRY.keys()), expected)

    # ── Wave 82 regression tests (W1232) ─────────────────────────────────────

    def test_disk_critical_in_registry(self):
        """disk.critical must be present with correct severity and actionable=True."""
        self.assertIn("disk.critical", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["disk.critical"]
        self.assertEqual(entry["severity"], "critical")
        self.assertTrue(entry["actionable"])
        self.assertIsNotNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 600)
        self.assertTrue(entry["user_msg_ru"])

    def test_proc_cmdline_permission_in_registry(self):
        """system.proc_cmdline_permission must be present as a non-actionable error.

        W1697: restored severity=error (Sequoia KERN_PROCARGS2 blocks psutil.process_iter)
        and Sequoia-specific user_msg_ru. Previous value was 'warn'; restored to 'error'.
        """
        self.assertIn("system.proc_cmdline_permission", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["system.proc_cmdline_permission"]
        self.assertEqual(entry["severity"], "error")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 3600)
        self.assertTrue(entry["user_msg_ru"])

    def test_stt_model_cache_miss_in_registry(self):
        """startup.stt_model_cache_miss must be present as a non-actionable warn.

        W1697: restored dedupe_seconds=86400 (one toast per startup day, not per hour).
        Previous value was 3600; restored to 86400.
        """
        self.assertIn("startup.stt_model_cache_miss", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["startup.stt_model_cache_miss"]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 86400)
        self.assertTrue(entry["user_msg_ru"])

    def test_rewriter_mlx_token_bug_in_registry(self):
        """W1231 F2: rewriter.mlx_token_bug must be registered with correct shape."""
        code = "rewriter.mlx_token_bug"
        self.assertIn(code, ERROR_REGISTRY, f"{code} missing from ERROR_REGISTRY")
        entry = ERROR_REGISTRY[code]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 600)
        self.assertIn("MLX", entry["user_msg_ru"])

    def test_rewriter_gpu_stream_error_in_registry(self):
        """W1231 F2: rewriter.gpu_stream_error must be registered with correct shape."""
        code = "rewriter.gpu_stream_error"
        self.assertIn(code, ERROR_REGISTRY, f"{code} missing from ERROR_REGISTRY")
        entry = ERROR_REGISTRY[code]
        self.assertEqual(entry["severity"], "warn")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 600)
        self.assertIn("GPU", entry["user_msg_ru"])

    def test_audio_stack_wedged_in_registry(self):
        """2026-07-12: audio.stack_wedged must be registered with correct shape.

        Pushed by AudioSelfHealer when a soft PortAudio reinit didn't clear a
        wedged audio stack — loud, non-actionable (recommend a manual backend
        restart), deduped 5 min so a stuck stack doesn't spam toasts."""
        code = "audio.stack_wedged"
        self.assertIn(code, ERROR_REGISTRY, f"{code} missing from ERROR_REGISTRY")
        entry = ERROR_REGISTRY[code]
        self.assertEqual(entry["severity"], "error")
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertEqual(entry["dedupe_seconds"], 300)
        self.assertTrue(entry["user_msg_ru"])

    def test_error_registry_count_matches_documentation(self):
        """Registry must contain exactly the documented number of codes.

        Historical counts: initial Phase B = 24, Wave 60 +5, Wave 61 +3,
        Wave 64 +5, Wave 77 +3, Wave 78 +5, Wave 306 +1 = 46,
        plus rewriter.channel_error / rewriter.fallback_used /
        rewriter.unauthorized / rewriter.warmup_failed / stt.mlx_timeout /
        stt.padding_mismatch / diarization.vad_gated / agent.binary_drift = 54,
        W1231 F2 (W1233) added rewriter.mlx_token_bug + rewriter.gpu_stream_error = 56,
        W1231 F1 (W1232) system.proc_cmdline_permission was missing = 57 (prev PR
        already had disk.critical + startup.stt_model_cache_miss),
        W1614 F1 added stt.transcribe_failed (was in registry but test set drifted)
        + audio.max_duration_reached (new) = 58;
        W1759 merge-train added history.purge_incomplete = 59;
        crypto-audit (2026-06-20) added history.encrypt_fail = 60;
        2026-07-12 mic-watchdog self-heal added audio.stack_wedged = 61;
        2026-07-15 wake-word watchdog added audio.wakeword_wedged = 62;
        R1 (2026-07-24) recording-reliability wave added audio.recording_rescued = 63;
        R2 (2026-07-25) owner shadow telemetry added recording.owner_mismatch = 64;
        M2 (2026-07-29) in-process REST server added rest.port_conflict = 65;
        test_expected_codes_present guards the exact set so this count test is a
        redundant but cheap invariant.
        """
        self.assertEqual(len(ERROR_REGISTRY), 65)
