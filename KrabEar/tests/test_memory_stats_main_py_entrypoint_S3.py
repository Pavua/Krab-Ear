"""S3/Задача 1 (доработка по находке координатора) — memory-инструменты обязаны

опознавать backend, запущенный через ``KrabEar/main.py``, не только через
устаревший ``KrabEar/backend/service.py``.

Sibling-асимметрия: РОВНО тот же кортеж паттернов cmdline-матчинга живёт в
ДВУХ местах — ``backend/service.py::_handle_get_memory_stats`` (IPC-метод
``get_memory_stats``, использует GUI-диагностика) и
``scripts/memory_baseline.py::get_processes`` (CLI-инструмент замера RAM для
двухнедельной канарейки волны S3 — до/после включения in-process REST). После
смены точки входа плиста на ``main.py`` (см. ``test_backend_plist_data_dir_parity_S3.py``
и ``BackendSupervisor.swift::backendScriptPath``) оба инструмента, не
опознавая backend ни по одной из трёх веток, дали бы тихо неверные числа —
именно те числа, на которые опирается решение по канарейке.

S3/Задача 10 (доработка по находке координатора, второй раз за волну ровно
та же пара расходится): ``scripts/memory_baseline.py`` уже опознаёт легаси
``KrabEar/backend/rest_server.py`` (см. ``test_memory_baseline.py``), но
``backend/service.py::_handle_get_memory_stats`` — нет. Владелец видит
именно GUI-диагностику (``get_memory_stats``) во время двухнедельной
канарейки, а не CLI-скрипт — без этого фикса замер «до» в панели молчит про
второй процесс. Классы ниже добавляют матч + новый ``kind="rest"``
(тот же префикс, что ``rest_rss_mb`` в ``memory_baseline.py`` — две стороны
обязаны называть эту категорию одинаково).
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_BASELINE_SCRIPT = REPO_ROOT / "scripts" / "memory_baseline.py"

from backend.state_store import StateStore  # noqa: E402
from backend.service import BackendService  # noqa: E402


class _FakeRecorder:
    is_recording = False
    sample_rate = 16000


class _FakeTranscriber:
    pass


class _FakeTranslator:
    pass


class GetMemoryStatsMainPyEntrypointTest(unittest.TestCase):
    """``get_memory_stats`` (backend/service.py:3197) обязан матчить main.py."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )
        self.addCleanup(self.service.close)

    def test_process_started_via_main_py_classified_as_backend(self) -> None:
        """RED до фикса: main.py не входит в кортеж паттернов — kind не назначается."""
        fake_proc = MagicMock()
        fake_proc.pid = 4242
        fake_proc.cmdline.return_value = [
            "/path/.venv_krab_ear/bin/python3",
            "/path/KrabEar/main.py",
            "--data-dir",
            "/tmp",
        ]
        fake_proc.name.return_value = "python3"
        fake_proc.memory_info.return_value = MagicMock(rss=100 * 1024 * 1024, vms=200 * 1024 * 1024)

        fake_psutil = MagicMock()
        fake_psutil.process_iter.return_value = [fake_proc]
        fake_psutil.NoSuchProcess = Exception
        fake_psutil.AccessDenied = Exception
        fake_psutil.ZombieProcess = Exception

        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            resp = self.service.handle_request(
                {"id": "t1", "method": "get_memory_stats", "params": {}}
            )

        self.assertTrue(resp["ok"], msg=f"IPC dispatch error: {resp}")
        result = resp["result"]
        self.assertTrue(result["ok"])
        procs = result["processes"]
        self.assertEqual(
            len(procs), 1,
            msg=f"процесс, запущенный через KrabEar/main.py, не опознан: {procs!r}",
        )
        self.assertEqual(procs[0]["kind"], "backend")
        self.assertEqual(procs[0]["pid"], 4242)


class MemoryBaselineGetProcessesMainPyEntrypointTest(unittest.TestCase):
    """``scripts/memory_baseline.py::get_processes`` обязан матчить main.py.

    Питается той же канарейкой волны S3 (замер RAM до/после включения
    in-process REST) — если инструмент не находит backend, замер тихо врёт.
    """

    def setUp(self) -> None:
        if not MEMORY_BASELINE_SCRIPT.exists():
            self.skipTest("memory_baseline.py not found")

    def _load_module_with_fake_psutil(self, fake_psutil: MagicMock):
        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            spec = importlib.util.spec_from_file_location(
                "memory_baseline_test_S3", MEMORY_BASELINE_SCRIPT
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        return mod

    def test_process_started_via_main_py_found_by_get_processes(self) -> None:
        """RED до фикса: main.py не входит в кортеж паттернов — процесс пропущен."""
        fake_proc = MagicMock()
        fake_proc.info = {
            "pid": 4242,
            "name": "python3",
            "cmdline": [
                "/path/.venv_krab_ear/bin/python3",
                "/path/KrabEar/main.py",
                "--data-dir",
                "/tmp",
            ],
            "memory_info": MagicMock(rss=100 * 1024 * 1024, vms=200 * 1024 * 1024),
        }

        fake_psutil = MagicMock()
        fake_psutil.process_iter.return_value = [fake_proc]
        fake_psutil.NoSuchProcess = Exception
        fake_psutil.AccessDenied = Exception

        mod = self._load_module_with_fake_psutil(fake_psutil)
        matches = mod.get_processes()

        self.assertEqual(
            len(matches), 1,
            msg=f"процесс, запущенный через KrabEar/main.py, не найден: {matches!r}",
        )
        self.assertEqual(matches[0]["pid"], 4242)


class GetMemoryStatsRestServerEntrypointTest(unittest.TestCase):
    """``get_memory_stats`` обязан матчить легаси ``rest_server.py`` и
    классифицировать его как ``kind="rest"`` (не ``"backend"`` — это другой
    процесс с другим жизненным циклом, критично различимый в GUI-диагностике
    во время двухнедельной S3-канарейки «до/после» слияния)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )
        self.addCleanup(self.service.close)

    def test_process_started_via_rest_server_py_classified_as_rest(self) -> None:
        """RED до фикса: rest_server.py не входит в кортеж паттернов — процесс пропущен."""
        fake_proc = MagicMock()
        fake_proc.pid = 9911
        fake_proc.cmdline.return_value = [
            "/path/.venv_krab_ear/bin/python3",
            "/path/KrabEar/backend/rest_server.py",
        ]
        fake_proc.name.return_value = "python3"
        fake_proc.memory_info.return_value = MagicMock(rss=50 * 1024 * 1024, vms=100 * 1024 * 1024)

        fake_psutil = MagicMock()
        fake_psutil.process_iter.return_value = [fake_proc]
        fake_psutil.NoSuchProcess = Exception
        fake_psutil.AccessDenied = Exception
        fake_psutil.ZombieProcess = Exception

        with patch.dict("sys.modules", {"psutil": fake_psutil}):
            resp = self.service.handle_request(
                {"id": "t1", "method": "get_memory_stats", "params": {}}
            )

        self.assertTrue(resp["ok"], msg=f"IPC dispatch error: {resp}")
        result = resp["result"]
        self.assertTrue(result["ok"])
        procs = result["processes"]
        self.assertEqual(
            len(procs), 1,
            msg=f"процесс, запущенный через KrabEar/backend/rest_server.py, не опознан: {procs!r}",
        )
        self.assertEqual(procs[0]["kind"], "rest")
        self.assertEqual(procs[0]["pid"], 9911)


if __name__ == "__main__":
    unittest.main()
