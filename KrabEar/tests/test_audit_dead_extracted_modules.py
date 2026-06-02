"""Unit tests for scripts/audit_dead_extracted_modules.py (W1768 guard).

Покрывает корневую причину W746/W797 (decorative extraction):
  - модуль, не импортируемый нигде в production → флагается DEAD;
  - кросс-файловый дубликат, где монолит использует СВОЮ инлайн-копию →
    извлечённая копия флагается как DEAD DUP;
  - корректно подключённый извлечённый модуль → НЕ флагается;
  - runnable entry-point модуль (``if __name__ == "__main__"``) → НЕ флагается
    как dead-module;
  - generic-имя ``main`` не считается извлечённым дубликатом;
  - реэкспорт через ``__init__.py``, который никто не потребляет → не спасает
    модуль от статуса DEAD;
  - allowlist (module:/dup:) подавляет находки.

Плюс smoke-прогон на РЕАЛЬНОМ репозитории: проверяет, что страж находит
известные W797-находки (ipc_dispatch.py / service_logging.py / IPCServer dup).
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — добавляем КОРЕНЬ репозитория и scripts/ в sys.path.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")
for _p in (PROJECT_ROOT, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import audit_dead_extracted_modules as audit  # noqa: E402


# ---------------------------------------------------------------------------
# Helper: построить синтетический мини-репозиторий на диске.
# ---------------------------------------------------------------------------

def _write(base: Path, relpath: str, content: str) -> None:
    target = base / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(content), encoding="utf-8")


def _make_repo(tmp: Path, files: dict) -> Path:
    """Создаёт repo_root/KrabEar/... из словаря {relpath_under_KrabEar: src}."""
    repo = tmp / "repo"
    pkg = repo / "KrabEar"
    pkg.mkdir(parents=True, exist_ok=True)
    for rel, src in files.items():
        _write(pkg, rel, src)
    return repo


# ---------------------------------------------------------------------------
# Тесты детектора мёртвых модулей
# ---------------------------------------------------------------------------

class DeadModuleDetectorTests(unittest.TestCase):
    def test_module_imported_nowhere_is_flagged_dead(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(Path(td), {
                "backend/__init__.py": "",
                # извлечённый модуль с публичной фабрикой — НИКТО не импортирует
                "backend/orphan_dispatch.py": '''
                    """Извлечённый, но мёртвый модуль."""
                    def build_table(svc):
                        return {"ping": svc.ping}
                ''',
                # монолит строит свой инлайн-словарь, не зовёт build_table
                "backend/service.py": '''
                    class BackendService:
                        def handle_request(self, p):
                            handlers = {"ping": self._ping}
                            return handlers.get(p["method"])
                        def _ping(self, p):
                            return {}
                ''',
            })
            dead_modules, dead_dups = audit.run_audit(repo)
            mods = {Path(f["module"]).name for f in dead_modules}
            self.assertIn("orphan_dispatch.py", mods)

    def test_properly_imported_module_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(Path(td), {
                "backend/__init__.py": "",
                "backend/live_helper.py": '''
                    class LiveHelper:
                        def go(self):
                            return 1
                ''',
                # монолит реально импортирует и использует извлечённый символ
                "backend/service.py": '''
                    from backend.live_helper import LiveHelper

                    class BackendService:
                        def __init__(self):
                            self._helper = LiveHelper()
                ''',
            })
            dead_modules, _ = audit.run_audit(repo)
            mods = {Path(f["module"]).name for f in dead_modules}
            self.assertNotIn("live_helper.py", mods)

    def test_entrypoint_module_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(Path(td), {
                "backend/__init__.py": "",
                # runnable entry point: публичные символы не импортируются,
                # но модуль НЕ мёртв (запускается как скрипт / WSGI).
                "backend/rest_like.py": '''
                    class ResponseSchema:
                        pass

                    def create_app():
                        return object()

                    if __name__ == "__main__":
                        create_app()
                ''',
                "backend/service.py": '''
                    class BackendService:
                        pass
                ''',
            })
            dead_modules, _ = audit.run_audit(repo)
            mods = {Path(f["module"]).name for f in dead_modules}
            self.assertNotIn("rest_like.py", mods)

    def test_unconsumed_reexport_does_not_save_module(self):
        # __init__ реэкспортирует символ, но НИКТО его не потребляет →
        # модуль всё равно мёртв.
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(Path(td), {
                "backend/__init__.py": '''
                    __all__ = ["GhostService"]

                    def __getattr__(name):
                        if name == "GhostService":
                            from .ghost import GhostService
                            return GhostService
                        raise AttributeError(name)
                ''',
                "backend/ghost.py": '''
                    class GhostService:
                        def handle(self):
                            return {}
                ''',
                # монолит НЕ использует backend.GhostService и не импортирует ghost
                "backend/service.py": '''
                    class BackendService:
                        def handle_request(self, p):
                            handlers = {"x": self._x}
                            return handlers.get(p["method"])
                        def _x(self, p):
                            return {}
                ''',
            })
            dead_modules, _ = audit.run_audit(repo)
            mods = {Path(f["module"]).name for f in dead_modules}
            self.assertIn("ghost.py", mods)

    def test_consumed_reexport_keeps_module_alive(self):
        # __init__ реэкспортирует символ, и потребитель его реально импортирует
        # `from backend import RealService` → модуль ЖИВ.
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(Path(td), {
                "backend/__init__.py": '''
                    __all__ = ["RealService"]

                    def __getattr__(name):
                        if name == "RealService":
                            from .real import RealService
                            return RealService
                        raise AttributeError(name)
                ''',
                "backend/real.py": '''
                    class RealService:
                        def handle(self):
                            return {}
                ''',
                "backend/consumer.py": '''
                    from backend import RealService

                    def use():
                        return RealService()
                ''',
            })
            dead_modules, _ = audit.run_audit(repo)
            mods = {Path(f["module"]).name for f in dead_modules}
            self.assertNotIn("real.py", mods)


# ---------------------------------------------------------------------------
# Тесты детектора мёртвых кросс-файловых дубликатов
# ---------------------------------------------------------------------------

class DeadDuplicateDetectorTests(unittest.TestCase):
    def test_dup_where_monolith_uses_own_copy_is_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(Path(td), {
                "backend/__init__.py": "",
                # извлечённая копия IPCServer
                "backend/ipc_server.py": '''
                    class IPCServer:
                        def serve_forever(self):
                            return "extracted"
                ''',
                # монолит определяет СВОЙ инлайн IPCServer и использует его
                "backend/service.py": '''
                    class IPCServer:
                        def serve_forever(self):
                            return "inline"

                    def main():
                        server = IPCServer()
                        server.serve_forever()
                ''',
            })
            _, dead_dups = audit.run_audit(repo)
            dup_syms = {f["symbol"] for f in dead_dups}
            self.assertIn("IPCServer", dup_syms)
            ipc = next(f for f in dead_dups if f["symbol"] == "IPCServer")
            self.assertTrue(ipc["extracted_module"].endswith("ipc_server.py"))
            self.assertTrue(ipc["monolith"].endswith("service.py"))

    def test_dup_where_monolith_imports_extracted_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(Path(td), {
                "backend/__init__.py": "",
                "backend/ipc_server.py": '''
                    class IPCServer:
                        def serve_forever(self):
                            return "extracted"
                ''',
                # монолит ИМПОРТИРУЕТ извлечённый символ (split завершён правильно)
                "backend/service.py": '''
                    from backend.ipc_server import IPCServer

                    def main():
                        server = IPCServer()
                        server.serve_forever()
                ''',
            })
            _, dead_dups = audit.run_audit(repo)
            dup_syms = {f["symbol"] for f in dead_dups}
            self.assertNotIn("IPCServer", dup_syms)

    def test_generic_main_is_not_treated_as_dup(self):
        with tempfile.TemporaryDirectory() as td:
            repo = _make_repo(Path(td), {
                "backend/__init__.py": "",
                "backend/some_worker.py": '''
                    def main():
                        return 0

                    if __name__ == "__main__":
                        main()
                ''',
                "backend/service.py": '''
                    def main():
                        return 1

                    if __name__ == "__main__":
                        main()
                ''',
            })
            _, dead_dups = audit.run_audit(repo)
            dup_syms = {f["symbol"] for f in dead_dups}
            self.assertNotIn("main", dup_syms)


# ---------------------------------------------------------------------------
# Тесты allowlist
# ---------------------------------------------------------------------------

class AllowlistTests(unittest.TestCase):
    def test_allowlist_parses_module_and_dup_entries(self):
        with tempfile.TemporaryDirectory() as td:
            allow = Path(td) / "allow.txt"
            allow.write_text(textwrap.dedent('''
                # comment
                module:backend/foo.py
                dup:IPCServer@backend/ipc_server.py   # inline note
            '''), encoding="utf-8")
            mods, dups = audit.load_allowlist(allow)
            self.assertEqual(mods, {"backend/foo.py"})
            self.assertEqual(dups, {"IPCServer@backend/ipc_server.py"})

    def test_missing_allowlist_returns_empty(self):
        mods, dups = audit.load_allowlist(Path("/nonexistent/allow.txt"))
        self.assertEqual(mods, set())
        self.assertEqual(dups, set())


# ---------------------------------------------------------------------------
# Smoke-прогон на реальном репозитории
# ---------------------------------------------------------------------------

class RealRepoSmokeTests(unittest.TestCase):
    """Подтверждает, что страж ловит реальные W797-decorative-extraction
    находки в текущем дереве."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(PROJECT_ROOT)
        cls.dead_modules, cls.dead_dups = audit.run_audit(cls.repo_root)
        cls.dead_mod_names = {Path(f["module"]).name for f in cls.dead_modules}
        cls.dead_dup_keys = {
            (f["symbol"], Path(f["extracted_module"]).name) for f in cls.dead_dups
        }

    def test_ipc_dispatch_flagged_dead(self):
        # build_dispatch_table не импортируется production — мёртвый модуль.
        self.assertIn("ipc_dispatch.py", self.dead_mod_names)

    def test_service_logging_flagged_dead(self):
        self.assertIn("service_logging.py", self.dead_mod_names)

    def test_ipcserver_dup_flagged(self):
        # IPCServer определён И в ipc_server.py, И инлайн в service.py;
        # production использует инлайн-копию.
        self.assertIn(("IPCServer", "ipc_server.py"), self.dead_dup_keys)

    def test_configure_logging_dup_flagged(self):
        self.assertIn(("configure_logging", "service_logging.py"), self.dead_dup_keys)

    def test_findings_are_nonempty(self):
        # Sanity: страж вообще что-то нашёл (иначе сломан).
        self.assertGreater(len(self.dead_modules) + len(self.dead_dups), 0)


if __name__ == "__main__":
    unittest.main()
