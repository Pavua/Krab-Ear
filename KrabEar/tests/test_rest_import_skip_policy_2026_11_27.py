"""Политика skip для REST-тестов: тихо пропускать можно только объявленную optional-зависимость.

ChatGPT-ревью #2298 (HOLD): в четырёх файлах `test_rest_*` стояло
`except ModuleNotFoundError: pass`. Оно глотало и сбой импорта кода приложения
(backend/core), и отсутствие обязательного пакета из requirements.txt — все тесты файла
становились «вечным skip» при зелёном CI. CI ставит полный requirements.txt, поэтому
пропавший flask там — поломка окружения, а не повод для skip.

Проверяется (1) сама политика `_rest_import_policy.classify_import_failure`,
(2) поведение: с «пропавшим» flask тест TestRestStubImportHealthy в каждом из четырёх
файлов обязан ПАДАТЬ, (3) AST: ни один обработчик ModuleNotFoundError в этих файлах не
остаётся пустым `pass`.
"""

import ast
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from _rest_import_policy import classify_import_failure

_TESTS_DIR = Path(__file__).resolve().parent
_KRABEAR_ROOT = _TESTS_DIR.parent
_FILES = (
    "test_rest_cors_exfil_w21.py",
    "test_rest_hardening.py",
    "test_rest_server_w1212.py",
    "test_rest_wave31_hardening.py",
)


def _mnf(name):
    return ModuleNotFoundError(f"No module named {name!r}", name=name)


class ClassifyImportFailureTest(unittest.TestCase):
    def test_declared_optional_dependency_is_skipped(self):
        for name in ("gigaam", "openwakeword", "sentence_transformers", "mlx", "mlx.core"):
            with self.subTest(name=name):
                self.assertEqual(classify_import_failure(_mnf(name))[0], "skip")

    def test_required_package_from_requirements_fails(self):
        for name in ("flask", "flask_cors", "flask_limiter", "numpy", "requests", "marshmallow"):
            with self.subTest(name=name):
                self.assertEqual(classify_import_failure(_mnf(name))[0], "fail")

    def test_app_module_failure_fails(self):
        for name in ("backend.rest_server", "backend.service", "core.engine", "contracts.x"):
            with self.subTest(name=name):
                self.assertEqual(classify_import_failure(_mnf(name))[0], "fail")

    def test_undeclared_module_fails(self):
        # Не объявлен ни как обязательный, ни как optional — это не повод молчать.
        self.assertEqual(classify_import_failure(_mnf("totally_unknown_pkg"))[0], "fail")

    def test_missing_name_fails(self):
        self.assertEqual(classify_import_failure(ModuleNotFoundError("boom"))[0], "fail")

    def test_non_module_not_found_error_fails(self):
        # Дрейф stub («cannot import name») — обычный ImportError, не skip.
        self.assertEqual(classify_import_failure(ImportError("cannot import name X"))[0], "fail")
        self.assertEqual(classify_import_failure(RuntimeError("x"))[0], "fail")


_HIDE_FLASK_PLUGIN = textwrap.dedent(
    '''
    # Эмуляция «обязательный пакет пропал»: import flask -> ModuleNotFoundError.
    import sys
    sys.modules["flask"] = None
    '''
)


class MissingRequiredPackageIsLoudTest(unittest.TestCase):
    def test_hidden_flask_fails_in_every_file(self):
        with tempfile.TemporaryDirectory(prefix="krab_ear_rest_skip_policy_") as td:
            tmp = Path(td)
            (tmp / "hide_flask_plugin.py").write_text(_HIDE_FLASK_PLUGIN, encoding="utf-8")
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([str(_KRABEAR_ROOT), str(_TESTS_DIR), str(tmp)])
            for fname in _FILES:
                with self.subTest(file=fname):
                    proc = subprocess.run(
                        [sys.executable, "-m", "pytest", str(_TESTS_DIR / fname), "-q",
                         "-p", "no:cacheprovider", "-p", "hide_flask_plugin",
                         "--rootdir", str(_KRABEAR_ROOT),
                         "-k", "TestRestStubImportHealthy"],
                        env=env, cwd=str(_KRABEAR_ROOT), capture_output=True, text=True,
                        timeout=240,
                    )
                    self.assertNotEqual(
                        proc.returncode, 0,
                        f"{fname}: пропавший обязательный flask дал rc=0 (тихий skip)\n"
                        f"{proc.stdout[-800:]}",
                    )


class NoSilentModuleNotFoundHandlerTest(unittest.TestCase):
    def test_no_bare_pass_handler(self):
        for fname in _FILES:
            tree = ast.parse((_TESTS_DIR / fname).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                names = ({n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)}
                         if node.type is not None else set())
                if "ModuleNotFoundError" not in names:
                    continue
                only_pass = all(isinstance(s, ast.Pass) for s in node.body)
                self.assertFalse(only_pass, f"{fname}:{node.lineno}: пустой `except ModuleNotFoundError: pass`")


if __name__ == "__main__":
    unittest.main()
