"""Тесты iCloud workaround в AudioEngine: auto-copy для заблокированных файлов.

Проверяем:
- _is_icloud_path() корректно определяет пути с Mobile Documents / CloudDocs
- _needs_icloud_copy() возвращает True при errno 11 (EDEADLK)
- _copy_to_tmp_with_icloud_download() вызывает brctl и возвращает путь к копии
- Интеграция в transcribe_file_async: iCloud-путь → auto-copy triggered
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import (  # noqa: E402
    _ICLOUD_PATH_MARKERS,
    _copy_to_tmp_with_icloud_download,
    _is_icloud_path,
    _needs_icloud_copy,
)


class ICloudPathDetectionTests(unittest.TestCase):
    """_is_icloud_path() должна распознавать все известные iCloud маркеры."""

    def test_mobile_documents_detected(self):
        path = "/Users/user/Library/Mobile Documents/com~apple~CloudDocs/Downloads/audio.m4a"
        self.assertTrue(_is_icloud_path(path))

    def test_com_apple_cloud_docs_detected(self):
        path = "/Users/user/Library/Mobile Documents/com~apple~CloudDocs/audio.m4a"
        self.assertTrue(_is_icloud_path(path))

    def test_icloud_tilde_prefix_detected(self):
        path = "/Users/user/Library/Mobile Documents/iCloud~com~example~app/audio.mp3"
        self.assertTrue(_is_icloud_path(path))

    def test_cloud_docs_marker_detected(self):
        path = "/Volumes/iDisk/CloudDocs/voice/recording.wav"
        self.assertTrue(_is_icloud_path(path))

    def test_regular_path_not_detected(self):
        self.assertFalse(_is_icloud_path("/Users/user/Downloads/recording.m4a"))

    def test_desktop_path_not_detected(self):
        self.assertFalse(_is_icloud_path("/Users/user/Desktop/calls/audio.m4a"))

    def test_all_markers_covered(self):
        """Каждый маркер из _ICLOUD_PATH_MARKERS должен детектироваться."""
        for marker in _ICLOUD_PATH_MARKERS:
            path = f"/Users/user/{marker}/audio.m4a"
            self.assertTrue(
                _is_icloud_path(path),
                f"Маркер {marker!r} не детектируется",
            )


class NeedsICloudCopyTests(unittest.TestCase):
    """_needs_icloud_copy() должна возвращать True только при errno 11."""

    def test_returns_false_for_readable_file(self):
        with tempfile.NamedTemporaryFile(suffix=".m4a") as tmp:
            tmp.write(b"fake audio data")
            tmp.flush()
            self.assertFalse(_needs_icloud_copy(tmp.name))

    def test_returns_true_on_errno_11(self):
        err = OSError()
        err.errno = 11  # EDEADLK — iCloud placeholder
        with patch("builtins.open", side_effect=err):
            self.assertTrue(_needs_icloud_copy("/fake/path/audio.m4a"))

    def test_returns_false_on_other_oserror(self):
        err = OSError()
        err.errno = errno.ENOENT  # File not found — другая причина
        with patch("builtins.open", side_effect=err):
            self.assertFalse(_needs_icloud_copy("/fake/path/audio.m4a"))

    def test_returns_false_on_permission_error(self):
        err = OSError()
        err.errno = errno.EACCES
        with patch("builtins.open", side_effect=err):
            self.assertFalse(_needs_icloud_copy("/fake/path/audio.m4a"))


class CopyToTmpTests(unittest.TestCase):
    """_copy_to_tmp_with_icloud_download() должна копировать файл в /tmp."""

    def test_copies_normal_file(self):
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as src:
            src.write(b"audio content")
            src_path = src.name
        try:
            result = _copy_to_tmp_with_icloud_download(src_path)
            self.assertIsNotNone(result)
            self.assertTrue(os.path.exists(result))
            with open(result, "rb") as fh:
                self.assertEqual(fh.read(), b"audio content")
        finally:
            os.unlink(src_path)
            if result and os.path.exists(result):
                os.unlink(result)

    def test_suffix_preserved(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as src:
            src.write(b"wav data")
            src_path = src.name
        try:
            result = _copy_to_tmp_with_icloud_download(src_path)
            self.assertIsNotNone(result)
            self.assertTrue(result.endswith(".wav"))
        finally:
            os.unlink(src_path)
            if result and os.path.exists(result):
                os.unlink(result)

    def test_calls_brctl_for_placeholder(self):
        """Если файл размером 0 байт (placeholder), вызывается brctl download."""
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as src:
            src_path = src.name  # пустой файл = 0 байт

        brctl_calls = []

        def fake_run(cmd, **kwargs):
            brctl_calls.append(cmd)
            # После вызова brctl «симулируем» загрузку — записываем данные
            with open(src_path, "wb") as fh:
                fh.write(b"downloaded audio")
            result = MagicMock()
            result.returncode = 0
            return result

        try:
            with patch("subprocess.run", side_effect=fake_run):
                result = _copy_to_tmp_with_icloud_download(src_path)
            # brctl download должен был быть вызван
            brctl_invocations = [c for c in brctl_calls if c and c[0] == "brctl"]
            self.assertTrue(
                len(brctl_invocations) >= 1,
                f"brctl не был вызван; calls={brctl_calls}",
            )
            self.assertIsNotNone(result)
        finally:
            os.unlink(src_path)
            if result and os.path.exists(result):
                os.unlink(result)

    def test_returns_none_on_copy_error(self):
        with patch("shutil.copy2", side_effect=OSError("copy failed")):
            with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as src:
                src.write(b"data")
                src_path = src.name
            try:
                result = _copy_to_tmp_with_icloud_download(src_path)
                self.assertIsNone(result)
            finally:
                os.unlink(src_path)


class EngineICloudIntegrationTests(unittest.TestCase):
    """Проверяем, что AudioEngine вызывает auto-copy для iCloud-путей."""

    def _make_engine(self):
        from core.engine import AudioEngine
        return AudioEngine()

    def test_icloud_path_triggers_copy(self):
        """Если путь содержит 'Mobile Documents', _copy_to_tmp_with_icloud_download вызывается."""
        engine = self._make_engine()

        icloud_path = "/Users/user/Library/Mobile Documents/com~apple~CloudDocs/1 - Позвонить с Дашуля.m4a"

        # Создаём фейковый временный файл для «источника»
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as fake_src:
            fake_src.write(b"fake audio")
            fake_src_path = fake_src.name

        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as fake_tmp:
            fake_tmp.write(b"fake audio")
            fake_tmp_path = fake_tmp.name

        copied_paths = []

        def fake_copy(path):
            copied_paths.append(path)
            return fake_tmp_path

        try:
            with patch("core.engine._is_icloud_path", return_value=True), \
                 patch("core.engine._needs_icloud_copy", return_value=False), \
                 patch("core.engine._copy_to_tmp_with_icloud_download", side_effect=fake_copy) as mock_copy, \
                 patch("os.path.exists", return_value=True), \
                 patch("os.path.getsize", return_value=1024 * 100), \
                 patch.object(engine, "_transcribe_with_fallback", return_value={"text": "тест", "segments": []}), \
                 patch.object(engine, "_maybe_run_diarization", return_value={}), \
                 patch.object(engine, "_llm_rewrite_allowed", return_value=False):

                engine.transcribe(icloud_path)

            mock_copy.assert_called_once_with(icloud_path)
        finally:
            os.unlink(fake_src_path)
            # fake_tmp_path may already be unlinked by engine cleanup — ignore
            try:
                os.unlink(fake_tmp_path)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    unittest.main()
