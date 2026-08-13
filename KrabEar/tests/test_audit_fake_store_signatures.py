"""Тесты гарда `scripts/audit_fake_store_signatures.py`.

Класс бага, который закрывает гард (живой случай, PR #1916): тесты определяют
фейковый StateStore вручную, продовая сигнатура уезжает вперёд (волна
«Контенция общего лока» добавила `lock_timeout_sec`/`nowait`), и фейки молча
расходятся с ней. Тест краснеет не в момент дрейфа, а случайно — через месяцы,
когда правка вызывающего кода наконец доведёт исполнение до нового кварга.

Ключевое проектное решение гарда — критерий: фейк обязан принимать не всю
сигнатуру реального класса, а ровно те ИМЕНОВАННЫЕ аргументы, которыми его
реально зовёт продовый код. Первая версия критерия («принимать всю сигнатуру»)
дала 21 ложную находку на `save_settings`, чей параметр в фейках просто назван
иначе, а зовут его позиционно. Тесты ниже фиксируют оба края этого критерия.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
SCRIPT = REPO_ROOT / "scripts" / "audit_fake_store_signatures.py"

if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("audit_fake_store_signatures", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Без регистрации в sys.modules @dataclass не может разрешить __module__
    # загруженного таким образом модуля и падает на AttributeError.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class ExtractRealSignaturesTest(unittest.TestCase):
    """Сигнатуры реального класса читаются из исходника, без импорта проекта."""

    def setUp(self) -> None:
        self.mod = _load_module()

    def test_extracts_public_method_params_without_self(self) -> None:
        source = '''
class StateStore:
    def load_settings(self, lock_timeout_sec=None, nowait=False):
        return {}

    def save_settings(self, new_settings):
        return new_settings
'''
        sigs = self.mod.extract_class_signatures(source, "StateStore")

        self.assertEqual(sigs["load_settings"], {"lock_timeout_sec", "nowait"})
        self.assertEqual(sigs["save_settings"], {"new_settings"})

    def test_skips_private_methods(self) -> None:
        source = '''
class StateStore:
    def _lock(self, nowait=False):
        pass

    def compact(self):
        pass
'''
        sigs = self.mod.extract_class_signatures(source, "StateStore")

        self.assertIn("compact", sigs)
        self.assertNotIn("_lock", sigs)

    def test_ignores_other_classes(self) -> None:
        source = '''
class SomethingElse:
    def load_settings(self, whatever):
        pass
'''
        sigs = self.mod.extract_class_signatures(source, "StateStore")

        self.assertEqual(sigs, {})


class CollectRequiredKwargsTest(unittest.TestCase):
    """Требование к фейку выводится из РЕАЛЬНЫХ вызовов продового кода."""

    def setUp(self) -> None:
        self.mod = _load_module()
        self.known = {"load_settings", "save_settings", "count_active_items"}

    def test_collects_keyword_names_from_calls(self) -> None:
        source = '''
def f(store):
    store.load_settings(nowait=True)
    store.load_settings(lock_timeout_sec=0.5)
'''
        required = self.mod.collect_required_kwargs([("svc.py", source)], self.known)

        self.assertEqual(required["load_settings"], {"nowait", "lock_timeout_sec"})

    def test_positional_only_calls_impose_no_kwarg_requirement(self) -> None:
        """save_settings зовут позиционно → имя параметра фейка не важно.

        Это и есть край критерия: требовать здесь `new_settings` значило бы
        выдать 21 ложную находку на давно работающих фейках.
        """
        source = '''
def f(store):
    store.save_settings({"a": 1})
'''
        required = self.mod.collect_required_kwargs([("svc.py", source)], self.known)

        self.assertNotIn("save_settings", required)

    def test_ignores_methods_outside_the_known_api(self) -> None:
        source = '''
def f(obj):
    obj.some_unrelated_call(nowait=True)
'''
        required = self.mod.collect_required_kwargs([("svc.py", source)], self.known)

        self.assertEqual(required, {})


class FindFakeDriftTest(unittest.TestCase):
    """Обнаружение расхождения в тестовых фейках."""

    def setUp(self) -> None:
        self.mod = _load_module()
        self.required = {"load_settings": {"lock_timeout_sec", "nowait"}}
        self.known = {"load_settings", "save_settings", "count_active_items"}

    def _find(self, source: str):
        return self.mod.find_fake_drift(source, "test_x.py", self.required, self.known)

    def test_flags_fake_store_class_missing_kwarg(self) -> None:
        source = '''
class FakeStore:
    def load_settings(self):
        return {}
'''
        findings = self._find(source)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].method, "load_settings")
        self.assertEqual(sorted(findings[0].missing), ["lock_timeout_sec", "nowait"])

    def test_accepts_fake_with_matching_signature(self) -> None:
        source = '''
class FakeStore:
    def load_settings(self, lock_timeout_sec=None, nowait=False):
        return {}
'''
        self.assertEqual(self._find(source), [])

    def test_accepts_fake_with_var_keyword(self) -> None:
        """**kwargs принимает что угодно — дрейфа быть не может."""
        source = '''
class FakeStore:
    def load_settings(self, **kwargs):
        return {}
'''
        self.assertEqual(self._find(source), [])

    def test_flags_nested_factory_function(self) -> None:
        """MagicMock-фабрика со вложенной функцией — тот же дрейф."""
        source = '''
def _make_store():
    store = MagicMock()

    def load_settings():
        return {}

    store.load_settings.side_effect = load_settings
    return store
'''
        findings = self._find(source)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].method, "load_settings")

    def test_does_not_flag_unrelated_same_named_function(self) -> None:
        """Одноимённая функция вне фейка стора — не наша цель.

        Иначе гард начнёт краснеть на произвольном коде и его отключат.
        """
        source = '''
def load_settings():
    """Хелпер теста, к StateStore отношения не имеет."""
    return {}
'''
        self.assertEqual(self._find(source), [])

    def test_recognises_fake_by_two_matching_methods(self) -> None:
        """Класс без Store/Fake в имени, но со структурой стора — тоже фейк."""
        source = '''
class _Persistence:
    def load_settings(self):
        return {}

    def save_settings(self, s):
        return s
'''
        findings = self._find(source)

        self.assertEqual(len(findings), 1)


class RepositoryIsCleanTest(unittest.TestCase):
    """Регрессия: в дереве не осталось разошедшихся фейков."""

    def test_audit_reports_no_drift(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--fail-on-found"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

        self.assertEqual(
            result.returncode, 0,
            f"гард нашёл расхождения:\n{result.stdout}\n{result.stderr}",
        )

    def test_selftest_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--selftest"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
