"""2026-08-05: throwaway/e2e backend-инстансы не должны слать в прод-Sentry.

Живой инцидент: SENTRY_DSN — глобальная настройка (.env), не завязана на
--data-dir. Три раунда e2e-тестирования этой же сессии (throwaway backend на
tempfile.TemporaryDirectory()) породили 3 отдельных прод-Sentry события
(missing mlx_whisper, recording.long_duration_warning, shutdown-барьер) —
чистый тестовый шум, неотличимый от реальных проблем без ручной проверки
sys.argv в каждом событии.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.service import _is_throwaway_data_dir


class TestIsThrowawayDataDir(unittest.TestCase):
    def test_true_for_path_under_system_temp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(_is_throwaway_data_dir(Path(d)))

    def test_true_for_nested_path_under_system_temp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            nested = Path(d) / "sub" / "data"
            self.assertTrue(_is_throwaway_data_dir(nested))

    def test_true_for_bare_slash_tmp_path(self):
        """2026-08-05, Fable HIGH: собственные штатные e2e-скрипты проекта
        (scripts/run_e2e_smokes.command, run_e2e_bridge_smoke.command) кладут
        data-dir буквально в /tmp/krab_ear_e2e.XXXXXX — НЕ через
        tempfile.gettempdir() (тот на macOS резолвится в /private/var/
        folders/.../T, другой путь). Живая проверка на macOS подтвердила:
        этот кейс НЕ ловился gettempdir()-only версией — этот тест упал бы
        до фикса, доказывая разрыв."""
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            self.assertTrue(_is_throwaway_data_dir(Path(d)))

    def test_false_for_production_application_support_path(self):
        prod = Path.home() / "Library" / "Application Support" / "KrabEar"
        self.assertFalse(_is_throwaway_data_dir(prod))

    def test_false_for_dev_home_data_dir(self):
        dev = Path.home() / ".krab_ear_data"
        self.assertFalse(_is_throwaway_data_dir(dev))

    def test_never_raises_on_weird_path(self):
        # Relative path без resolve() контекста — не должно бросать.
        self.assertIsInstance(_is_throwaway_data_dir(Path("relative/path")), bool)

    def test_fails_open_when_gettempdir_raises(self):
        """2026-08-05 (Fable LOW test-gap): exception-путь реально непокрыт
        без этого — patch именно ту ветку, которая должна fail-open в сторону
        ВКЛЮЧЁННОГО Sentry (False = не throwaway), не молча отключить прод."""
        with patch("tempfile.gettempdir", side_effect=OSError("boom")):
            self.assertFalse(_is_throwaway_data_dir(Path.home() / "some_dir"))


if __name__ == "__main__":
    unittest.main()
