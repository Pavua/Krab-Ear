"""Shadow/enforce-телеметрия владения и проводка source (R2 Task 6).

Тесты защищают вторую ось stop-протокола: generation token всегда решается
раньше owner-политики, а положительное несовпадение владельцев наблюдаемо без
утечки произвольного ``source`` в логи. Дополнительно фиксируется проводка
``source`` от meeting, dictation и quick capture, живой ErrorBus и безопасная
нормализация runtime-флага ``recording_owner_enforce``.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_ROOT.parent
for import_root in (PROJECT_ROOT, TESTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from backend.error_codes import ERROR_REGISTRY  # noqa: E402
from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.settings_validator import (  # noqa: E402
    SettingsValidator,
    _BOOL_FIELDS,
)
from core.config import DEFAULT_SETTINGS  # noqa: E402
from test_recording_stop_gate import (  # noqa: E402
    _CountingRecorder,
    _make_service,
)


class RecordingOwnerTelemetryTest(unittest.TestCase):
    """Проверить решения shadow/enforce и PII-безопасную наблюдаемость."""

    def setUp(self) -> None:
        self._tmp_ctx = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmp_ctx.cleanup)
        self._tmp = Path(self._tmp_ctx.name)
        self._service_index = 0

    def _service(
        self,
        *,
        recorder: _CountingRecorder | None = None,
        settings_overrides: dict | None = None,
    ) -> RecordingCoreService:
        self._service_index += 1
        service = _make_service(
            self._tmp / f"service-{self._service_index}",
            recorder=recorder,
            settings_overrides=settings_overrides,
        )
        self.addCleanup(service.close_background_workers)
        return service

    @staticmethod
    def _start(
        service: RecordingCoreService,
        owner: str,
    ) -> dict:
        started = service.handle_start_recording({"source": owner})
        if started.get("status") != "recording":
            raise AssertionError(f"Тестовый start не состоялся: {started!r}")
        return started

    def test_shadow_matching_token_reports_once_and_still_stops(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        service._error_bus = MagicMock()
        started = self._start(service, "meeting")

        with patch(
            "backend.recording_core_service.logger.warning"
        ) as warning:
            response = service.handle_stop_recording({
                "generation_token": started["generation_token"],
                "source": "dictation",
            })

        self.assertEqual(response["status"], "ok")
        self.assertEqual(recorder.stop_calls, 1)
        warning.assert_called_once()
        service._error_bus.push.assert_called_once()
        error = service._error_bus.push.call_args.args[0]
        self.assertEqual(error.code, "recording.owner_mismatch")
        self.assertEqual(error.component, "recording")
        self.assertEqual(
            error.context,
            {"owner": "meeting", "requested": "dictation"},
        )

    def test_enforce_matching_token_rejects_before_any_teardown(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_owner_enforce": True},
        )
        service._error_bus = MagicMock()
        started = self._start(service, "meeting")
        active_generation = service._active_generation
        partial = MagicMock()
        rsf = MagicMock()
        service._rt_partial = partial
        service._rsf = rsf

        with patch.object(
            service,
            "_stop_preview_worker",
            wraps=service._stop_preview_worker,
        ) as stop_preview:
            response = service.handle_stop_recording({
                "generation_token": started["generation_token"],
                "source": "dictation",
            })

        self.assertEqual(response["status"], "owner_mismatch")
        self.assertEqual(recorder.stop_calls, 0)
        self.assertTrue(recorder.is_recording)
        self.assertIs(service._active_generation, active_generation)
        stop_preview.assert_not_called()
        partial.stop.assert_not_called()
        rsf.stop.assert_not_called()
        service._error_bus.push.assert_called_once()

    def test_tokenless_legacy_mismatch_obeys_shadow_and_enforce(self) -> None:
        cases = (
            (False, "ok", 1),
            (True, "owner_mismatch", 0),
        )
        for enforce, expected_status, expected_stops in cases:
            with self.subTest(enforce=enforce):
                recorder = _CountingRecorder()
                service = self._service(
                    recorder=recorder,
                    settings_overrides={
                        "recording_owner_enforce": enforce,
                    },
                )
                service._error_bus = MagicMock()
                self._start(service, "meeting")

                response = service.handle_stop_recording({
                    "source": "dictation",
                })

                self.assertEqual(response["status"], expected_status)
                self.assertEqual(recorder.stop_calls, expected_stops)
                service._error_bus.push.assert_called_once()

    def test_matching_owner_is_silent_in_both_modes(self) -> None:
        for enforce in (False, True):
            with self.subTest(enforce=enforce):
                recorder = _CountingRecorder()
                service = self._service(
                    recorder=recorder,
                    settings_overrides={
                        "recording_owner_enforce": enforce,
                    },
                )
                service._error_bus = MagicMock()
                started = self._start(service, "meeting")

                with patch(
                    "backend.recording_core_service.logger.warning"
                ) as warning:
                    response = service.handle_stop_recording({
                        "generation_token": (
                            started["generation_token"]
                        ),
                        "source": "meeting",
                    })

                self.assertEqual(response["status"], "ok")
                self.assertEqual(recorder.stop_calls, 1)
                warning.assert_not_called()
                service._error_bus.push.assert_not_called()

    def test_absent_or_invalid_source_keeps_legacy_path_in_enforce(self) -> None:
        legacy_sources = (
            ("missing", None),
            ("null", None),
            ("empty", ""),
            ("whitespace", "   "),
            ("number", 7),
            ("list", ["dictation"]),
            ("object", {"source": "dictation"}),
        )
        for label, raw_source in legacy_sources:
            with self.subTest(source=label):
                recorder = _CountingRecorder()
                service = self._service(
                    recorder=recorder,
                    settings_overrides={
                        "recording_owner_enforce": True,
                    },
                )
                service._error_bus = MagicMock()
                self._start(service, "meeting")
                params = {}
                if label != "missing":
                    params["source"] = raw_source

                with patch(
                    "backend.recording_core_service.logger.warning"
                ) as warning, patch(
                    "backend.recording_core_service.logger.debug"
                ) as debug:
                    response = service.handle_stop_recording(params)

                self.assertEqual(response["status"], "ok")
                self.assertEqual(recorder.stop_calls, 1)
                warning.assert_not_called()
                debug.assert_called_once()
                service._error_bus.push.assert_not_called()

    def test_legacy_debug_failure_is_fail_open(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_owner_enforce": True},
        )
        self._start(service, "meeting")

        with patch(
            "backend.recording_core_service.logger.debug",
            side_effect=RuntimeError("debug logger сломан тестом"),
        ) as debug:
            response = service.handle_stop_recording({})

        self.assertEqual(response["status"], "ok")
        self.assertEqual(recorder.stop_calls, 1)
        debug.assert_called_once()

    def test_owner_string_is_trimmed_before_comparison(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_owner_enforce": True},
        )
        service._error_bus = MagicMock()
        started = self._start(service, "meeting")

        response = service.handle_stop_recording({
            "generation_token": started["generation_token"],
            "source": "  meeting  ",
        })

        self.assertEqual(response["status"], "ok")
        self.assertEqual(recorder.stop_calls, 1)
        service._error_bus.push.assert_not_called()

    def test_token_invariants_precede_owner_telemetry(self) -> None:
        malformed_tokens = ("foreign-token", "", None, 0, [], {})
        for token in malformed_tokens:
            with self.subTest(token=token):
                recorder = _CountingRecorder()
                service = self._service(recorder=recorder)
                self._start(service, "meeting")

                with patch.object(
                    service,
                    "_report_owner_mismatch",
                ) as report:
                    response = service.handle_stop_recording({
                        "generation_token": token,
                        "source": "dictation",
                    })

                self.assertEqual(
                    response["status"],
                    "unknown_generation",
                )
                self.assertEqual(recorder.stop_calls, 0)
                report.assert_not_called()

    def test_finalizing_token_precedes_owner_telemetry(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        started = self._start(service, "meeting")
        service._active_generation["state"] = "finalizing"

        with patch.object(
            service,
            "_report_owner_mismatch",
        ) as report:
            response = service.handle_stop_recording({
                "generation_token": started["generation_token"],
                "source": "dictation",
            })

        self.assertEqual(response["status"], "stop_in_progress")
        self.assertEqual(recorder.stop_calls, 0)
        report.assert_not_called()

    def test_terminal_replay_precedes_new_owner_telemetry(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        first = self._start(service, "dictation")
        terminal = service.handle_stop_recording({
            "generation_token": first["generation_token"],
            "source": "dictation",
        })
        second = self._start(service, "meeting")

        with patch.object(
            service,
            "_report_owner_mismatch",
        ) as report:
            replayed = service.handle_stop_recording({
                "generation_token": first["generation_token"],
                "source": "dictation",
            })

        self.assertEqual(replayed, terminal)
        self.assertEqual(recorder.stop_calls, 1)
        self.assertTrue(recorder.is_recording)
        self.assertEqual(
            service._active_generation["token"],
            second["generation_token"],
        )
        report.assert_not_called()

    def test_promoted_meeting_owner_stops_silently(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_owner_enforce": True},
        )
        self._start(service, "dictation")
        promoted = service.handle_start_recording({"source": "meeting"})
        self.assertTrue(promoted["owner_promoted"])

        with patch.object(
            service,
            "_report_owner_mismatch",
        ) as report:
            response = service.handle_stop_recording({
                "generation_token": promoted["generation_token"],
                "source": "meeting",
            })

        self.assertEqual(response["status"], "ok")
        self.assertEqual(recorder.start_calls, 1)
        self.assertEqual(recorder.stop_calls, 1)
        report.assert_not_called()

    def test_telemetry_redacts_arbitrary_source_values(self) -> None:
        owner_secret = "owner-user@example.test"
        requested_secret = "requested-user@example.test"
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_owner_enforce": True},
        )
        service._error_bus = MagicMock()
        started = self._start(service, owner_secret)

        with patch(
            "backend.recording_core_service.logger.warning"
        ) as warning:
            response = service.handle_stop_recording({
                "generation_token": started["generation_token"],
                "source": requested_secret,
            })

        self.assertEqual(response["status"], "owner_mismatch")
        error = service._error_bus.push.call_args.args[0]
        serialized_error = error.model_dump_json()
        serialized_log = repr(warning.call_args)
        for secret in (owner_secret, requested_secret):
            self.assertNotIn(secret, serialized_error)
            self.assertNotIn(secret, serialized_log)
        self.assertEqual(
            error.context,
            {"owner": "other", "requested": "other"},
        )

    def test_error_bus_failure_is_fail_open_in_shadow(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        service._error_bus = MagicMock()
        service._error_bus.push.side_effect = RuntimeError(
            "ErrorBus сломан тестом"
        )
        started = self._start(service, "meeting")

        response = service.handle_stop_recording({
            "generation_token": started["generation_token"],
            "source": "dictation",
        })

        self.assertEqual(response["status"], "ok")
        self.assertEqual(recorder.stop_calls, 1)
        service._error_bus.push.assert_called_once()

    def test_logger_failure_is_fail_open_in_shadow(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        service._error_bus = MagicMock()
        started = self._start(service, "meeting")

        with patch(
            "backend.recording_core_service.logger.warning",
            side_effect=RuntimeError("logger сломан тестом"),
        ) as warning:
            response = service.handle_stop_recording({
                "generation_token": started["generation_token"],
                "source": "dictation",
            })

        self.assertEqual(response["status"], "ok")
        self.assertEqual(recorder.stop_calls, 1)
        warning.assert_called_once()

    def test_string_false_does_not_accidentally_enable_enforce(self) -> None:
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_owner_enforce": "false"},
        )
        self._start(service, "meeting")

        response = service.handle_stop_recording({
            "source": "dictation",
        })

        self.assertEqual(response["status"], "ok")
        self.assertEqual(recorder.stop_calls, 1)


class RecordingOwnerConfigurationContractTest(unittest.TestCase):
    """Проверить registry, дефолт, bool-валидацию и runtime-проводку."""

    def test_owner_enforce_defaults_to_shadow(self) -> None:
        self.assertIs(
            DEFAULT_SETTINGS["recording_owner_enforce"],
            False,
        )
        self.assertIs(_BOOL_FIELDS["recording_owner_enforce"], False)

    def test_settings_validator_coerces_owner_enforce_strings(self) -> None:
        validator = SettingsValidator()
        for raw, expected in (("false", False), ("true", True)):
            with self.subTest(raw=raw):
                result = validator.validate({
                    "recording_owner_enforce": raw,
                })
                self.assertTrue(result.valid)
                self.assertIs(
                    result.fixed["recording_owner_enforce"],
                    expected,
                )

    def test_owner_mismatch_error_code_is_neutral_and_bounded(self) -> None:
        entry = ERROR_REGISTRY["recording.owner_mismatch"]
        self.assertEqual(entry["severity"], "warn")
        self.assertEqual(entry["dedupe_seconds"], 30)
        self.assertFalse(entry["actionable"])
        self.assertNotIn(
            "не тронута",
            entry["user_msg_ru"].lower(),
        )
        self.assertEqual(len(ERROR_REGISTRY), 65)

    def test_backend_service_wires_error_bus_into_recording_core(self) -> None:
        source = (
            PROJECT_ROOT / "backend" / "service.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "self._recording_core_svc._error_bus = self._error_bus",
            source,
        )


class RecordingOwnerSourceWiringContractTest(unittest.TestCase):
    """Проверить реальные IPC-вызовы, а не декоративные helper-ы."""

    @staticmethod
    def _swift_source(name: str) -> str:
        return (
            REPO_ROOT
            / "native"
            / "KrabEarAgent"
            / "Sources"
            / "KrabEarAgent"
            / name
        ).read_text(encoding="utf-8")

    def test_meeting_internal_stop_identifies_owner(self) -> None:
        source = (
            PROJECT_ROOT
            / "backend"
            / "meeting_session_service.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'stop_params: dict[str, Any] = {"source": "meeting"}',
            source,
        )
        self.assertIn(
            'stop_params["generation_token"] = generation_token',
            source,
        )
        self.assertIn(
            "handle_stop_recording(stop_params)",
            source,
        )

    def test_dictation_start_and_stop_identify_owner(self) -> None:
        source = self._swift_source("main+HotkeyRecording.swift")
        # Проверяем ПРИСУТСТВИЕ owner-ключа внутри params, а не то, что он
        # там единственный: рядом легитимно живёт start_request_id, и
        # закрывающая скобка сразу после "dictation" краснила бы корректный
        # код при любом расширении вызова.
        self.assertRegex(
            source,
            re.compile(
                r'method:\s*"start_recording",\s*'
                r'params:\s*\[[^\]]*?"source":\s*"dictation"',
                re.DOTALL,
            ),
        )
        stop_start = source.index("func stopRecording()")
        stop_body = source[stop_start:]
        self.assertIn('let stopOwner = "dictation"', stop_body)
        self.assertIn('"source": stopOwner', stop_body)
        self.assertIn('params["generation_token"] = stopToken', stop_body)
        self.assertIn("RecordingStopCoordinator.execute", stop_body)

    def test_quick_capture_all_recording_calls_identify_owner(self) -> None:
        source = self._swift_source("main+QuickCapture.swift")
        # Как и для диктовки: owner-ключ обязан быть, но не обязан быть
        # единственным параметром вызова (см. start_request_id).
        start_calls = re.findall(
            r'method:\s*"start_recording",\s*'
            r'params:\s*\[[^\]]*?"source":\s*"quick_capture"',
            source,
            re.DOTALL,
        )
        self.assertEqual(len(start_calls), 1)
        # Вызов многострочный и несёт второй аргумент ownerRevision (строгий
        # CAS-lease). Фиксируем СМЫСЛ — stopToken уходит как generationToken —
        # а не однострочную форму записи.
        self.assertRegex(
            source,
            re.compile(
                r"quickCaptureStopRequest\(\s*"
                r"generationToken:\s*stopToken\b",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            source,
            re.compile(
                r"stopOrphanQuickCapture\(.*?"
                r"quickCaptureStopRequest\(\s*"
                r"generationToken:\s*generationToken",
                re.DOTALL,
            ),
        )
        self.assertIn(
            'var params: [String: Any] = ["source": "quick_capture"]',
            source,
        )
        self.assertIn(
            'params["generation_token"] = generationToken',
            source,
        )
        self.assertIn("RecordingStopCoordinator.execute", source)


if __name__ == "__main__":
    unittest.main()
