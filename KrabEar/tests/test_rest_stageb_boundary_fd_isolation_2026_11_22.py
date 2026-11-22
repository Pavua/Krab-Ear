"""G2-KET10-T10-03: файловая граница Stage B не должна обходиться через dir_fd/дескрипторы.

Audit hook проверял только путь из аргументов. Относительный путь + dir_fd внешнего
каталога разрешался хуком относительно cwd (scratch) и проходил, хотя настоящая
операция шла во внешнем каталоге. Тест поднимает настоящий `Boundary.audit` в
дочернем процессе (audit hook необратим), выполняет операции над синтетическими
файлами во ВНЕШНЕМ временном каталоге и проверяет, что каталог не изменился,
а нарушения зафиксированы. Исходники проекта не затрагиваются.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

_CHILD = textwrap.dedent(
    """
    import importlib.util, json, os, sys
    from pathlib import Path

    scratch, external, module_path = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.argv = [module_path, scratch]
    spec = importlib.util.spec_from_file_location("rest_stageb_boundary", module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    boundary = mod.Boundary(Path(scratch).resolve())

    os.chdir(scratch)
    # Дескриптор внешнего каталога получен ДО установки хука (унаследован/открыт ранее).
    ext_fd = os.open(external, os.O_RDONLY)
    sys.addaudithook(boundary.audit)
    results = {}

    def attempt(name, fn):
        try:
            fn()
            results[name] = "allowed"
        except PermissionError:
            results[name] = "denied"
        except OSError as exc:
            results[name] = "oserror:" + type(exc).__name__

    def write_via_dir_fd():
        fd = os.open("victim.txt", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=ext_fd)
        os.close(fd)

    attempt("write", write_via_dir_fd)
    attempt("rename", lambda: os.rename("victim.txt", "renamed.txt", src_dir_fd=ext_fd, dst_dir_fd=ext_fd))
    attempt("utime", lambda: os.utime("renamed.txt", (1, 1), dir_fd=ext_fd))
    attempt("mkdir", lambda: os.mkdir("sub", dir_fd=ext_fd))
    attempt("chmod_fd", lambda: os.chmod(ext_fd, 0o700))
    attempt("utime_fd", lambda: os.utime(ext_fd, (1, 1)))
    print(json.dumps({"results": results, "violations": boundary.violations}))
    """
)


class StageBBoundaryFdIsolationTest(unittest.TestCase):
    def test_dir_fd_and_descriptor_mutations_are_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "scratch"
            external = Path(tmp) / "external"
            scratch.mkdir()
            external.mkdir()
            before_mode = external.stat().st_mode
            before_mtime = external.stat().st_mtime_ns
            proc = subprocess.run(
                [sys.executable, "-I", "-c", _CHILD, str(scratch), str(external),
                 str(TESTS_DIR / "rest_stageb_boundary.py")],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
            # Внешний каталог не изменился ни содержимым, ни правами, ни временем.
            self.assertEqual(sorted(p.name for p in external.iterdir()), [], payload)
            self.assertEqual(external.stat().st_mode, before_mode)
            self.assertEqual(external.stat().st_mtime_ns, before_mtime)
            for name, outcome in payload["results"].items():
                self.assertEqual(outcome, "denied", f"{name}: {payload}")
            self.assertTrue(payload["violations"], "нарушения не зафиксированы")


if __name__ == "__main__":
    unittest.main()
