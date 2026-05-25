"""Тесты ротации логов: backend.log (RotatingFileHandler) + configure_logging."""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_logging(data_dir: Path, log_format: str = "text") -> list[logging.Handler]:
    """Call configure_logging и вернуть handlers, добавленные в root logger."""
    from unittest.mock import patch
    from backend import service as svc_module

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    for h in root.handlers[:]:
        root.removeHandler(h)

    with patch.object(svc_module.settings, "LOG_FORMAT", log_format):
        svc_module.configure_logging(data_dir)

    added = root.handlers[:]
    # Restore
    for h in added:
        h.close()
        root.removeHandler(h)
    for h in original_handlers:
        root.addHandler(h)
    root.setLevel(original_level)
    return added


# ---------------------------------------------------------------------------
# A. RotatingFileHandler присутствует и правильно сконфигурирован
# ---------------------------------------------------------------------------

class TestRotatingFileHandlerConfig(unittest.TestCase):
    """configure_logging использует RotatingFileHandler с правильными параметрами."""

    def _get_rotating_handler(self, data_dir: Path):
        from logging.handlers import RotatingFileHandler
        handlers = _setup_logging(data_dir)
        rotating = [h for h in handlers if isinstance(h, RotatingFileHandler)]
        self.assertEqual(len(rotating), 1, "Должен быть ровно один RotatingFileHandler")
        return rotating[0]

    def test_rotating_handler_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            from logging.handlers import RotatingFileHandler
            handlers = _setup_logging(data_dir)
            types = [type(h).__name__ for h in handlers]
            self.assertIn("RotatingFileHandler", types)

    def test_max_bytes_is_5mb(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = self._get_rotating_handler(Path(tmp) / "data")
            self.assertEqual(h.maxBytes, 5 * 1024 * 1024,
                             "maxBytes должен быть 5 MB = 5*1024*1024")

    def test_backup_count_is_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = self._get_rotating_handler(Path(tmp) / "data")
            self.assertEqual(h.backupCount, 3,
                             "backupCount должен быть 3")

    def test_log_file_named_backend_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            h = self._get_rotating_handler(data_dir)
            self.assertEqual(Path(h.baseFilename).name, "backend.log")

    def test_encoding_is_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = self._get_rotating_handler(Path(tmp) / "data")
            self.assertEqual(h.encoding, "utf-8")

    def test_stream_handler_also_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            handlers = _setup_logging(Path(tmp) / "data")
            stream_handlers = [h for h in handlers if type(h).__name__ == "StreamHandler"]
            self.assertGreaterEqual(len(stream_handlers), 1,
                                    "StreamHandler (stdout) должен присутствовать")


# ---------------------------------------------------------------------------
# B. Фактическая ротация при превышении maxBytes
# ---------------------------------------------------------------------------

class TestRotationBehavior(unittest.TestCase):
    """RotatingFileHandler реально ротирует при достижении maxBytes."""

    def _configure_with_small_limit(self, data_dir: Path, max_bytes: int = 200):
        """Настраивает RotatingFileHandler с маленьким лимитом для теста."""
        from logging.handlers import RotatingFileHandler

        data_dir.mkdir(parents=True, exist_ok=True)
        log_path = data_dir / "backend.log"
        handler = RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=3, encoding="utf-8"
        )
        formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
        handler.setFormatter(formatter)

        root = logging.getLogger("rotation_test")
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        return root, handler, log_path

    def test_rotation_creates_backup_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            root, handler, log_path = self._configure_with_small_limit(data_dir, max_bytes=200)
            try:
                # Пишем достаточно для превышения 200 байт
                for i in range(20):
                    root.info("A" * 30 + f" {i}")

                backup = data_dir / "backend.log.1"
                self.assertTrue(backup.exists(),
                                "backend.log.1 должен появиться после ротации")
            finally:
                handler.close()
                root.removeHandler(handler)

    def test_backup_count_not_exceeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            root, handler, log_path = self._configure_with_small_limit(data_dir, max_bytes=150)
            try:
                # Пишем много, чтобы вызвать несколько ротаций
                for i in range(100):
                    root.info("B" * 50 + f" {i}")

                # Не должно быть .4 или выше при backupCount=3
                backup4 = data_dir / "backend.log.4"
                self.assertFalse(backup4.exists(),
                                 "backend.log.4 не должен существовать при backupCount=3")
            finally:
                handler.close()
                root.removeHandler(handler)

    def test_main_log_survives_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            root, handler, log_path = self._configure_with_small_limit(data_dir, max_bytes=200)
            try:
                for i in range(30):
                    root.info("C" * 30 + f" msg {i}")
                self.assertTrue(log_path.exists(),
                                "backend.log должен существовать после ротации")
            finally:
                handler.close()
                root.removeHandler(handler)

    def test_all_backups_exist_after_multiple_rotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            root, handler, log_path = self._configure_with_small_limit(data_dir, max_bytes=100)
            try:
                for i in range(200):
                    root.info("D" * 30 + f" {i}")

                # Должны существовать backend.log + .1 + .2 + .3
                self.assertTrue(log_path.exists(), "backend.log должен существовать")
                self.assertTrue((data_dir / "backend.log.1").exists(), ".1 должен существовать")
                self.assertTrue((data_dir / "backend.log.2").exists(), ".2 должен существовать")
                self.assertTrue((data_dir / "backend.log.3").exists(), ".3 должен существовать")
            finally:
                handler.close()
                root.removeHandler(handler)

    def test_data_not_lost_across_rotation(self):
        """Данные из первой ротации сохраняются в .1.

        Стратегия: записываем маркер, затем достаточно данных для ОДНОЙ
        ротации (maxBytes=500, ~8 сообщений). Маркер уйдёт в .1 и не
        выйдет за пределы backupCount=3.
        """
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            root, handler, log_path = self._configure_with_small_limit(data_dir, max_bytes=500)
            marker = "UNIQUE_MARKER_XYZ_42"
            try:
                root.info(marker)
                # ~8 сообщений вызывают ровно одну ротацию при maxBytes=500.
                for i in range(8):
                    root.info("E" * 30 + f" {i}")

                # Маркер должен быть в backend.log.1
                all_content = ""
                for suffix in ["", ".1", ".2", ".3"]:
                    p = data_dir / f"backend.log{suffix}"
                    if p.exists():
                        all_content += p.read_text(encoding="utf-8")

                self.assertIn(marker, all_content,
                              "Данные не должны теряться при первой ротации")
            finally:
                handler.close()
                root.removeHandler(handler)


# ---------------------------------------------------------------------------
# C. configure_logging интеграция с ротацией
# ---------------------------------------------------------------------------

class TestConfigureLoggingRotation(unittest.TestCase):
    """configure_logging создаёт RotatingFileHandler с правильными значениями."""

    def test_configure_logging_text_has_rotating_handler(self):
        from logging.handlers import RotatingFileHandler
        from unittest.mock import patch
        from backend import service as svc_module

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            root = logging.getLogger()
            old_handlers = root.handlers[:]
            old_level = root.level
            for h in root.handlers[:]:
                root.removeHandler(h)
            try:
                with patch.object(svc_module.settings, "LOG_FORMAT", "text"):
                    svc_module.configure_logging(data_dir)
                rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
                self.assertEqual(len(rotating), 1)
                self.assertEqual(rotating[0].maxBytes, 5 * 1024 * 1024)
                self.assertEqual(rotating[0].backupCount, 3)
            finally:
                for h in root.handlers[:]:
                    h.close()
                    root.removeHandler(h)
                for h in old_handlers:
                    root.addHandler(h)
                root.setLevel(old_level)

    def test_configure_logging_json_has_rotating_handler(self):
        from logging.handlers import RotatingFileHandler
        from unittest.mock import patch
        from backend import service as svc_module

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            root = logging.getLogger()
            old_handlers = root.handlers[:]
            old_level = root.level
            for h in root.handlers[:]:
                root.removeHandler(h)
            try:
                with patch.object(svc_module.settings, "LOG_FORMAT", "json"):
                    svc_module.configure_logging(data_dir)
                rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
                self.assertEqual(len(rotating), 1)
                self.assertEqual(rotating[0].maxBytes, 5 * 1024 * 1024)
                self.assertEqual(rotating[0].backupCount, 3)
            finally:
                for h in root.handlers[:]:
                    h.close()
                    root.removeHandler(h)
                for h in old_handlers:
                    root.addHandler(h)
                root.setLevel(old_level)


if __name__ == "__main__":
    unittest.main()
