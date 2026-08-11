"""Тесты shutdown_forensics — dirty-marker + сбор форензики (R1 Task 6).

``subprocess.run`` везде застаблен через ``unittest.mock.patch`` — реальный
``log show``/``launchctl print`` не должен вызываться в тестах (медленно,
недетерминированно, macOS-only). Новый файл — существующие shutdown-тесты
(``test_shutdown_handler.py``, ``test_shutdown_handler_deep.py``,
``test_shutdown_handler_wired_in_main.py``, ``test_shutdown_info_r1_fields.py``)
НЕ трогать (Global Constraints плана R1).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shutdown_forensics import (  # noqa: E402
    _MARKER,
    _SHUTDOWN_INFO_FILE as FORENSICS_SHUTDOWN_INFO_FILE,
    check_and_collect,
    write_alive_marker,
)
from backend.shutdown_handler import (  # noqa: E402
    GracefulShutdownHandler,
    _SHUTDOWN_INFO_FILE,
)


class _FakeCompletedProcess:
    """Duck-type ``subprocess.CompletedProcess`` — только поля, которые читает код."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeIPCServer:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True

    def request_stop_from_signal(self):
        pass


class FakeService:
    """Минимальный сервис, удовлетворяющий IPC-контракту GracefulShutdownHandler."""

    def __init__(self):
        self._ipc_server = FakeIPCServer()


# ===========================================================================
# check_and_collect: базовые статусы (first_run / clean)
# ===========================================================================


class CheckAndCollectStatusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_run_no_marker_no_info(self):
        status = check_and_collect(data_dir=self.data_dir)
        self.assertEqual(status, "first_run")

    def test_clean_shutdown_no_collection(self):
        # Маркера нет, но shutdown_info.json от прошлой graceful-жизни есть.
        (self.data_dir / FORENSICS_SHUTDOWN_INFO_FILE).write_text(
            json.dumps({"clean": True}), encoding="utf-8",
        )
        status = check_and_collect(data_dir=self.data_dir)
        self.assertEqual(status, "clean")
        # Никакого каталога форензики не должно появиться на чистом пути.
        self.assertFalse((self.data_dir / "forensics").exists())


# ===========================================================================
# check_and_collect: UNCLEAN-ветка (маркер есть — сбор форензики)
# ===========================================================================


class CheckAndCollectUncleanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        write_alive_marker(self.data_dir)
        self.marker_path = self.data_dir / _MARKER
        self.assertTrue(self.marker_path.exists())

    def tearDown(self):
        self._tmp.cleanup()

    def test_unclean_collects_and_removes_marker(self):
        own_log = self.data_dir / "own.err.log"
        own_log.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")

        with patch(
            "backend.shutdown_forensics.subprocess.run",
            return_value=_FakeCompletedProcess(stdout="fake unified log output"),
        ) as mock_run:
            status = check_and_collect(
                data_dir=self.data_dir, log_dirs=[own_log], timeout_sec=5.0,
            )

        self.assertEqual(status, "unclean_collected")
        self.assertFalse(self.marker_path.exists(), "маркер обязан быть удалён после сбора")

        forensics_root = self.data_dir / "forensics"
        subdirs = list(forensics_root.iterdir())
        self.assertEqual(len(subdirs), 1)
        out_dir = subdirs[0]

        self.assertTrue((out_dir / "log_show.txt").exists())
        self.assertTrue((out_dir / "launchctl_print.txt").exists())
        self.assertTrue((out_dir / "stale_marker.json").exists())

        own_tail = (out_dir / "own_logs_tail.txt").read_text(encoding="utf-8")
        self.assertIn("line 49", own_tail)
        self.assertIn(str(own_log), own_tail)

        log_show_content = (out_dir / "log_show.txt").read_text(encoding="utf-8")
        self.assertIn("fake unified log output", log_show_content)

        # log show вызывается первым (порядок сбора из спеки §4.3).
        first_call_args = mock_run.call_args_list[0].args[0]
        self.assertEqual(first_call_args[0], "log")

    def test_unclean_missing_own_log_file_skipped_not_raised(self):
        missing_log = self.data_dir / "does_not_exist.log"
        with patch(
            "backend.shutdown_forensics.subprocess.run",
            return_value=_FakeCompletedProcess(),
        ):
            status = check_and_collect(
                data_dir=self.data_dir, log_dirs=[missing_log], timeout_sec=5.0,
            )
        self.assertEqual(status, "unclean_collected")

    def test_unclean_subprocess_missing_command_still_collected(self):
        """Linux/CI-гард (спека §4.3 п.3): отсутствующая команда log/launchctl —
        файл с текстом ошибки вместо содержимого, статус всё равно
        unclean_collected (собрано, что было)."""
        with patch(
            "backend.shutdown_forensics.subprocess.run",
            side_effect=FileNotFoundError("log: command not found"),
        ):
            status = check_and_collect(data_dir=self.data_dir, timeout_sec=5.0)

        self.assertEqual(status, "unclean_collected")
        out_dir = next((self.data_dir / "forensics").iterdir())
        log_show_content = (out_dir / "log_show.txt").read_text(encoding="utf-8")
        self.assertIn("command not found", log_show_content)

    def test_unclean_copies_prev_shutdown_info_when_present(self):
        (self.data_dir / FORENSICS_SHUTDOWN_INFO_FILE).write_text(
            json.dumps({"clean": False, "signal": "SIGKILL"}), encoding="utf-8",
        )
        with patch(
            "backend.shutdown_forensics.subprocess.run",
            return_value=_FakeCompletedProcess(),
        ):
            check_and_collect(data_dir=self.data_dir, timeout_sec=5.0)

        out_dir = next((self.data_dir / "forensics").iterdir())
        prev_info = json.loads((out_dir / "prev_shutdown_info.json").read_text(encoding="utf-8"))
        self.assertEqual(prev_info["signal"], "SIGKILL")


# ===========================================================================
# Retention: максимум 5 новейших каталогов форензики
# ===========================================================================


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        write_alive_marker(self.data_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_retention_keeps_5(self):
        forensics_root = self.data_dir / "forensics"
        forensics_root.mkdir(parents=True, exist_ok=True)
        # Имена ЗАВЕДОМО младше реальных ISO-timestamp имён (генерируются от
        # текущего UTC-года "20xx...") — сортировка строкой гарантированно
        # ставит их раньше нового каталога, который создаст check_and_collect.
        import os as _os
        import time as _time
        from backend.shutdown_forensics import _COLLECT_MIN_GAP_SEC

        old_dirs = []
        # mtime фикстур состариваем за окно crash-loop rate-limit'а
        # (мини-волна 2026-08-11): намерение ЭТОГО теста — retention, а
        # свежесозданные mkdir-каталоги иначе выглядят «только что собранной
        # форензикой» и check_and_collect легитимно пропустил бы сбор.
        stale = _time.time() - _COLLECT_MIN_GAP_SEC - 60
        for i in range(6):
            d = forensics_root / f"00000000_00000{i}_000000"
            d.mkdir()
            _os.utime(d, (stale, stale))
            old_dirs.append(d)

        with patch(
            "backend.shutdown_forensics.subprocess.run",
            return_value=_FakeCompletedProcess(),
        ):
            status = check_and_collect(data_dir=self.data_dir, timeout_sec=5.0)

        self.assertEqual(status, "unclean_collected")
        remaining = sorted(p.name for p in forensics_root.iterdir())
        self.assertEqual(len(remaining), 5, f"ожидалось 5 каталогов, получено: {remaining}")
        # Два самых старых (000000, 000001) обязаны быть удалены.
        self.assertNotIn(old_dirs[0].name, remaining)
        self.assertNotIn(old_dirs[1].name, remaining)
        # Оставшиеся 4 старых + новый реальный каталог — все присутствуют.
        for d in old_dirs[2:]:
            self.assertIn(d.name, remaining)


# ===========================================================================
# Катастрофический сбой сбора — никогда не бросает
# ===========================================================================


class CollectFailureNeverRaisesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        write_alive_marker(self.data_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_collect_failure_never_raises(self):
        """Отдельные subprocess-сбои (лог/launchctl отсутствуют) остаются
        best-effort и НЕ роняют весь сбор (см. test_unclean_subprocess_
        missing_command_still_collected выше) — это осознанно устойчивое
        поведение. Здесь проверяется КАТАСТРОФИЧЕСКИЙ путь: сама директория
        форензики не создаётся (например ENOSPC) — это выходит за пределы
        отдельных best-effort шагов и обязано завершиться статусом
        unclean_collect_failed, а НЕ исключением наружу (hard-инвариант
        задачи: сбор форензики никогда не роняет старт backend)."""
        with patch.object(Path, "mkdir", side_effect=OSError("ENOSPC: диск полон")):
            status = check_and_collect(data_dir=self.data_dir, timeout_sec=5.0)

        self.assertEqual(status, "unclean_collect_failed")

    def test_check_and_collect_top_level_exception_never_raises(self):
        """Любое неожиданное исключение внутри check_and_collect (не только
        в сборе форензики) ловится внешним try/except."""
        with patch(
            "backend.shutdown_forensics.Path",
            side_effect=RuntimeError("неожиданная поломка"),
        ):
            status = check_and_collect(data_dir=self.data_dir, timeout_sec=5.0)
        self.assertEqual(status, "unclean_collect_failed")


# ===========================================================================
# write_alive_marker: fail-open при ошибке диска
# ===========================================================================


class WriteAliveMarkerTest(unittest.TestCase):
    def test_write_alive_marker_creates_file_with_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            write_alive_marker(data_dir)
            marker = json.loads((data_dir / _MARKER).read_text(encoding="utf-8"))
            self.assertIn("pid", marker)
            self.assertIn("started_at_iso", marker)

    def test_write_alive_marker_io_error_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.object(Path, "write_text", side_effect=OSError("disk full")):
                write_alive_marker(data_dir)  # не должно бросить
            self.assertFalse((data_dir / _MARKER).exists())


# ===========================================================================
# Интеграция с GracefulShutdownHandler._persist: маркер снимается только
# при успешной записи shutdown_info.json (hard-инвариант задачи).
# ===========================================================================


class PersistMarkerIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_persist_removes_marker_on_success(self):
        write_alive_marker(self.data_dir)
        marker_path = self.data_dir / _MARKER
        self.assertTrue(marker_path.exists())

        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        handler.bind(FakeService())
        handler.shutdown()

        self.assertFalse(marker_path.exists(), "graceful shutdown обязан снять маркер")
        self.assertTrue((self.data_dir / _SHUTDOWN_INFO_FILE).exists())

    def test_persist_keeps_marker_when_write_fails(self):
        write_alive_marker(self.data_dir)
        marker_path = self.data_dir / _MARKER
        self.assertTrue(marker_path.exists())

        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        handler.bind(FakeService())

        with patch.object(Path, "replace", side_effect=OSError("disk full during rename")):
            handler.shutdown()

        self.assertTrue(
            marker_path.exists(),
            "провалившаяся запись shutdown_info.json НЕ должна снимать маркер — "
            "иначе следующий старт ошибочно сочтёт эту смерть graceful",
        )

    def test_persist_without_prior_marker_does_not_raise(self):
        """Обычный путь без R1 spill-фичи (маркер никогда не создавался) —
        unlink(missing_ok=True) молча не находит файл, никаких исключений."""
        handler = GracefulShutdownHandler(data_dir=self.data_dir)
        handler.bind(FakeService())
        handler.shutdown()  # не должно бросить
        self.assertTrue((self.data_dir / _SHUTDOWN_INFO_FILE).exists())


class CollectRateLimitTest(unittest.TestCase):
    """Rate-limit самого СБОРА форензики (мини-волна 2026-08-11).

    Контекст: launchd (KeepAlive=true, ThrottleInterval=5) в crash-loop'е
    поднимает backend до ~720 раз/час, и КАЖДЫЙ подъём прогонял полный
    check_and_collect — два subprocess-вызова с таймаутами до 30с каждый +
    новый каталог forensics/<ts>/. Существующий дисковый лимит
    (UNCLEAN_RESTART_REPORT_MIN_GAP_SEC, service.py) ограничивает только
    ОТПРАВКУ события в Sentry — стоимость самого сбора не трогал.

    Семантика: UNCLEAN-вердикт с недавним (моложе _COLLECT_MIN_GAP_SEC)
    уже собранным каталогом форензики → дорогие шаги пропускаются, вердикт
    "unclean_rate_limited" (сам ФАКТ unclean-смерти НЕ теряется — отправка
    события живёт своим лимитом). Источник правды о последнем сборе —
    mtime новейшего подкаталога forensics/ (без нового state-файла).
    Направление отказа: не смогли прочитать mtime → СОБИРАЕМ (fail-open в
    сторону форензики — недостающая экономия лучше недостающей форензики).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        write_alive_marker(self.data_dir)
        self.marker_path = self.data_dir / _MARKER

    def tearDown(self):
        self._tmp.cleanup()

    def _collect_once(self):
        with patch(
            "backend.shutdown_forensics.subprocess.run",
            return_value=_FakeCompletedProcess(stdout="pass one"),
        ) as mock_run:
            status = check_and_collect(
                data_dir=self.data_dir, log_dirs=[], timeout_sec=5.0,
            )
        return status, mock_run

    def test_second_unclean_within_gap_skips_collection(self):
        status1, run1 = self._collect_once()
        self.assertEqual(status1, "unclean_collected")
        self.assertGreater(len(run1.call_args_list), 0)

        # Вторая UNCLEAN-смерть сразу же (crash-loop): маркер снова на месте.
        write_alive_marker(self.data_dir)
        status2, run2 = self._collect_once()

        self.assertEqual(
            status2, "unclean_rate_limited",
            "повторный сбор в пределах _COLLECT_MIN_GAP_SEC обязан быть "
            "пропущен — каждый подъём crash-loop'а гонял 2 subprocess'а",
        )
        self.assertEqual(
            run2.call_args_list, [],
            "rate-limited ветка НЕ должна звать subprocess вовсе",
        )
        subdirs = list((self.data_dir / "forensics").iterdir())
        self.assertEqual(
            len(subdirs), 1,
            "rate-limited ветка НЕ должна плодить новые каталоги форензики",
        )
        self.assertFalse(
            self.marker_path.exists(),
            "маркер обязан сниматься и в rate-limited ветке — иначе он "
            "накапливает ложный UNCLEAN-сигнал поверх текущей жизни",
        )

    def test_stale_forensics_dir_older_than_gap_collects_again(self):
        import os as _os
        import time as _time

        status1, _ = self._collect_once()
        self.assertEqual(status1, "unclean_collected")

        # Состариваем единственный собранный каталог за пределы окна.
        from backend.shutdown_forensics import _COLLECT_MIN_GAP_SEC
        old = _time.time() - _COLLECT_MIN_GAP_SEC - 60
        forensics_root = self.data_dir / "forensics"
        newest = sorted(forensics_root.iterdir())[-1]
        _os.utime(newest, (old, old))

        write_alive_marker(self.data_dir)
        status2, run2 = self._collect_once()
        self.assertEqual(status2, "unclean_collected")
        self.assertGreater(
            len(run2.call_args_list), 0,
            "каталог старше окна — полный сбор обязан выполниться снова",
        )

    def test_mtime_read_failure_fails_open_to_collection(self):
        status1, _ = self._collect_once()
        self.assertEqual(status1, "unclean_collected")

        write_alive_marker(self.data_dir)
        with patch(
            "backend.shutdown_forensics._latest_forensics_mtime",
            side_effect=RuntimeError("stat broken"),
        ):
            status2, run2 = self._collect_once()
        self.assertEqual(
            status2, "unclean_collected",
            "ошибка чтения mtime → fail-open в сторону СБОРА, не экономии",
        )

    def test_no_prior_forensics_dir_collects_normally(self):
        # Пустой data_dir (forensics/ ещё не существует) — первый UNCLEAN
        # всегда собирает; rate-limit включается только со второго.
        status, run = self._collect_once()
        self.assertEqual(status, "unclean_collected")
        self.assertGreater(len(run.call_args_list), 0)


class RateLimitedVerdictStillReportedSourceContractTest(unittest.TestCase):
    """service.py::_report_unclean_restart обязан трактовать
    "unclean_rate_limited" как unclean-вердикт для ОТПРАВКИ события — у
    отправки СВОЙ дисковый лимит (UNCLEAN_RESTART_REPORT_MIN_GAP_SEC);
    экономия на сборе не должна глушить сигнал о смертях. AST здесь
    избыточен: пиним вхождение вердикта в тот же кортеж-allowlist, где
    живут два существующих (проверяем оба, чтобы регекс не «прошёл» на
    совсем другом кортеже)."""

    def test_verdict_in_report_allowlist(self):
        service_src = (
            PROJECT_ROOT / "backend" / "service.py"
        ).read_text(encoding="utf-8")
        import re
        m = re.search(
            r"if verdict not in \(([^)]*)\)", service_src,
        )
        self.assertIsNotNone(m, "не найден вердикт-allowlist в _report_unclean_restart")
        allowlist = m.group(1)
        for expected in (
            "unclean_collected", "unclean_collect_failed", "unclean_rate_limited",
        ):
            self.assertIn(
                expected, allowlist,
                f"вердикт {expected!r} обязан быть в allowlist отправки — "
                "иначе смерти в crash-loop'е перестанут репортиться вовсе",
            )


if __name__ == "__main__":
    unittest.main()
