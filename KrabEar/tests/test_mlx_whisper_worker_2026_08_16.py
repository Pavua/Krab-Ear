"""P0c 2026-08-16: mlx_whisper в OS-worker, SEGV не убивает REST PID.

Живой инцидент: ai.krab.ear.rest, Homebrew Python 3.14.6, EXC_BAD_ACCESS
в потоке whisper-large-v3-turbo. core/mlx_subprocess.py — in-process watchdog,
не изоляция PID. Handoff: docs/HANDOFF_WHISPER_TURBO_SEGV_2026-08-16_RU.md
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class MlxWhisperWorkerFlagTest(unittest.TestCase):
    def test_env_zero_wins_over_rest_argv(self):
        from core.mlx_whisper_session import mlx_whisper_worker_enabled

        with patch.dict("os.environ", {"KRAB_EAR_MLX_WHISPER_WORKER": "0"}, clear=False):
            self.assertFalse(
                mlx_whisper_worker_enabled(
                    argv=["python", "/app/KrabEar/backend/rest_server.py"]
                )
            )

    def test_env_one_enables(self):
        from core.mlx_whisper_session import mlx_whisper_worker_enabled

        with patch.dict("os.environ", {"KRAB_EAR_MLX_WHISPER_WORKER": "1"}, clear=False):
            self.assertTrue(mlx_whisper_worker_enabled(argv=["pytest"]))

    def test_rest_argv_enables_without_env(self):
        from core.mlx_whisper_session import mlx_whisper_worker_enabled

        env = {k: v for k, v in __import__("os").environ.items() if k != "KRAB_EAR_MLX_WHISPER_WORKER"}
        with patch.dict("os.environ", env, clear=True):
            self.assertTrue(
                mlx_whisper_worker_enabled(
                    argv=["python", "KrabEar/backend/rest_server.py"]
                )
            )

    def test_pytest_argv_stays_off_without_env(self):
        from core.mlx_whisper_session import mlx_whisper_worker_enabled

        env = {k: v for k, v in __import__("os").environ.items() if k != "KRAB_EAR_MLX_WHISPER_WORKER"}
        with patch.dict("os.environ", env, clear=True):
            self.assertFalse(
                mlx_whisper_worker_enabled(argv=["pytest", "KrabEar/tests/test_x.py"])
            )


class RestPlistWorkerEnvTest(unittest.TestCase):
    def test_rest_launchagent_enables_mlx_whisper_worker(self):
        # plistlib ломается на XML-комментариях с "--" в этом шаблоне
        # (тот же класс, что backend-plist). Читаем текст.
        template = (
            REPO_ROOT
            / "KrabEar"
            / "launchagents"
            / "ai.krab.ear.rest.plist.template"
        )
        text = template.read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"<key>KRAB_EAR_MLX_WHISPER_WORKER</key>\s*<string>1</string>",
        )


class MlxWhisperSessionProtocolTest(unittest.TestCase):
    def tearDown(self):
        try:
            from core.mlx_whisper_session import reset_mlx_whisper_session

            reset_mlx_whisper_session()
        except Exception:
            pass

    def _fake_popen(self, stdout_line: str | None, returncode: int | None):
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = MagicMock()
        proc.stdout.readline.return_value = stdout_line if stdout_line is not None else ""
        proc.stderr.readline.return_value = ""
        proc.stderr.read.return_value = ""
        proc.poll.return_value = returncode
        proc.pid = 4242
        return proc

    def test_empty_read_after_sigsegv_raises_crashed(self):
        from core.mlx_whisper_session import (
            MLXWorkerCrashed,
            get_mlx_whisper_session,
        )

        proc = self._fake_popen("", -11)
        session = get_mlx_whisper_session()
        with patch("core.mlx_whisper_session.subprocess.Popen", return_value=proc):
            session.start()
            with self.assertRaises(MLXWorkerCrashed) as ctx:
                session.transcribe(
                    "/tmp/a.wav",
                    {"path_or_hf_repo": "mlx-community/whisper-large-v3-turbo"},
                    timeout_sec=2.0,
                    model_name="turbo",
                )
        self.assertEqual(ctx.exception.returncode, -11)
        self.assertIsNone(session._proc)

    def test_dead_child_respawns_before_next_transcribe(self):
        """W1216 F1 sibling: poll()!=None → новый child, этот запрос успешен."""
        from core.mlx_whisper_session import get_mlx_whisper_session

        dead = self._fake_popen("", -11)
        live = self._fake_popen(
            '{"ok": true, "result": {"text": "ok", "segments": []}}\n',
            None,
        )
        pops = [dead, live]
        session = get_mlx_whisper_session()
        with patch(
            "core.mlx_whisper_session.subprocess.Popen",
            side_effect=lambda *a, **k: pops.pop(0),
        ):
            session.start()
            self.assertIs(session._proc, dead)
            result = session.transcribe(
                "/tmp/a.wav",
                {"path_or_hf_repo": "mlx-community/whisper-large-v3-turbo"},
                timeout_sec=2.0,
                model_name="turbo",
            )
        self.assertEqual(result["text"], "ok")
        self.assertIs(session._proc, live)
        self.assertEqual(pops, [])

    def test_live_child_start_does_not_respawn(self):
        from core.mlx_whisper_session import get_mlx_whisper_session

        live = self._fake_popen(
            '{"ok": true, "result": {"text": "ok", "segments": []}}\n',
            None,
        )
        session = get_mlx_whisper_session()
        with patch(
            "core.mlx_whisper_session.subprocess.Popen",
            return_value=live,
        ) as popen:
            session.start()
            session.start()
            result = session.transcribe(
                "/tmp/a.wav",
                {"path_or_hf_repo": "mlx-community/whisper-large-v3-turbo"},
                timeout_sec=2.0,
                model_name="turbo",
            )
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(result["text"], "ok")
        self.assertIs(session._proc, live)

    def test_ok_json_returns_result_dict(self):
        from core.mlx_whisper_session import get_mlx_whisper_session

        payload = '{"ok": true, "result": {"text": "привет", "segments": []}}\n'
        proc = self._fake_popen(payload, None)
        session = get_mlx_whisper_session()
        with patch("core.mlx_whisper_session.subprocess.Popen", return_value=proc):
            session.start()
            result = session.transcribe(
                "/tmp/a.wav",
                {"path_or_hf_repo": "mlx-community/whisper-large-v3-turbo"},
                timeout_sec=2.0,
                model_name="turbo",
            )
        self.assertEqual(result["text"], "привет")
        self.assertEqual(result["segments"], [])

    def test_invalid_json_resets_session(self):
        from core.mlx_whisper_session import (
            MLXWorkerCrashed,
            get_mlx_whisper_session,
        )

        proc = self._fake_popen("not-json\n", None)
        session = get_mlx_whisper_session()
        with patch("core.mlx_whisper_session.subprocess.Popen", return_value=proc):
            session.start()
            with self.assertRaises(MLXWorkerCrashed):
                session.transcribe(
                    "/tmp/a.wav",
                    {"path_or_hf_repo": "mlx-community/whisper-large-v3-turbo"},
                    timeout_sec=2.0,
                    model_name="turbo",
                )
        self.assertIsNone(session._proc)

    def test_kill_does_not_wait_on_shutdown_json(self):
        from core.mlx_whisper_session import (
            get_mlx_whisper_session,
            kill_mlx_whisper_session,
        )

        proc = self._fake_popen("", None)
        session = get_mlx_whisper_session()
        with patch("core.mlx_whisper_session.subprocess.Popen", return_value=proc):
            session.start()
            kill_mlx_whisper_session()
        proc.kill.assert_called()
        proc.wait.assert_not_called()
        from core.mlx_whisper_session import get_mlx_whisper_session as get_again
        self.assertIsNot(session, get_again())

    def test_typeerror_payload_raises_typeerror(self):
        from core.mlx_whisper_session import get_mlx_whisper_session

        payload = '{"ok": false, "error": "TypeError: unexpected keyword argument \'no_speech_threshold\'"}\n'
        proc = self._fake_popen(payload, None)
        session = get_mlx_whisper_session()
        with patch("core.mlx_whisper_session.subprocess.Popen", return_value=proc):
            session.start()
            with self.assertRaises(TypeError):
                session.transcribe(
                    "/tmp/a.wav",
                    {
                        "path_or_hf_repo": "mlx-community/whisper-large-v3-turbo",
                        "no_speech_threshold": 0.6,
                    },
                    timeout_sec=2.0,
                    model_name="turbo",
                )


class EngineRoutesToWorkerTest(unittest.TestCase):
    def _make_engine(self):
        from core.engine import AudioEngine

        with patch("core.engine.threading.Thread.start", autospec=True):
            engine = AudioEngine.__new__(AudioEngine)
        engine.current_model = "mlx-community/whisper-large-v3-turbo"
        engine.quality_profile = "balanced"
        engine._unavailable_models = {}
        engine._error_bus = MagicMock()
        engine._llm_rewriter = None
        engine._settings_get = lambda k, d: d
        return engine

    def test_transcribe_model_uses_worker_not_watchdog(self):
        import numpy as np

        engine = self._make_engine()
        audio = np.zeros(16000, dtype=np.float32)
        expected = {"text": "ok", "segments": []}
        watchdog = MagicMock()

        with (
            patch("core.mlx_whisper_session.mlx_whisper_worker_enabled", return_value=True),
            patch(
                "core.mlx_whisper_session.transcribe_via_mlx_worker",
                return_value=expected,
            ) as mock_worker,
            patch("core.engine.get_watchdog", return_value=watchdog),
            patch("core.engine.mlx_lock"),
            patch("core.engine.mlx_inter_process_lock"),
            patch("core.engine.settings") as mock_settings,
        ):
            mock_settings.TRANSCRIBE_LANGUAGE = "ru"
            mock_settings.MLX_CRASH_RECOVERY_ENABLED = True
            mock_settings.MLX_TRANSCRIBE_TIMEOUT_SEC = 45.0
            result = engine._transcribe_model(audio, "mlx-community/whisper-large-v3-turbo", "")

        self.assertEqual(result, expected)
        mock_worker.assert_called()
        watchdog.run_with_timeout.assert_not_called()

    def test_worker_crash_does_not_call_watchdog(self):
        import numpy as np
        from core.mlx_whisper_session import MLXWorkerCrashed

        engine = self._make_engine()
        audio = np.zeros(16000, dtype=np.float32)
        watchdog = MagicMock()
        crashed = MLXWorkerCrashed(returncode=-11, model_name="turbo")

        with (
            patch("core.mlx_whisper_session.mlx_whisper_worker_enabled", return_value=True),
            patch(
                "core.mlx_whisper_session.transcribe_via_mlx_worker",
                side_effect=crashed,
            ),
            patch("core.engine.get_watchdog", return_value=watchdog),
            patch("core.engine.mlx_lock"),
            patch("core.engine.mlx_inter_process_lock"),
            patch("core.engine.settings") as mock_settings,
        ):
            mock_settings.TRANSCRIBE_LANGUAGE = "ru"
            mock_settings.MLX_CRASH_RECOVERY_ENABLED = True
            mock_settings.MLX_TRANSCRIBE_TIMEOUT_SEC = 45.0
            with self.assertRaises(MLXWorkerCrashed):
                engine._transcribe_model(audio, "mlx-community/whisper-large-v3-turbo", "")

        watchdog.run_with_timeout.assert_not_called()

    def test_warmup_uses_worker_not_inprocess_mlx(self):
        engine = self._make_engine()
        mock_mlx = MagicMock()

        with (
            patch("core.engine.mlx_whisper", mock_mlx),
            patch("core.mlx_whisper_session.mlx_whisper_worker_enabled", return_value=True),
            patch(
                "core.mlx_whisper_session.transcribe_via_mlx_worker",
                return_value={"text": ""},
            ) as mock_worker,
            patch("core.engine.mlx_lock"),
            patch("core.engine.mlx_inter_process_lock"),
        ):
            result = engine.warmup()

        self.assertTrue(result["loaded"])
        mock_worker.assert_called_once()
        mock_mlx.transcribe.assert_not_called()


class AdapterRoutesToWorkerTest(unittest.TestCase):
    def test_adapter_transcribe_uses_worker(self):
        import numpy as np
        from core.pipeline.stt_whisper_mlx_adapter import WhisperMLXAdapter

        adapter = WhisperMLXAdapter(model_path="mlx-community/whisper-large-v3-turbo")
        audio = np.zeros(16000, dtype=np.float32)

        with (
            patch("core.mlx_whisper_session.mlx_whisper_worker_enabled", return_value=True),
            patch(
                "core.mlx_whisper_session.transcribe_via_mlx_worker",
                return_value={"text": "hola", "language": "es"},
            ) as mock_worker,
            patch("core.pipeline.stt_whisper_mlx_adapter.get_watchdog", MagicMock()),
        ):
            result = adapter.transcribe(audio, language="es")

        self.assertEqual(result.text, "hola")
        mock_worker.assert_called()


class RestPoisonedExitKillsWorkerTest(unittest.TestCase):
    def test_poisoned_exit_kills_worker_before_os_exit(self):
        import backend.rest_server as rs

        with (
            patch.object(rs.settings, "REST_IN_PROCESS_ENABLED", False),
            patch("core.mlx_whisper_session.kill_mlx_whisper_session") as kill,
            patch.object(rs.os, "_exit") as exit_fn,
        ):
            rs._exit_poisoned_rest_process(70)
        kill.assert_called_once()
        exit_fn.assert_called_once_with(70)

    def test_atexit_closes_mlx_session_when_not_adopted(self):
        import backend.rest_server as rs

        with (
            patch.object(rs, "_singletons_adopted", False),
            patch("core.mlx_whisper_session.close_mlx_whisper_session") as close,
        ):
            rs._rest_engine_cleanup()
        close.assert_called_once()

    def test_atexit_skips_mlx_session_when_adopted(self):
        import backend.rest_server as rs

        with (
            patch.object(rs, "_singletons_adopted", True),
            patch("core.mlx_whisper_session.close_mlx_whisper_session") as close,
        ):
            rs._rest_engine_cleanup()
        close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
