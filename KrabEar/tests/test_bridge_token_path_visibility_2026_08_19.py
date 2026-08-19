"""Путь к токену моста обязан быть ВИДЕН с обеих сторон и вычисляться одинаково.

🔴 HIGH из адверсариального ревью (2026-08-19). Путь к файлу токена разрешается ДВУМЯ
независимыми каналами конфигурации:
  * мост (IPC-процесс) — `data_dir=self.store.data_dir`, то есть из CLI `--data-dir`;
  * REST-процесс — `read_bridge_token(settings.DATA_DIR)`, то есть из env
    `KRAB_EAR_DATA_DIR` с дефолтом `~/.krab_ear_data`.

При расхождении каналов обе половины «самолечения» (P0d) перечитывают КАЖДАЯ СВОЙ файл,
mtime ни одного не двигается, и 401 становится вечным. Это регресс-риск уже случавшегося
инцидента 2026-07-12 (rest.plist без KRAB_EAR_DATA_DIR — мост молчал, пока не нашли).

Полностью слить каналы нельзя: это разные процессы с разным способом запуска. Поэтому
лечение — снять слепоту: (1) ОДНА функция вычисления пути, чтобы формула не разъезжалась;
(2) полный путь виден в логе при первом обращении и в диагностике, чтобы расхождение
читалось за секунды, а не за часы.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import event_bridge as eb  # noqa: E402


class TokenPathResolutionTest(unittest.TestCase):
    def test_single_resolver_exists(self):
        """Формула пути обязана жить в ОДНОМ месте, иначе стороны разъедутся."""
        self.assertTrue(
            hasattr(eb, "resolve_token_path"),
            "нет единой функции разрешения пути — каждая сторона считает путь сама",
        )

    def test_resolver_is_pure_and_predictable(self):
        with TemporaryDirectory() as tmp:
            got = eb.resolve_token_path(tmp)
            # 🔴 Сравниваем с КАНОНИЧЕСКИМ путём: на macOS /var — симлинк на /private/var,
            # и именно канонизация делает пути двух процессов сверимыми глазами.
            self.assertEqual(got, (Path(tmp) / eb.EVENT_BRIDGE_TOKEN_FILENAME).resolve())
            self.assertEqual(got, eb.resolve_token_path(Path(tmp)), "str и Path дают разное")
            self.assertEqual(got, eb.resolve_token_path(got.parent), "формула не идемпотентна")

    def test_reader_uses_the_resolver(self):
        """read_bridge_token обязан ходить через ту же формулу, а не строить путь сам."""
        with TemporaryDirectory() as tmp:
            eb.resolve_token_path(tmp).write_text("a" * 64, encoding="utf-8")
            self.assertEqual(eb.read_bridge_token(tmp), "a" * 64)


class TokenPathVisibilityTest(unittest.TestCase):
    def test_diagnostics_exposes_token_path(self):
        """🔴 Без пути в диагностике расхождение каналов невидимо: обе стороны
        рапортуют «файл прочитан», просто файлы разные."""
        with TemporaryDirectory() as tmp:
            bridge = eb.EventBridge(
                settings={"event_bridge_enabled": False},
                data_dir=Path(tmp),
            )
            diag = bridge.get_diagnostics()
        self.assertIn("token_path", diag, "диагностика молчит о том, какой файл читается")
        self.assertIn(eb.EVENT_BRIDGE_TOKEN_FILENAME, str(diag["token_path"]))
        self.assertTrue(
            Path(str(diag["token_path"])).is_absolute(),
            "путь обязан быть абсолютным — относительный не поможет сверить стороны",
        )

    def test_diagnostics_reports_whether_token_exists(self):
        """«Путь такой» и «файл там есть» — разные вопросы; нужны оба."""
        with TemporaryDirectory() as tmp:
            bridge = eb.EventBridge(
                settings={"event_bridge_enabled": False},
                data_dir=Path(tmp),
            )
            self.assertIn("token_present", bridge.get_diagnostics())
            self.assertFalse(bridge.get_diagnostics()["token_present"])
            eb.resolve_token_path(tmp).write_text("b" * 64, encoding="utf-8")
            self.assertTrue(bridge.get_diagnostics()["token_present"])

    def test_reader_logs_full_path_on_first_read(self, ):
        """REST-сторона обязана однократно сказать в лог, ОТКУДА читает токен."""
        with TemporaryDirectory() as tmp, self.assertLogs(
            "KrabEar.Backend.EventBridge", level="INFO",
        ) as captured:
            eb.read_bridge_token(tmp, log_source=True)
        joined = "\n".join(captured.output)
        self.assertIn(eb.EVENT_BRIDGE_TOKEN_FILENAME, joined)
        self.assertIn(str(Path(tmp).resolve()), joined, "в логе нет полного пути")


if __name__ == "__main__":
    unittest.main()
