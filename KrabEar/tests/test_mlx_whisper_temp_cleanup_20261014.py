"""Временный WAV удаляется при частичной записи и любом исходе worker-вызова."""
from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import mlx_whisper_session as worker


class WhisperTempCleanupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="whisper-cleanup-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.paths = []
        original_mkstemp = tempfile.mkstemp

        def mkstemp(**kwargs):
            fd, path = original_mkstemp(dir=self.directory, **kwargs)
            self.paths.append(Path(path))
            return fd, path

        self.mkstemp_patch = patch.object(worker.tempfile, "mkstemp", mkstemp)
        self.mkstemp_patch.start()
        self.addCleanup(self.mkstemp_patch.stop)

    def test_partial_write_error_is_preserved_and_file_removed(self):
        failure = OSError(errno.ENOSPC, "synthetic disk full")

        def partial_write(path, _audio):
            Path(path).write_bytes(b"RIFF-partial-synthetic-audio")
            raise failure

        with (
            patch.object(worker, "_write_pcm_wav", side_effect=partial_write),
            patch.object(worker, "get_mlx_whisper_session") as get_session,
            self.assertRaises(OSError) as caught,
        ):
            worker.transcribe_via_mlx_worker(object(), {}, 1.0, "synthetic-model")

        self.assertIs(caught.exception, failure)
        self.assertEqual(len(self.paths), 1)
        self.assertFalse(self.paths[0].exists())
        get_session.assert_not_called()

    def test_success_keeps_file_until_transcribe_then_removes_it(self):
        expected = {"text": "synthetic result", "segments": []}
        params = {"language": "ru"}

        def transcribe(path, received_params, **kwargs):
            self.assertEqual(Path(path).read_bytes(), b"synthetic-pcm-wav")
            self.assertIs(received_params, params)
            self.assertEqual(set(kwargs), {"timeout_sec", "model_name"})
            self.assertEqual(kwargs["model_name"], "synthetic-model")
            self.assertGreater(kwargs["timeout_sec"], 0.0)
            self.assertLessEqual(kwargs["timeout_sec"], 1.5)
            return expected

        session = Mock(transcribe=Mock(side_effect=transcribe))
        with (
            patch.object(worker, "_write_pcm_wav", side_effect=self._write_synthetic),
            patch.object(worker, "get_mlx_whisper_session", return_value=session),
        ):
            result = worker.transcribe_via_mlx_worker(
                object(), params, 1.5, "synthetic-model",
            )

        self.assertIs(result, expected)
        session.transcribe.assert_called_once()
        self.assertFalse(self.paths[0].exists())

    def test_transcribe_error_is_preserved_and_file_removed(self):
        failure = RuntimeError("synthetic worker failure")
        session = Mock(transcribe=Mock(side_effect=failure))
        with (
            patch.object(worker, "_write_pcm_wav", side_effect=self._write_synthetic),
            patch.object(worker, "get_mlx_whisper_session", return_value=session),
            self.assertRaises(RuntimeError) as caught,
        ):
            worker.transcribe_via_mlx_worker(object(), {}, 1.0, "synthetic-model")

        self.assertIs(caught.exception, failure)
        self.assertFalse(self.paths[0].exists())

    def test_caller_owned_paths_are_forwarded_and_preserved(self):
        path = self.directory / "caller-owned.wav"
        path.write_bytes(b"synthetic-caller-audio")
        session = Mock(transcribe=Mock(return_value={"text": "synthetic result"}))
        for supplied in (path, str(path)):
            with (
                self.subTest(input_type=type(supplied).__name__),
                patch.object(worker, "_write_pcm_wav") as write,
                patch.object(worker, "get_mlx_whisper_session", return_value=session),
            ):
                worker.transcribe_via_mlx_worker(supplied, {}, 1.0, "synthetic-model")
                self.assertEqual(session.transcribe.call_args.args[0], str(path))
                self.assertEqual(path.read_bytes(), b"synthetic-caller-audio")
                write.assert_not_called()
        self.assertEqual(self.paths, [])

    @staticmethod
    def _write_synthetic(path, _audio):
        Path(path).write_bytes(b"synthetic-pcm-wav")
