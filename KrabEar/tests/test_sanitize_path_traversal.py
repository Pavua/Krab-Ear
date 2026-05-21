"""Wave 342 — тесты относительного path traversal в sanitize_path.

Wave 341 обнаружил: относительный путь вида ``../etc/passwd`` резолвится
через CWD (рабочую директорию процесса). Если CWD находится внутри home,
исходный код ошибочно пропускал такой путь через проверку allowed_dirs.

Исправление: явный отказ при relative paths (кроме тильды) до resolve().
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRAB_EAR_ROOT = PROJECT_ROOT / "KrabEar"
if str(KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(KRAB_EAR_ROOT))

from backend.input_sanitizer import InputSanitizer  # noqa: E402


class TestRelativePathTraversal(unittest.TestCase):
    """Проверяет, что относительные пути-traversal заблокированы (Wave 342)."""

    def setUp(self) -> None:
        self.san = InputSanitizer(allowed_dirs=[str(Path.home()), "/tmp"])

    # ------------------------------------------------------------------
    # Traversal cases — все должны быть заблокированы
    # ------------------------------------------------------------------

    def test_absolute_traversal_blocked(self) -> None:
        """Абсолютный path traversal: /etc/passwd."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("/etc/passwd")

    def test_relative_single_dotdot_blocked(self) -> None:
        """Относительный путь: ../etc/passwd — CWD-relative, должен быть заблокирован."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("../etc/passwd")

    def test_relative_traversal_blocked_from_cwd(self) -> None:
        """Путь ../../etc/passwd, разрешается через CWD — заблокирован."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("../../etc/passwd")

    def test_mixed_traversal_blocked(self) -> None:
        """Смешанный: foo/../../../etc/passwd — нормализуется к /etc/passwd."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("foo/../../../etc/passwd")

    def test_tilde_with_traversal_blocked(self) -> None:
        """Тильда + traversal: ~/Documents/../../../etc/passwd."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("~/Documents/../../../etc/passwd")

    def test_bare_relative_path_blocked(self) -> None:
        """Просто относительный путь без traversal: subdir/file.txt."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("subdir/file.txt")

    def test_dot_relative_path_blocked(self) -> None:
        """./file.txt — относительный, должен быть заблокирован."""
        with self.assertRaises(ValueError):
            self.san.sanitize_path("./file.txt")

    # ------------------------------------------------------------------
    # Valid paths — должны проходить
    # ------------------------------------------------------------------

    def test_allowed_path_passes(self) -> None:
        """Абсолютный путь внутри home — разрешён."""
        p = str(Path.home() / "Documents" / "recording.wav")
        result = self.san.sanitize_path(p)
        self.assertTrue(Path(result).is_absolute())
        self.assertTrue(result.startswith(str(Path.home())))

    def test_allowed_tmp_path_passes(self) -> None:
        """Путь в /tmp — разрешён."""
        result = self.san.sanitize_path("/tmp/krabear_test.wav")
        # macOS resolves /tmp → /private/tmp
        self.assertTrue(result.startswith("/tmp") or result.startswith("/private/tmp"))

    def test_tilde_home_path_passes(self) -> None:
        """Тильда разворачивается и проверяется как абсолютный путь."""
        result = self.san.sanitize_path("~/Documents/notes.txt")
        self.assertTrue(Path(result).is_absolute())
        self.assertTrue(result.startswith(str(Path.home())))

    def test_dotdot_normalised_within_allowed(self) -> None:
        """~/Downloads/../Documents/file.txt нормализуется к ~/Documents/file.txt — OK."""
        p = "~/Downloads/../Documents/session.ndjson"
        result = self.san.sanitize_path(p)
        expected = str((Path.home() / "Documents" / "session.ndjson").resolve())
        self.assertEqual(result, expected)

    # ------------------------------------------------------------------
    # Symlink handling
    # ------------------------------------------------------------------

    def test_symlink_in_allowed_dir_handled(self) -> None:
        """Симлинк внутри /tmp → цель тоже в /tmp — разрешён."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_file = Path(tmpdir) / "real.wav"
            real_file.touch()
            link = Path(tmpdir) / "link.wav"
            link.symlink_to(real_file)

            san = InputSanitizer(allowed_dirs=[tmpdir])
            result = san.sanitize_path(str(link))
            # resolve() follows the symlink — result is the real file
            self.assertEqual(result, str(real_file.resolve()))

    def test_symlink_escaping_allowed_dir_blocked(self) -> None:
        """Симлинк из /tmp → /etc/passwd: resolve() выходит за allowed_dirs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            link = Path(tmpdir) / "escape.txt"
            link.symlink_to("/etc/passwd")

            san = InputSanitizer(allowed_dirs=[tmpdir])
            with self.assertRaises(ValueError):
                san.sanitize_path(str(link))

    # ------------------------------------------------------------------
    # Concurrent safety
    # ------------------------------------------------------------------

    def test_concurrent_sanitize(self) -> None:
        """Параллельный sanitize_path из 30 потоков не вызывает гонок."""
        san = InputSanitizer(allowed_dirs=[str(Path.home()), "/tmp"])
        errors: list[Exception] = []

        def worker(idx: int) -> None:
            try:
                # Mix of valid and invalid paths
                if idx % 3 == 0:
                    san.sanitize_path(str(Path.home() / f"file_{idx}.wav"))
                elif idx % 3 == 1:
                    try:
                        san.sanitize_path(f"../etc/passwd_{idx}")
                    except ValueError:
                        pass  # expected
                else:
                    san.sanitize_path("/tmp/safe.wav")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")


if __name__ == "__main__":
    unittest.main()
