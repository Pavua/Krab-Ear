"""Регрессия: test_rest_auth::test_readiness_with_correct_key не зависит от порядка файлов.

Живой дефект (CI 27.11, #2300, дважды): тест вернул 503 вместо 200. Обработчик
/v1/readiness берёт `BackendService` из модуля `backend.rest_server`. Если соседний тест-файл
уже импортировал rest_server, подмена `backend.service.BackendService` в test_rest_auth до
обработчика не доходила, а реальный проб STT на раннере без кэша моделей даёт overall_ready=False.
Локально (кэш моделей есть) дефект невидим, поэтому проверка идёт в подпроцессе, который
эмулирует CI: реальный STT-проб «не готов» + rest_server импортирован ДО test_rest_auth.
"""

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_KRABEAR_ROOT = _TESTS_DIR.parent

_CI_LIKE_PLUGIN = textwrap.dedent(
    '''
    # Эмуляция CI без кэша моделей: реальный STT-проб = «не готов».
    def pytest_configure(config):
        import backend.service as svc
        svc.BackendService._probe_stt = staticmethod(lambda: {"ready": False})
    '''
)

_IMPORTER = textwrap.dedent(
    '''
    # Как десятки соседних файлов: настоящий rest_server импортируется раньше test_rest_auth.
    from unittest.mock import patch
    with patch("pathlib.Path.mkdir"):
        import backend.rest_server  # noqa: F401

    def test_noop():
        pass
    '''
)


class ReadinessOrderIndependenceTest(unittest.TestCase):
    def _run(self, files, tmp: Path):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(_KRABEAR_ROOT), str(tmp)])
        cmd = [
            sys.executable, "-m", "pytest", *[str(f) for f in files],
            "-q", "-p", "no:cacheprovider", "-p", "ci_like_plugin",
            "--rootdir", str(_KRABEAR_ROOT),
            "-k", "test_noop or test_readiness_with_correct_key",
        ]
        return subprocess.run(cmd, env=env, cwd=str(_KRABEAR_ROOT), capture_output=True,
                              text=True, timeout=240)

    def test_readiness_ok_when_rest_server_imported_first_and_models_absent(self):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="krab_ear_readiness_order_") as td:
            tmp = Path(td)
            (tmp / "ci_like_plugin.py").write_text(_CI_LIKE_PLUGIN, encoding="utf-8")
            importer = tmp / "test_00_importer.py"
            importer.write_text(_IMPORTER, encoding="utf-8")
            auth = _TESTS_DIR / "test_rest_auth.py"
            for order in ([auth], [importer, auth]):
                with self.subTest(order=[p.name for p in order]):
                    proc = self._run(order, tmp)
                    self.assertEqual(
                        proc.returncode, 0,
                        f"порядок {[p.name for p in order]}: rc={proc.returncode}\n"
                        f"{proc.stdout[-1500:]}\n{proc.stderr[-500:]}",
                    )


if __name__ == "__main__":
    unittest.main()
