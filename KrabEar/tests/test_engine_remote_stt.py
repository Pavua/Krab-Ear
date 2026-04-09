"""Тесты для AudioEngine._transcribe_remote: обработка str path и numpy ndarray."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TranscribeRemoteTestCase(unittest.TestCase):
    """_transcribe_remote должен работать и для str-путей, и для ndarray."""

    def setUp(self):
        from core.engine import AudioEngine
        from core.config import settings
        os.makedirs(str(settings.DATA_DIR), exist_ok=True)
        self.engine = AudioEngine()

    def _mock_ok_response(self, text: str = "remote result"):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"text": text}
        resp.raise_for_status.return_value = None
        return resp

    @patch("core.engine.requests.post")
    def test_ndarray_input_serializes_to_temp_wav(self, mock_post):
        """np.ndarray → temp WAV → HTTP POST → success."""
        import numpy as np
        mock_post.return_value = self._mock_ok_response("ok")
        audio = np.zeros(16000, dtype=np.float32)  # 1s silence @ 16kHz

        result = self.engine._transcribe_remote(audio, "test prompt")

        self.assertEqual(result["text"], "ok")
        self.assertEqual(result["engine"], "remote")
        # Verify the file handle kwarg was a .wav tempfile
        _, kwargs = mock_post.call_args
        file_tuple = kwargs["files"]["file"]
        self.assertTrue(file_tuple[0].endswith(".wav"))

    @patch("core.engine.requests.post")
    def test_str_path_input_still_works(self, mock_post):
        """Существующий use case со str-path тоже работает (backward compat)."""
        import tempfile
        mock_post.return_value = self._mock_ok_response("file ok")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(b"RIFF\x00\x00\x00\x00WAVEfmt ")  # minimal WAV header fragment
            tmp_path = tmp.name

        try:
            result = self.engine._transcribe_remote(tmp_path, "prompt")
            self.assertEqual(result["text"], "file ok")
            self.assertEqual(result["engine"], "remote")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    @patch("core.engine.requests.post")
    def test_ndarray_temp_file_cleaned_up_after_success(self, mock_post):
        """После успешного вызова temp .wav удалён."""
        import numpy as np
        captured_paths = []

        def capture_and_respond(*args, **kwargs):
            # Extract filename from files kwarg
            file_tuple = kwargs["files"]["file"]
            captured_paths.append(file_tuple[0])
            return self._mock_ok_response("ok")

        mock_post.side_effect = capture_and_respond
        audio = np.zeros(8000, dtype=np.float32)
        self.engine._transcribe_remote(audio, "p")

        self.assertEqual(len(captured_paths), 1)
        filename = captured_paths[0]
        # The temp file basename is just the filename, full path is in settings.DATA_DIR
        from core.config import settings
        full_path = filename if os.path.isabs(filename) else os.path.join(str(settings.DATA_DIR), filename)
        self.assertFalse(
            os.path.exists(full_path),
            f"temp file {full_path} was not cleaned up after success",
        )

    @patch("core.engine.requests.post")
    def test_ndarray_temp_file_cleaned_up_on_http_failure(self, mock_post):
        """Temp WAV удаляется даже если HTTP fail."""
        import numpy as np
        mock_post.side_effect = RuntimeError("connection refused")
        audio = np.zeros(8000, dtype=np.float32)

        # Count temp files in DATA_DIR before/after
        from core.config import settings
        data_dir = Path(str(settings.DATA_DIR))
        wav_files_before = set(data_dir.glob("*.wav")) if data_dir.exists() else set()

        with self.assertRaises(RuntimeError):
            self.engine._transcribe_remote(audio, "p")

        wav_files_after = set(data_dir.glob("*.wav")) if data_dir.exists() else set()
        new_files = wav_files_after - wav_files_before
        self.assertEqual(
            len(new_files), 0,
            f"temp WAV files leaked after HTTP failure: {new_files}",
        )

    def test_unsupported_type_raises_type_error(self):
        """Неподдерживаемый тип audio_data → TypeError c информативным сообщением."""
        with self.assertRaises(TypeError) as ctx:
            self.engine._transcribe_remote(12345, "prompt")
        self.assertIn("unsupported audio_data type", str(ctx.exception))
        self.assertIn("int", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
