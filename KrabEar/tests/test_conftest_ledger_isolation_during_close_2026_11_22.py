"""G2-KET10-T10-01: изоляция memory ledger обязана жить всё время закрытия BackendService.

Раньше function-фикстура `_memory_ledger_tmp_path` возвращала прежний
`_TEST_PATH_OVERRIDE` (None) раньше, чем session-финализатор закрывал долгоживущие
службы: `close()` → `conductor.stop()` → `remove_own()` шёл бы по реальному
~/.openclaw/memory_ledger.json владельца. Тест гоняет настоящий conftest в
дочернем pytest с инертными службами и читает, какой override видел `close()`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent

_PROBE = textwrap.dedent(
    """
    import json, os
    from backend import memory_ledger, service

    OUT = os.environ["PROBE_OUT"]
    _KEEP = []


    class _Inert:
        def __init__(self, tag):
            self.tag = tag
            self.done = False

        def close(self):
            if self.done:
                return
            self.done = True
            with open(OUT, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"tag": self.tag,
                                     "override": str(memory_ledger._TEST_PATH_OVERRIDE)}) + "\\n")


    def _register(tag):
        obj = _Inert(tag)
        _KEEP.append(obj)  # WeakSet не держит сильных ссылок
        service._LIVE_INSTANCES.add(obj)


    def test_a_function_level():
        _register("function")


    _register("session")  # создан при импорте: живёт до начала любого теста, как setUpClass
    """
)


class LedgerIsolationDuringCloseTest(unittest.TestCase):
    def test_close_sees_temporary_ledger_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "test_probe_ledger.py"
            probe.write_text(_PROBE, encoding="utf-8")
            out = Path(tmp) / "out.ndjson"
            env = dict(os.environ)
            env["PROBE_OUT"] = str(out)
            env["PYTHONPATH"] = os.pathsep.join([str(PROJECT_ROOT), str(TESTS_DIR)])
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider",
                 "-q", "-W", "ignore", "--rootdir", tmp, str(probe)],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(records, "ни одна инертная служба не была закрыта")
            real_default = str(Path.home() / ".openclaw")
            for rec in records:
                self.assertNotEqual(rec["override"], "None",
                                    f"close() службы {rec['tag']} шёл без подмены пути ledger")
                self.assertFalse(rec["override"].startswith(real_default), rec)


if __name__ == "__main__":
    unittest.main()
